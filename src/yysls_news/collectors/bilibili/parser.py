from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from yysls_news.domain.models import BiliDynamicViewModel, NormalizedContent, SourceType

TYPE_NAMES = {
    "DYNAMIC_TYPE_AV": "视频",
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_WORD": "文字",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_FORWARD": "转发",
}


def _value(data: Any, *keys: str, default: Any = "") -> Any:
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _rich_text_nodes(nodes: Any) -> str:
    if not isinstance(nodes, list):
        return ""
    return _clean_text(
        "".join(
            content_to_text(node.get("text", ""))
            for node in nodes
            if isinstance(node, dict)
        )
    )


def content_to_text(value: Any) -> str:
    """将B站动态正文结构转换为可读文本，避免把接口字典直接展示出来。"""
    if isinstance(value, str):
        text = _clean_text(value)
        if text.startswith("{") and len(text) <= 100_000:
            try:
                legacy_value = ast.literal_eval(text)
            except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
                legacy_value = None
            if isinstance(legacy_value, dict) and {
                "text",
                "rich_text_nodes",
                "paragraphs",
            }.intersection(legacy_value):
                legacy_text = content_to_text(legacy_value)
                if legacy_text:
                    return legacy_text
        return text
    if isinstance(value, dict):
        direct_text = value.get("text")
        if isinstance(direct_text, str):
            direct_text = _clean_text(direct_text)
            if direct_text:
                return direct_text
        if isinstance(direct_text, (dict, list)):
            nested_text = content_to_text(direct_text)
            if nested_text:
                return nested_text

        rich_text = _rich_text_nodes(value.get("rich_text_nodes"))
        if rich_text:
            return rich_text

        paragraphs = value.get("paragraphs")
        if isinstance(paragraphs, list):
            paragraph_text = "\n".join(
                text
                for text in (content_to_text(paragraph) for paragraph in paragraphs)
                if text
            )
            if paragraph_text:
                return _clean_text(paragraph_text)
        return ""
    if isinstance(value, list):
        list_text = "\n".join(
            text for text in (content_to_text(item) for item in value) if text
        )
        return _clean_text(list_text)
    if isinstance(value, (int, float, bool)):
        return _clean_text(value)
    return ""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str) and value.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
                return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
            except ValueError:
                continue
    return None


@dataclass(frozen=True)
class _DynamicParts:
    """按动态类型提取筛选和文字回退需要的字段。"""

    title: str = ""
    content: str = ""


def extract_items(raw: dict[str, Any]) -> list[dict[str, Any]]:
    payload = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    items = payload.get("items", []) if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)]


