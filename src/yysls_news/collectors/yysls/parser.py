from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


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
        if not href:
            continue
        url = urljoin(base_url, href)
        parsed_url = urlsplit(url)
        if parsed_url.hostname != urlsplit(base_url).hostname:
            continue
        path = parsed_url.path.lower()
        if "/news/" not in path or not path.endswith(".html"):
            continue
        if re.search(r"(?:^|/)index(?:_\d+)?\.html$", path):
            continue
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
