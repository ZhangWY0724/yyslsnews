from urllib.parse import quote

import pytest

from yysls_news.rendering.renderer import HtmlRenderer, PlaywrightRenderer


def test_bilibili_template_formats_structured_content_as_text() -> None:
    html = HtmlRenderer().render_bilibili(
        {
            "author_name": "测试UP",
            "dynamic_type_name": "图文",
            "content": (
                "{'text': '正文内容', 'rich_text_nodes': [], "
                "'paragraphs': [], 'has_more': False}"
            ),
            "source_url": "https://t.bilibili.com/1",
        }
    )

    assert "正文内容" in html
    assert "rich_text_nodes" not in html
    assert "{'text'" not in html


def test_bilibili_template_renders_generated_rich_content_html() -> None:
    html = HtmlRenderer().render_bilibili(
        {
            "author_name": "测试UP",
            "content_html": '<a href="https://example.com/topic">#话题</a><br>正文',
            "source_url": "https://t.bilibili.com/1",
        }
    )

    assert '<a href="https://example.com/topic">#话题</a>' in html
    assert "<br>正文" in html


@pytest.mark.asyncio
async def test_playwright_renderer_creates_complete_dynamic_image(tmp_path) -> None:
    payload = {
        "author_name": "测试UP",
        "dynamic_type_name": "文字",
        "title": "本地模板渲染",
        "content": "使用统一 Playwright 引擎将本地 HTML 模板截图为图片。",
        "avatar_url": "",
        "images": [],
        "source_url": "https://t.bilibili.com/1",
    }

    output = await PlaywrightRenderer().render_bilibili(payload, tmp_path / "dynamic.png")
    assert output.is_file()
    assert output.stat().st_size > 0


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
