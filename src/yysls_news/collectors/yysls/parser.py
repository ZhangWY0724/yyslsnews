from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from yysls_news.domain.models import NormalizedContent, SourceType


@dataclass(frozen=True)
class NewsListItem:
    title: str
    url: str
    category: str
    published_at: datetime | None


DATE_PATTERN = re.compile(
    r"(?P<full>20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})|(?P<short>\d{1,2}[-/.]\d{1,2})"
)


def _clean_text(value: str) -> str:
    value = re.sub(r"[ \t]+", " ", value.replace("\r", ""))
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _parse_date(text: str) -> datetime | None:
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    raw = match.group("full") or match.group("short")
    raw = raw.replace("/", "-").replace(".", "-")
    try:
        if match.group("full"):
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        current_year = datetime.now(timezone.utc).year
        return datetime.strptime(f"{current_year}-{raw}", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _category_from_text(text: str, default: str) -> str:
    for category in ("公告", "新闻", "活动"):
        if category in text:
            return category
    return default


def parse_listing(
    html: str,
    base_url: str,
    category: str = "最新",
) -> list[NewsListItem]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[NewsListItem] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or "/news/" not in href or not href.lower().endswith(".html"):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        text = _clean_text(anchor.get_text(" ", strip=True))
        if not text:
            continue
        parent_text = (
            _clean_text(anchor.parent.get_text(" ", strip=True)) if anchor.parent else text
        )
        result.append(
            NewsListItem(
                title=_remove_category_prefix(text),
                url=url,
                category=_category_from_text(parent_text, category),
                published_at=_parse_date(parent_text),
            )
        )
    return result


def _remove_category_prefix(title: str) -> str:
    return re.sub(r"^(最新|新闻|公告|活动)\s*", "", title).strip()


def parse_article(
    html: str,
    url: str,
    list_item: NewsListItem | None = None,
) -> NormalizedContent:
    soup = BeautifulSoup(html, "html.parser")
    title = list_item.title if list_item else ""
    if not title:
        heading = soup.select_one("h1, .title, .article-title")
        title = _clean_text(heading.get_text(" ", strip=True)) if heading else ""
    if not title and soup.title:
        title = _clean_text(soup.title.get_text(" ", strip=True))

    category = (
        list_item.category
        if list_item
        else _category_from_text(soup.get_text(" ", strip=True), "新闻")
    )
    published_at = (
        list_item.published_at if list_item else _parse_date(soup.get_text(" ", strip=True))
    )

    root = _find_article_root(soup)
    for tag in root.select("script, style, noscript, iframe, header, footer, nav"):
        tag.decompose()
    for image in root.select("img"):
        if isinstance(image, Tag):
            source = image.get("src") or image.get("data-src")
            if source:
                image["src"] = urljoin(url, str(source))
            image.attrs = {
                key: value for key, value in image.attrs.items() if key in {"src", "alt"}
            }

    content_text = _clean_text(root.get_text("\n", strip=True))
    content_html = "".join(str(child) for child in root.contents).strip()
    return NormalizedContent(
        source_type=SourceType.YYSLS,
        source_key="official",
        external_id=url,
        title=title,
        author="燕云十六声官网",
        category=category,
        content_text=content_text,
        content_html=content_html,
        source_url=url,
        published_at=published_at,
        render_payload={
            "title": title,
            "category": category,
            "published_at": published_at.isoformat() if published_at else "",
            "content_text": content_text,
            "content_html": content_html,
            "source_url": url,
        },
        raw_payload={"url": url, "html": html},
    )


def _find_article_root(soup: BeautifulSoup) -> Tag:
    selectors = (
        "#NIE-art",
        "article",
        ".article-content",
        ".news-content",
        ".content-detail",
        ".article-detail",
        "main",
    )
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            return element
    return soup.body or soup  # type: ignore[return-value]
