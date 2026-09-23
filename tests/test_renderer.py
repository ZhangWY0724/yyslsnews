from urllib.parse import quote

import pytest

from yysls_news.rendering.renderer import PlaywrightRenderer


@pytest.mark.asyncio
async def test_playwright_renderer_captures_official_content_selector(tmp_path) -> None:
    html = """
    <html><body>
      <div id="outside">不应被截取</div>
      <div id="NIE-art" style="width: 240px; padding: 20px; background: #182333; color: white;">
        <h1>官网正文</h1><p>保留指定容器内容。</p>
      </div>
    </body></html>
    """
    url = "data:text/html," + quote(html)

    output = await PlaywrightRenderer().capture_url(
        url,
        tmp_path / "official-content.png",
        selector="#NIE-art",
    )
    assert output.is_file()
    assert output.stat().st_size > 0