def extract_new_items(
    raw: dict[str, Any],
    last_dynamic_id: str = "",
    recent_dynamic_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """按参考插件的游标逻辑提取新动态，返回顺序为最新到最旧。"""
    known_ids = {str(value) for value in recent_dynamic_ids if value}
    if last_dynamic_id:
        known_ids.add(str(last_dynamic_id))

    new_items: list[dict[str, Any]] = []
    for item in extract_items(raw):
        modules = item.get("modules")
        if not isinstance(modules, dict):
            continue
        if _value(modules, "module_tag", "text") == "置顶":
            continue
        dynamic_id = str(item.get("id_str") or item.get("id") or "")
        if not dynamic_id:
            continue
        if dynamic_id in known_ids:
            break
        new_items.append(item)
    return new_items


def _parse_video_parts(dynamic: Any, major: Any) -> _DynamicParts:
    """视频动态使用 archive，正文仍来自动态 desc。"""
    archive = _value(major, "archive", default={})
    title = content_to_text(_value(archive, "title"))
    desc = _value(dynamic, "desc", default={})
    content = content_to_text(desc)
    archive_desc_value = _value(archive, "desc")
    archive_desc = content_to_text(archive_desc_value)
    if archive_desc and archive_desc not in content:
        content = f"{content}\n{archive_desc}".strip()
    return _DynamicParts(title=title, content=content)


def _parse_opus_parts(
    dynamic: Any,
    dynamic_type: str,
    major: Any,
) -> _DynamicParts:
    """图文、文字和专栏优先使用 opus，兼容旧 article/desc 字段。"""
    opus = _value(major, "opus", default={})
    article = _value(major, "article", default={})
    summary = _value(opus, "summary", default={})
    desc = _value(dynamic, "desc", default={})
    article_desc = _value(article, "desc", default={})

    candidates = [summary, desc, article_desc]
    if dynamic_type == "DYNAMIC_TYPE_ARTICLE":
        candidates = [summary, article_desc, desc]
    content_source = next(
        (candidate for candidate in candidates if content_to_text(candidate)),
        "",
    )
    title = content_to_text(_value(opus, "title") or _value(article, "title"))
    return _DynamicParts(title=title, content=content_to_text(content_source))


def _parse_forward_parts(dynamic: Any) -> _DynamicParts:
    """转发动态自身只承载转发评论，原动态由 parse_dynamic 递归解析。"""
    desc = _value(dynamic, "desc", default={})
    return _DynamicParts(content=content_to_text(desc))


def parse_dynamic(item: dict[str, Any], uid: int) -> BiliDynamicViewModel | None:
    dynamic_id = str(item.get("id_str") or item.get("id") or "")
    if not dynamic_id:
        return None
    dynamic_type = str(item.get("type") or "DYNAMIC_TYPE_UNKNOWN")
    if dynamic_type not in TYPE_NAMES:
        return None
    author = _value(item, "modules", "module_author", default={})
    dynamic = _value(item, "modules", "module_dynamic", default={})
    major = _value(dynamic, "major", default={})

    if dynamic_type == "DYNAMIC_TYPE_AV":
        parts = _parse_video_parts(dynamic, major)
    elif dynamic_type in {
        "DYNAMIC_TYPE_ARTICLE",
        "DYNAMIC_TYPE_DRAW",
        "DYNAMIC_TYPE_WORD",
    }:
        parts = _parse_opus_parts(dynamic, dynamic_type, major)
    else:
        parts = _parse_forward_parts(dynamic)

    author_name = _clean_text(_value(author, "name"))
    published_at = _parse_datetime(_value(author, "pub_ts")) or _parse_datetime(
        _value(author, "pub_time")
    )
    source_url = (
        f"https://www.bilibili.com/opus/{dynamic_id}"
        if dynamic_type == "DYNAMIC_TYPE_ARTICLE"
        else f"https://t.bilibili.com/{dynamic_id}"
    )

    if dynamic_type == "DYNAMIC_TYPE_FORWARD":
        original = item.get("orig")
        if isinstance(original, dict):
            original_model = parse_dynamic(original, uid)
            if original_model:
                original_text = "\n".join(
                    part for part in (original_model.title, original_model.content) if part
                )
                parts = _DynamicParts(
                    content="\n".join(part for part in (parts.content, original_text) if part)
                )

    return BiliDynamicViewModel(
        dynamic_id=dynamic_id,
        dynamic_type=dynamic_type,
        uid=uid,
        author_name=author_name,
        publish_time=published_at,
        title=parts.title,
        content=parts.content,
        source_url=source_url,
    )


def matches_filters(
    model: BiliDynamicViewModel, filter_types: Iterable[str], keywords: Iterable[str]
) -> bool:
    type_name = TYPE_NAMES.get(model.dynamic_type, model.dynamic_type)
    if any(value in {model.dynamic_type, type_name} for value in filter_types):
        return False
    haystack = f"{model.title}\n{model.content}"
    return not any(keyword and keyword.lower() in haystack.lower() for keyword in keywords)


def to_normalized_content(model: BiliDynamicViewModel) -> NormalizedContent:
    return NormalizedContent(
        source_type=SourceType.BILIBILI,
        source_key=str(model.uid),
        external_id=model.dynamic_id,
        title=model.title or f"{TYPE_NAMES.get(model.dynamic_type, '动态')}动态",
        author=model.author_name,
        category=TYPE_NAMES.get(model.dynamic_type, "动态"),
        source_url=model.source_url,
        published_at=model.publish_time,
    )
