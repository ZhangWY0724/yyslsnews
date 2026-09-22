from __future__ import annotations

import ast
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

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


_RICH_TEXT_KEYS = {"text", "rich_text_nodes", "paragraphs"}


def _rich_text_source(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get("text")
    if isinstance(nested, dict) and _RICH_TEXT_KEYS.intersection(nested):
        return nested
    return value


def _safe_http_url(value: Any) -> str:
    url = str(value or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return ""


def content_to_html(value: Any, topic: Any = None) -> str:
    """将B站富文本节点转换为安全的模板片段，保留表情和话题链接。"""
    source = _rich_text_source(value)
    raw_text = source.get("text")
    text = _clean_text(raw_text) if isinstance(raw_text, str) else content_to_text(source)
    rendered = html.escape(text).replace("\n", "<br>")

    nodes = source.get("rich_text_nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type") or "")
            node_text = str(node.get("text") or "")
            if node_type == "RICH_TEXT_NODE_TYPE_EMOJI":
                emoji = node.get("emoji")
                emoji = emoji if isinstance(emoji, dict) else {}
                placeholder = str(emoji.get("text") or node_text)
                icon_url = _safe_http_url(emoji.get("icon_url"))
                if placeholder and icon_url:
                    replacement = (
                        '<img class="rich-emoji" '
                        f'src="{html.escape(icon_url, quote=True)}" '
                        f'alt="{html.escape(placeholder, quote=True)}">'
                    )
                    rendered = rendered.replace(html.escape(placeholder), replacement)
            elif node_type in {
                "RICH_TEXT_NODE_TYPE_TOPIC",
                "RICH_TEXT_NODE_TYPE_AT",
            }:
                jump_url = _safe_http_url(node.get("jump_url"))
                if node_text and jump_url:
                    replacement = (
                        f'<a href="{html.escape(jump_url, quote=True)}">'
                        f"{html.escape(node_text)}</a>"
                    )
                    rendered = rendered.replace(html.escape(node_text), replacement)

    if isinstance(topic, dict):
        topic_name = _clean_text(topic.get("name"))
        topic_url = _safe_http_url(topic.get("jump_url"))
        if topic_name and topic_url:
            topic_label = topic_name if topic_name.startswith("#") else f"#{topic_name}"
            escaped_label = html.escape(topic_label)
            if escaped_label not in rendered:
                rendered = (
                    f'<a href="{html.escape(topic_url, quote=True)}">'
                    f"{escaped_label}</a><br>{rendered}"
                )
    return rendered


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
    """按动态类型提取出的渲染字段。"""

    title: str = ""
    content: str = ""
    content_html: str = ""
    images: tuple[str, ...] = ()
    video_cover: str = ""
    video_url: str = ""


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


def _extract_images(item: dict[str, Any], dynamic_type: str) -> tuple[str, ...]:
    major = _value(item, "modules", "module_dynamic", "major", default={})
    images: list[str] = []
    if dynamic_type == "DYNAMIC_TYPE_DRAW":
        draw_items = _value(major, "draw", "items", default=[])
        if isinstance(draw_items, list):
            images.extend(
                url
                for entry in draw_items
                if isinstance(entry, dict)
                for url in [_safe_http_url(entry.get("src"))]
                if url
            )
    opus_pics = _value(major, "opus", "pics", default=[])
    if isinstance(opus_pics, list):
        images.extend(
            url
            for entry in opus_pics
            if isinstance(entry, dict)
            for url in [_safe_http_url(entry.get("url") or entry.get("src"))]
            if url
        )
    return tuple(dict.fromkeys(images))[:9]


def _parse_video_parts(
    dynamic: Any, major: Any, topic: Any
) -> _DynamicParts:
    """视频动态使用 archive，正文仍来自动态 desc。"""
    archive = _value(major, "archive", default={})
    title = content_to_text(_value(archive, "title"))
    video_cover = _safe_http_url(_value(archive, "cover"))
    jump_url = _safe_http_url(_value(archive, "jump_url"))
    bvid = str(_value(archive, "bvid") or "")
    video_url = jump_url or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")

    desc = _value(dynamic, "desc", default={})
    content = content_to_text(desc)
    content_html = content_to_html(desc, topic)
    archive_desc_value = _value(archive, "desc")
    archive_desc = content_to_text(archive_desc_value)
    if archive_desc and archive_desc not in content:
        content = f"{content}\n{archive_desc}".strip()
        archive_html = content_to_html(archive_desc_value)
        content_html = "<br>".join(
            part for part in (content_html, archive_html) if part
        )
    return _DynamicParts(
        title=title,
        content=content,
        content_html=content_html,
        video_cover=video_cover,
        video_url=video_url,
    )


def _parse_opus_parts(
    item: dict[str, Any],
    dynamic: Any,
    dynamic_type: str,
    major: Any,
    topic: Any,
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
    return _DynamicParts(
        title=title,
        content=content_to_text(content_source),
        content_html=content_to_html(content_source, topic),
        images=_extract_images(item, dynamic_type),
    )


def _parse_forward_parts(dynamic: Any, topic: Any) -> _DynamicParts:
    """转发动态自身只承载转发评论，原动态由 parse_dynamic 递归解析。"""
    desc = _value(dynamic, "desc", default={})
    return _DynamicParts(
        content=content_to_text(desc),
        content_html=content_to_html(desc, topic),
    )


def parse_dynamic(item: dict[str, Any], uid: int) -> BiliDynamicViewModel | None:
    dynamic_id = str(item.get("id_str") or item.get("id") or "")
    if not dynamic_id:
        return None
    dynamic_type = str(item.get("type") or "DYNAMIC_TYPE_UNKNOWN")
    if dynamic_type not in TYPE_NAMES:
        return None
    author = _value(item, "modules", "module_author", default={})
    dynamic = _value(item, "modules", "module_dynamic", default={})
    topic = _value(dynamic, "topic", default={})
    major = _value(dynamic, "major", default={})

    if dynamic_type == "DYNAMIC_TYPE_AV":
        parts = _parse_video_parts(dynamic, major, topic)
    elif dynamic_type in {
        "DYNAMIC_TYPE_ARTICLE",
        "DYNAMIC_TYPE_DRAW",
        "DYNAMIC_TYPE_WORD",
    }:
        parts = _parse_opus_parts(item, dynamic, dynamic_type, major, topic)
    else:
        parts = _parse_forward_parts(dynamic, topic)

    author_name = _clean_text(_value(author, "name"))
    avatar_url = _safe_http_url(_value(author, "face"))
    pendant_url = _safe_http_url(_value(author, "pendant", "image"))
    published_at = _parse_datetime(_value(author, "pub_ts")) or _parse_datetime(
        _value(author, "pub_time")
    )
    source_url = f"https://t.bilibili.com/{dynamic_id}"

    forward_content: dict[str, Any] | None = None
    if dynamic_type == "DYNAMIC_TYPE_FORWARD":
        original = item.get("orig")
        if isinstance(original, dict):
            original_model = parse_dynamic(original, uid)
            if original_model:
                forward_content = {
                    "dynamic_type": original_model.dynamic_type,
                    "author_name": original_model.author_name,
                    "avatar_url": original_model.avatar_url,
                    "pendant_url": original_model.pendant_url,
                    "title": original_model.title,
                    "content": original_model.content,
                    "content_html": original_model.content_html,
                    "images": list(original_model.images),
                    "source_url": original_model.source_url,
                }

    return BiliDynamicViewModel(
        dynamic_id=dynamic_id,
        dynamic_type=dynamic_type,
        uid=uid,
        author_name=author_name,
        avatar_url=avatar_url,
        publish_time=published_at,
        title=parts.title,
        content=parts.content,
        content_html=parts.content_html,
        images=parts.images,
        video_cover=parts.video_cover,
        video_url=parts.video_url,
        pendant_url=pendant_url,
        forward_content=forward_content,
        source_url=source_url,
        raw_payload=item,
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
    publish_time_text = (
        model.publish_time.astimezone().strftime("%Y年%m月%d日 %H:%M")
        if model.publish_time
        else ""
    )
    payload = {
        "dynamic_id": model.dynamic_id,
        "dynamic_type": model.dynamic_type,
        "dynamic_type_name": TYPE_NAMES.get(model.dynamic_type, "未知"),
        "uid": model.uid,
        "author_name": model.author_name,
        "avatar_url": model.avatar_url,
        "pendant_url": model.pendant_url,
        "publish_time": model.publish_time.isoformat() if model.publish_time else "",
        "publish_time_text": publish_time_text,
        "title": model.title,
        "content": model.content,
        "content_html": model.content_html,
        "images": list(model.images),
        "video_cover": model.video_cover,
        "video_url": model.video_url,
        "forward_content": model.forward_content,
        "source_url": model.source_url,
    }
    paragraphs = "</p><p>".join(
        html.escape(line) for line in model.content.splitlines() if line.strip()
    )
    normalized_html = model.content_html or (f"<p>{paragraphs}</p>" if paragraphs else "")
    return NormalizedContent(
        source_type=SourceType.BILIBILI,
        source_key=str(model.uid),
        external_id=model.dynamic_id,
        title=model.title or f"{TYPE_NAMES.get(model.dynamic_type, '动态')}动态",
        author=model.author_name,
        category=TYPE_NAMES.get(model.dynamic_type, "动态"),
        content_text=model.content,
        content_html=normalized_html,
        source_url=model.source_url,
        published_at=model.publish_time,
        render_payload=payload,
        raw_payload=model.raw_payload,
    )
