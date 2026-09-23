from urllib.parse import quote

import pytest
from PIL import Image

from yysls_news.rendering.renderer import PlaywrightRenderer


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

    output = await PlaywrightRenderer().render_yysls(url, tmp_path / "official-content.png")
    assert output.is_file()
    assert output.stat().st_size > 0
    with Image.open(output) as screenshot:
        assert screenshot.width == 420
