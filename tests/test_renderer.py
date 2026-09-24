from urllib.parse import quote

import pytest
from PIL import Image

from yysls_news.rendering.renderer import (
    MAX_UPLOAD_IMAGE_HEIGHT,
    PAGE_OVERLAP_PIXELS,
    PlaywrightRenderer,
)


@pytest.mark.asyncio
async def test_playwright_renderer_captures_official_page_container(tmp_path) -> None:
    html = """
    <html><body>
      <div id="outside">不应被截取</div>
      <div class="container-all news" style="width: 420px; background: #182333; color: white;">
        <div id="NIE-art" style="width: 240px; padding: 20px;">
          <h1>官网正文</h1><p>保留指定容器内容。</p>
        </div>
      </div>
    </body></html>
    """
    url = "data:text/html," + quote(html)

    outputs = await PlaywrightRenderer().render_yysls(url, tmp_path / "official-content.png")
    assert len(outputs) == 1
    output = outputs[0]
    assert output.is_file()
    assert output.stat().st_size > 0
    with Image.open(output) as screenshot:
        assert screenshot.width == 420


@pytest.mark.asyncio
async def test_tall_official_page_is_captured_in_ordered_browser_pages(tmp_path) -> None:
    html = f"""
    <html><body style="margin: 0">
      <div class="container-all news" style="width: 200px">
        <div id="NIE-art">
          <div style="height: {MAX_UPLOAD_IMAGE_HEIGHT // 2}px; background: red"></div>
          <div style="height: {MAX_UPLOAD_IMAGE_HEIGHT // 2}px; background: blue"></div>
          <div style="height: 200px; background: green"></div>
        </div>
      </div>
    </body></html>
    """
    url = "data:text/html," + quote(html)

    pages = await PlaywrightRenderer().render_yysls(url, tmp_path / "official-long.png")

    assert len(pages) == 2
    with Image.open(pages[0]) as first, Image.open(pages[1]) as second:
        assert first.width == second.width == 200
        assert first.height <= MAX_UPLOAD_IMAGE_HEIGHT
        assert second.height <= MAX_UPLOAD_IMAGE_HEIGHT
        assert first.height + second.height - PAGE_OVERLAP_PIXELS == 16200
        assert first.getpixel((10, 7990))[:3] == (255, 0, 0)
        assert first.getpixel((10, 8100))[:3] == (0, 0, 255)
        assert second.getpixel((10, 0))[:3] == (0, 0, 255)
        assert second.getpixel((10, second.height - 10))[:3] == (0, 128, 0)


def test_tall_bilibili_screenshot_paginates_without_losing_content(tmp_path) -> None:
    path = tmp_path / "dynamic.jpg"
    height = MAX_UPLOAD_IMAGE_HEIGHT + 1
    source = Image.new("RGB", (20, height), "red")
    source.paste("blue", (0, 8000, 20, height))
    source.paste("green", (0, height - 100, 20, height))
    source.save(path, format="JPEG", quality=95)
    source.close()

    pages = PlaywrightRenderer.paginate_bilibili_image(path)

    assert len(pages) == 2
    assert path.is_file()
    with Image.open(pages[0]) as first, Image.open(pages[1]) as second:
        assert first.width == second.width == 20
        assert first.height <= MAX_UPLOAD_IMAGE_HEIGHT
        assert second.height <= MAX_UPLOAD_IMAGE_HEIGHT
        assert first.height + second.height - PAGE_OVERLAP_PIXELS == height
        assert first.getpixel((5, 5))[0] > 200
        assert second.getpixel((5, 50))[2] > 200
        assert second.getpixel((5, second.height - 50))[1] > 100


@pytest.mark.asyncio
async def test_bilibili_opus_ignores_hidden_empty_image_placeholder(tmp_path) -> None:
    html = """
    <html><body>
      <div class="bili-opus-view"
           style="box-sizing: border-box; width: 320px; padding: 20px; background: white;">
        <h1>图文动态正文</h1>
        <img src="" style="display: none">
        <img src="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20width='40'%20height='40'%3E%3Crect%20width='40'%20height='40'%20fill='blue'/%3E%3C/svg%3E">
      </div>
    </body></html>
    """
    url = "data:text/html," + quote(html)

    output = await PlaywrightRenderer(timeout_ms=2_000).render_bilibili(
        url, tmp_path / "opus.jpg"
    )

    assert output.is_file()
    with Image.open(output) as screenshot:
        assert screenshot.width == 320
