from yysls_news.collectors.yysls.parser import parse_listing

LIST_HTML = """
<html><body>
  <a href="/news/2026/09/22/100.html"><span>公告</span> 新版本更新说明</a>
  <a href="/news/2026/09/21/099.html">旧文章</a>
  <a href="/not-news/100.html">不应匹配</a>
  <a href="/news/index_2.html">2</a>
  <a href="/news/index.html">末页</a>
</body></html>
"""


def test_parse_listing_extracts_news_links() -> None:
    items = parse_listing(LIST_HTML, "https://www.yysls.cn/news/")

    assert len(items) == 2
    assert items[0].title == "新版本更新说明"
    assert items[0].url.endswith("/100.html")
    assert items[0].category == "公告"
