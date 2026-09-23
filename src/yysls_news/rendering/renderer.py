from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class ImageRenderError(RuntimeError):
    """Playwright 图片渲染失败。"""


class PlaywrightRenderer:
    """按需打开详情页，截取 B站动态或官网新闻正文。"""

    def __init__(
        self,
        viewport_width: int = 1080,
        viewport_height: int = 900,
        timeout_ms: int = 20_000,
    ) -> None:
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
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
            viewport_width=1280,
            hide_css=".bili-header__menu, .login-tip { display: none !important; }",
            require_images=True,
        )

    async def render_yysls(
        self, source_url: str, output_path: str | Path
    ) -> Path:
        source_url = source_url.strip()
        if not source_url:
            raise ImageRenderError("官网新闻缺少原文地址")
        return await self.capture_url(source_url, output_path, selector="#NIE-art")

    async def capture_url(
        self,
        url: str,
        output_path: str | Path,
        selector: str = "#NIE-art",
        viewport_width: int | None = None,
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
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(
                        viewport={
                            "width": viewport_width or self.viewport_width,
                            "height": self.viewport_height,
                        },
                        device_scale_factor=1,
                    )
                    response = await page.goto(
                        url, wait_until="commit", timeout=max(self.timeout_ms, 30_000)
                    )
                    if (response is None and url.startswith(("http://", "https://"))) or (
                        response is not None and response.status >= 400
                    ):
                        raise ImageRenderError("详情页不可访问")
                    article = page.locator(selector).first
                    await article.wait_for(state="visible", timeout=self.timeout_ms)
                    await self._scroll_article(page, article)
                    try:
                        await page.wait_for_function(
                            """selector => {
                                const root = document.querySelector(selector);
                                return root && Array.from(root.querySelectorAll('img'))
                                    .every(image => image.complete && image.naturalWidth > 0);
                            }""",
                            arg=selector,
                            timeout=self.timeout_ms,
                        )
                    except PlaywrightTimeoutError as exc:
                        if require_images:
                            raise ImageRenderError("正文图片加载超时") from exc
                        LOGGER.warning("详情页部分图片加载超时，仍截取原页面: %s", url)
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
        except ImageRenderError:
            raise
        except Exception as exc:
            raise ImageRenderError(
                f"详情页截图失败: {type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        if not output.is_file() or output.stat().st_size == 0:
            raise ImageRenderError("详情页截图未生成有效文件")
        return output

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
