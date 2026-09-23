from __future__ import annotations

from dataclasses import dataclass

import httpx

from yysls_news.collectors.yysls.parser import NewsListItem, parse_listing

DEFAULT_NEWS_URL = "https://www.yysls.cn/news/"


class YyslsRequestError(RuntimeError):
    """官网请求或解析失败。"""


@dataclass(frozen=True)
class YyslsClient:
    timeout_seconds: float = 20
    user_agent: str = "yysls-news/0.1 (+public-news-monitor)"

    async def discover(
        self,
        list_url: str = DEFAULT_NEWS_URL,
        category: str = "最新",
    ) -> list[NewsListItem]:
        html = await self._get(list_url)
        return parse_listing(html, list_url, category)

    async def _get(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                response.encoding = response.encoding or "utf-8"
                return response.text
        except httpx.HTTPError as exc:
            raise YyslsRequestError(f"请求官网失败: {url}") from exc
