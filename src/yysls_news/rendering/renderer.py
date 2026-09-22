from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from yysls_news.collectors.bilibili.parser import content_to_text


class HtmlRenderer:
    """渲染本地 HTML 模板，实际图片由 Playwright 截图生成。"""

    def __init__(self, template_root: str | Path | None = None) -> None:
        root = Path(template_root) if template_root else Path(__file__).parent / "templates"
        self.environment = Environment(
            loader=FileSystemLoader(root),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.environment.filters["bili_text"] = content_to_text

    def render_bilibili(self, payload: dict[str, Any]) -> str:
        return self.environment.get_template("bilibili/dynamic.html").render(payload=payload)

    def render_yysls(self, payload: dict[str, Any]) -> str:
        return self.environment.get_template("yysls/article.html").render(payload=payload)


class ImageRenderError(RuntimeError):
    """Playwright 图片渲染失败。"""


class PlaywrightRenderer:
    """使用同一套 Playwright 流程生成 B站和官网图片。

    B站使用本地模板页面截图；官网优先截图公开详情页的 #NIE-art 容器，
    保留官网实际 HTML/CSS 样式，访问失败时再使用本地模板页面截图。
    浏览器按任务按需启动，截图完成后立即关闭，不常驻浏览器进程。
    """

    def __init__(
        self,
        template_root: str | Path | None = None,
        viewport_width: int = 1080,
        viewport_height: int = 900,
        timeout_ms: int = 20_000,
    ) -> None:
        self.html_renderer = HtmlRenderer(template_root)
        self.viewport_width = viewport_width
        self.viewport_height = viewport_height
        self.timeout_ms = timeout_ms

    async def render_bilibili(
        self,
        payload: dict[str, Any],
        output_path: str | Path,
    ) -> Path:
        html = self.html_renderer.render_bilibili(payload)
        return await self.capture_html(html, output_path)

    async def render_yysls(
        self,
        payload: dict[str, Any],
        output_path: str | Path,
    ) -> Path:
        source_url = str(payload.get("source_url") or "").strip()
        fallback_html = self.html_renderer.render_yysls(payload)
        if source_url:
            try:
                return await self.capture_url(source_url, output_path, selector="#NIE-art")
            except ImageRenderError:
                # 官网页面结构或网络短暂异常时，仍然使用同一 Playwright 引擎渲染本地模板。
                pass
        return await self.capture_html(fallback_html, output_path)

    async def capture_html(
        self,
        html: str,
        output_path: str | Path,
        selector: str = "#capture",
    ) -> Path:
        """将 HTML 字符串加载到 Playwright 页面并截图指定容器。"""
        return await self._capture(
            output_path=output_path,
            page_loader=self._load_html(html),
            selector=selector,
        )

    async def capture_url(
        self,
        url: str,
        output_path: str | Path,
        selector: str = "#NIE-art",
    ) -> Path:
        """打开真实页面并截取指定 DOM 容器。"""
        return await self._capture(
            output_path=output_path,
            page_loader=self._load_url(url),
            selector=selector,
        )

    async def _capture(self, output_path: str | Path, page_loader: Any, selector: str) -> Path:
        try:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError(
                "未安装 Playwright，请执行 `pip install -e .` 并安装 Chromium 浏览器"
            ) from exc

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        viewport={
                            "width": self.viewport_width,
                            "height": self.viewport_height,
                        },
                        device_scale_factor=1,
                    )
                    try:
                        page = await context.new_page()
                        await page_loader(page)
                        locator = page.locator(selector).first
                        await locator.wait_for(state="visible", timeout=self.timeout_ms)
                        await locator.scroll_into_view_if_needed(timeout=self.timeout_ms)
                        await self._wait_for_images(page, PlaywrightTimeoutError)
                        await locator.screenshot(path=str(output), type="png")
                    finally:
                        await context.close()
                finally:
                    await browser.close()
        except ImageRenderError:
            raise
        except Exception as exc:
            raise ImageRenderError(
                f"Playwright 截图失败: {type(exc).__name__}: {str(exc)[:180]}"
            ) from exc
        if not output.is_file() or output.stat().st_size == 0:
            raise ImageRenderError("Playwright 截图未生成有效文件")
        return output

    def _load_html(self, html: str):
        async def loader(page: Any) -> None:
            await page.set_content(html, wait_until="domcontentloaded", timeout=self.timeout_ms)

        return loader

    def _load_url(self, url: str):
        async def loader(page: Any) -> None:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)

        return loader

    async def _wait_for_images(self, page: Any, timeout_error: Any) -> None:
        try:
            await page.wait_for_function(
                """
                () => Array.from(document.images).every((image) => image.complete)
                """,
                timeout=self.timeout_ms,
            )
        except timeout_error as exc:
            raise ImageRenderError("图片资源加载超时") from exc
