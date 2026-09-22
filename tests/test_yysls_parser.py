from yysls_news.collectors.yysls.parser import parse_article, parse_listing

LIST_HTML = """
<html><body>
  <a href="/news/2026/09/22/100.html"><span>公告</span> 新版本更新说明</a>
  <a href="/news/2026/09/21/099.html">旧文章</a>
  <a href="/not-news/100.html">不应匹配</a>
</body></html>
"""

ARTICLE_HTML = """
<html><head><title>官网页面标题</title><script>alert('x')</script></head>
<body><header>网站导航</header>
<div class="contentbox NIE-art" id="NIE-art">
  <h1>新版本更新说明</h1>
  <p>第一段正文</p><img data-src="/assets/a.png" onerror="bad()" alt="配图">
  <script>console.log('remove')</script><p>第二段正文</p>
</div><footer>页脚</footer></body></html>
"""


def test_parse_listing_extracts_news_links() -> None:
    items = parse_listing(LIST_HTML, "https://www.yysls.cn/news/")

    assert len(items) == 2
    assert items[0].title == "新版本更新说明"
    assert items[0].url.endswith("/100.html")
    assert items[0].category == "公告"


def test_parse_article_removes_scripts_and_resolves_images() -> None:
    item = parse_listing(LIST_HTML, "https://www.yysls.cn/news/")[0]
    content = parse_article(ARTICLE_HTML, item.url, item)

    assert content.source_url.endswith("/100.html")
    assert "alert" not in content.content_html
    assert "console.log" not in content.content_html
    assert "https://www.yysls.cn/assets/a.png" in content.content_html
    assert "onerror" not in content.content_html
    assert "第一段正文" in content.content_text


def test_parse_article_prefers_official_content_container() -> None:
    content = parse_article(ARTICLE_HTML, "https://www.yysls.cn/news/2026/09/22/100.html")

    assert "新版本更新说明" in content.content_html
    assert "页脚" not in content.content_html
