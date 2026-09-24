from __future__ import annotations

import base64
import io
import logging
import time
from importlib.resources import files
from math import ceil, floor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from jinja2 import Environment
from PIL import Image

LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_IMAGE_HEIGHT = 16_000
PAGE_OVERLAP_PIXELS = 100
VISIBLE_IMAGES_READY = """selector => {
    const root = document.querySelector(selector);
    if (!root) return false;
    return Array.from(root.querySelectorAll('img'))
        .filter(image => {
            const bounds = image.getBoundingClientRect();
            return bounds.width > 0 && bounds.height > 0;
        })
        .every(image => image.complete && image.naturalWidth > 0);
}"""
VIDEO_CARD_TEMPLATE = (
    files(__package__).joinpath("templates", "bilibili_video.html").read_text(
        encoding="utf-8"
    )
)
VIDEO_CARD_ENVIRONMENT = Environment(autoescape=True)


class ImageRenderError(RuntimeError):
    """Playwright 图片渲染失败。"""


class PlaywrightRenderer:
    """渲染 B站视频卡片、动态正文和官网新闻正文。"""

    def __init__(self, timeout_ms: int = 20_000) -> None:
        self.timeout_ms = timeout_ms

    async def render_bilibili(
        self, source_url: str, output_path: str | Path
    ) -> Path:
        source_url = source_url.strip()
        if not source_url:
            raise ImageRenderError("B站动态缺少原文地址")
        return await self.capture_url(
            source_url,
            Path(output_path).with_suffix(".jpg"),
            selector=".bili-opus-view, .bili-dyn-detail__content",
            hide_css=".bili-header__menu, .login-tip { display: none !important; }",
            require_images=True,
        )

    async def render_bilibili_video(
        self,
        title: str,
        author: str,
        body_text: str,
        cover_url: str,
        video_url: str,
        output_path: str | Path,
    ) -> Path:
        """用结构化动态数据生成视频投稿卡片，不打开视频播放页。"""
        output = Path(output_path).with_suffix(".jpg")
        output.parent.mkdir(parents=True, exist_ok=True)
        safe_cover_url = _bilibili_cover_url(cover_url)
        safe_video_url = video_url.strip()
        qrcode_url = _video_qrcode_data_url(safe_video_url)
        html = VIDEO_CARD_ENVIRONMENT.from_string(VIDEO_CARD_TEMPLATE).render(
            title=title.strip() or "投稿了新视频",
            author=author.strip() or "未知 UP 主",
            body_text=body_text.strip(),
            cover_url=safe_cover_url,
            video_url=safe_video_url,
            qrcode_url=qrcode_url,
        )
        started = time.monotonic()
        LOGGER.info(
            "B站视频卡片渲染开始: selector=.bilibili-video-card viewport=1920x1080 output=%s",
            output.name,
        )
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError("未安装 Playwright") from exc

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(
                        viewport={"width": 1920, "height": 1080},
                        device_scale_factor=1,
                        extra_http_headers={"Referer": "https://www.bilibili.com/"},
                    )
                    await page.set_content(
                        html,
                        wait_until="domcontentloaded",
                        timeout=max(self.timeout_ms, 30_000),
                    )
                    card = page.locator(".bilibili-video-card").first
                    await card.wait_for(state="visible", timeout=self.timeout_ms)
                    if safe_cover_url:
                        await page.wait_for_function(
                            """() => {
                                const image = document.querySelector('.video-cover img');
                                return image && image.complete && image.naturalWidth > 0;
                            }""",
                            timeout=self.timeout_ms,
                        )
                    await page.evaluate("() => document.fonts.ready")
                    await card.screenshot(
                        path=str(output),
                        type="jpeg",
                        quality=88,
                        animations="disabled",
                        timeout=120_000,
                    )
                finally:
                    await browser.close()
        except ImageRenderError:
            raise
        except Exception as exc:
            error = (
                "视频封面加载超时"
                if isinstance(exc, PlaywrightTimeoutError) and safe_cover_url
                else f"视频卡片截图失败: {type(exc).__name__}: {str(exc)[:180]}"
            )
            LOGGER.warning(
                "B站视频卡片渲染失败: error=%s duration_ms=%s",
                error[:120],
                int((time.monotonic() - started) * 1000),
            )
            raise ImageRenderError(error) from exc

        if not output.is_file() or output.stat().st_size == 0:
            raise ImageRenderError("B站视频卡片未生成有效图片")
        with Image.open(output) as screenshot:
            width, height = screenshot.size
        LOGGER.info(
            "B站视频卡片渲染完成: size=%sx%s bytes=%s duration_ms=%s",
            width,
            height,
            output.stat().st_size,
            int((time.monotonic() - started) * 1000),
        )
        return output

    async def render_yysls(
        self, source_url: str, output_path: str | Path
    ) -> list[Path]:
        source_url = source_url.strip()
        if not source_url:
            raise ImageRenderError("官网新闻缺少原文地址")
        return await self.capture_url_pages(
            source_url,
            output_path,
            selector=".container-all.news",
            image_root_selector="#NIE-art",
        )

    async def capture_url(
        self,
        url: str,
        output_path: str | Path,
        selector: str = "#NIE-art",
        image_root_selector: str | None = None,
        hide_css: str = "",
        require_images: bool = False,
    ) -> Path:
        """滚动正文以触发懒加载，再截取原网页的指定容器。"""
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError("未安装 Playwright") from exc

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        host = urlsplit(url).hostname or "local"
        LOGGER.info(
            "截图开始: host=%s selector=%s viewport=1920x1080 output=%s",
            host,
            selector,
            output.name,
        )
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(
                        viewport={"width": 1920, "height": 1080},
                        device_scale_factor=1,
                    )
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=max(self.timeout_ms, 30_000),
                    )
                    if (response is None and url.startswith(("http://", "https://"))) or (
                        response is not None and response.status >= 400
                    ):
                        raise ImageRenderError("详情页不可访问")
                    LOGGER.info(
                        "详情页已载入: host=%s HTTP=%s duration_ms=%s",
                        host,
                        response.status if response else "local",
                        int((time.monotonic() - started) * 1000),
                    )
                    article = page.locator(selector).first
                    await article.wait_for(state="visible", timeout=self.timeout_ms)
                    await self._scroll_article(page, article)
                    # 等待网页字体就绪，避免在自定义字体加载前截到回退字体。
                    await page.evaluate("() => document.fonts.ready")
                    try:
                        await page.wait_for_function(
                            VISIBLE_IMAGES_READY,
                            arg=image_root_selector or selector,
                            timeout=self.timeout_ms,
                        )
                    except PlaywrightTimeoutError as exc:
                        if require_images:
                            raise ImageRenderError("正文图片加载超时") from exc
                        LOGGER.warning(
                            "正文图片加载超时，仍截取已加载页面: host=%s selector=%s",
                            host,
                            image_root_selector or selector,
                        )
                    LOGGER.info(
                        "截图内容已就绪: host=%s selector=%s duration_ms=%s",
                        host,
                        selector,
                        int((time.monotonic() - started) * 1000),
                    )
                    await page.evaluate("window.scrollTo(0, 0)")
                    if hide_css:
                        await page.add_style_tag(content=hide_css)
                    image_type = "jpeg" if output.suffix.lower() in {".jpg", ".jpeg"} else "png"
                    screenshot_options: dict[str, Any] = {
                        "path": str(output),
                        "type": image_type,
                        "animations": "disabled",
                        "timeout": 120_000,
                    }
                    if image_type == "jpeg":
                        screenshot_options["quality"] = 88
                    await article.screenshot(**screenshot_options)
                finally:
                    await browser.close()
        except ImageRenderError as exc:
            LOGGER.warning(
                "截图失败: host=%s selector=%s error=%s duration_ms=%s",
                host,
                selector,
                str(exc)[:120],
                int((time.monotonic() - started) * 1000),
            )
            raise
        except Exception as exc:
            LOGGER.warning(
                "截图异常: host=%s selector=%s error=%s duration_ms=%s",
                host,
                selector,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise ImageRenderError(
                f"详情页截图失败: {type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        if not output.is_file() or output.stat().st_size == 0:
            raise ImageRenderError("详情页截图未生成有效文件")
        with Image.open(output) as screenshot:
            width, height = screenshot.size
        LOGGER.info(
            "截图完成: host=%s selector=%s size=%sx%s bytes=%s duration_ms=%s",
            host,
            selector,
            width,
            height,
            output.stat().st_size,
            int((time.monotonic() - started) * 1000),
        )
        return output

    async def capture_url_pages(
        self,
        url: str,
        output_path: str | Path,
        selector: str,
        image_root_selector: str,
    ) -> list[Path]:
        """在浏览器中按原始宽度分页截图，避免生成超大位图。"""
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError("未安装 Playwright") from exc

        output = Path(output_path).with_suffix(".png")
        output.parent.mkdir(parents=True, exist_ok=True)
        pages: list[Path] = []
        started = time.monotonic()
        host = urlsplit(url).hostname or "local"
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(
                        viewport={"width": 1920, "height": 1080},
                        device_scale_factor=1,
                    )
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=max(self.timeout_ms, 30_000),
                    )
                    if (response is None and url.startswith(("http://", "https://"))) or (
                        response is not None and response.status >= 400
                    ):
                        raise ImageRenderError("详情页不可访问")
                    article = page.locator(selector).first
                    await article.wait_for(state="visible", timeout=self.timeout_ms)
                    await self._scroll_article(page, article)
                    await page.evaluate("() => document.fonts.ready")
                    try:
                        await page.wait_for_function(
                            VISIBLE_IMAGES_READY,
                            arg=image_root_selector,
                            timeout=self.timeout_ms,
                        )
                    except PlaywrightTimeoutError:
                        LOGGER.warning(
                            "官网正文图片加载超时，仍截取已加载页面: host=%s", host
                        )

                    bounds = await article.bounding_box()
                    if bounds is None:
                        raise ImageRenderError("官网正文区域不可见")
                    scroll = await page.evaluate(
                        "() => ({x: window.scrollX, y: window.scrollY})"
                    )
                    left = floor(bounds["x"] + scroll["x"])
                    top = floor(bounds["y"] + scroll["y"])
                    width = ceil(bounds["width"])
                    height = ceil(bounds["height"])
                    if width <= 0 or height <= 0:
                        raise ImageRenderError("官网正文区域尺寸无效")
                    await page.evaluate("window.scrollTo(0, 0)")

                    if height <= MAX_UPLOAD_IMAGE_HEIGHT:
                        pages = [output]
                        await article.screenshot(
                            path=str(output),
                            type="png",
                            animations="disabled",
                            timeout=120_000,
                        )
                    else:
                        page_count = ceil(
                            (height - PAGE_OVERLAP_PIXELS)
                            / (MAX_UPLOAD_IMAGE_HEIGHT - PAGE_OVERLAP_PIXELS)
                        )
                        page_height = ceil(
                            (height + PAGE_OVERLAP_PIXELS * (page_count - 1))
                            / page_count
                        )
                        for index in range(page_count):
                            offset = index * (page_height - PAGE_OVERLAP_PIXELS)
                            bottom = min(offset + page_height, height)
                            part = output.with_name(
                                f"{output.stem}-part-{index + 1:02d}{output.suffix}"
                            )
                            pages.append(part)
                            await page.screenshot(
                                path=str(part),
                                type="png",
                                full_page=True,
                                clip={
                                    "x": left,
                                    "y": top + offset,
                                    "width": width,
                                    "height": bottom - offset,
                                },
                                animations="disabled",
                                timeout=120_000,
                            )
                finally:
                    await browser.close()
            for part in pages:
                if not part.is_file() or part.stat().st_size == 0:
                    raise ImageRenderError("官网截图未生成有效文件")
                with Image.open(part) as screenshot:
                    if screenshot.width != width or screenshot.height > MAX_UPLOAD_IMAGE_HEIGHT:
                        raise ImageRenderError("官网分页截图尺寸异常")
            LOGGER.info(
                "官网截图完成: host=%s size=%sx%s pages=%s duration_ms=%s",
                host,
                width,
                height,
                len(pages),
                int((time.monotonic() - started) * 1000),
            )
            return pages
        except Exception as exc:
            for part in pages:
                part.unlink(missing_ok=True)
            if isinstance(exc, ImageRenderError):
                raise
            raise ImageRenderError(
                f"官网分页截图失败: {type(exc).__name__}: {str(exc)[:180]}"
            ) from exc

    @staticmethod
    def paginate_bilibili_image(path: Path) -> list[Path]:
        """把超高截图分页，保留原始宽度并在页边留少量重叠。"""
        pages: list[Path] = []
        try:
            with Image.open(path) as source:
                width, height = source.size
                if height <= MAX_UPLOAD_IMAGE_HEIGHT:
                    return [path]
                page_count = ceil(
                    (height - PAGE_OVERLAP_PIXELS)
                    / (MAX_UPLOAD_IMAGE_HEIGHT - PAGE_OVERLAP_PIXELS)
                )
                page_height = ceil(
                    (height + PAGE_OVERLAP_PIXELS * (page_count - 1)) / page_count
                )
                for index in range(page_count):
                    top = index * (page_height - PAGE_OVERLAP_PIXELS)
                    bottom = min(top + page_height, height)
                    page_path = path.with_name(f"{path.stem}-part-{index + 1:02d}{path.suffix}")
                    pages.append(page_path)
                    with source.crop((0, top, width, bottom)) as page:
                        page.save(page_path, format="JPEG", quality=88, optimize=True)
            LOGGER.info(
                "B站动态长图分为 %s 页: 原图 %sx%s，每页最高 %s px",
                page_count,
                width,
                height,
                page_height,
            )
            return pages
        except Exception:
            for page_path in pages:
                page_path.unlink(missing_ok=True)
            raise

    async def _scroll_article(self, page: Any, article: Any) -> None:
        bounds = await article.bounding_box()
        if bounds is None:
            raise ImageRenderError("正文区域不可见")
        scroll_y = await page.evaluate("window.scrollY")
        start = int(bounds["y"] + scroll_y)
        end = int(start + bounds["height"]) + 700
        for y in range(start, end, 700):
            await page.evaluate("y => window.scrollTo(0, y)", y)
            await page.wait_for_timeout(80)


def _bilibili_cover_url(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return ""
    hostname = (parsed.hostname or "").lower()
    allowed_domains = ("hdslb.com", "bilivideo.com", "biliimg.com")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or not any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in allowed_domains
        )
    ):
        return ""
    return urlunsplit(
        ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )


def _video_qrcode_data_url(video_url: str) -> str:
    if not video_url:
        return ""
    try:
        import qrcode

        image = qrcode.make(video_url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    except Exception as exc:
        LOGGER.warning("B站视频二维码生成失败: error=%s", type(exc).__name__)
        return ""
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
