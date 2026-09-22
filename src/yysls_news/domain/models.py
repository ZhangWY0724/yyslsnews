from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SourceType(str, Enum):
    BILIBILI = "bilibili"
    YYSLS = "yysls"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SENT = "sent"
    RETRY = "retry"
    FAILED = "failed"


class SceneType(str, Enum):
    GROUP = "group"
    USER = "user"


class MessageMode(str, Enum):
    IMAGE = "image"
    MARKDOWN = "markdown"
    TEXT = "text"


@dataclass(frozen=True)
class BiliDynamicViewModel:
    dynamic_id: str
    dynamic_type: str
    uid: int
    author_name: str = ""
    avatar_url: str = ""
    publish_time: datetime | None = None
    title: str = ""
    content: str = ""
    content_html: str = ""
    images: tuple[str, ...] = ()
    video_cover: str = ""
    video_url: str = ""
    pendant_url: str = ""
    forward_content: dict[str, Any] | None = None
    source_url: str = ""
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedContent:
    source_type: SourceType
    source_key: str
    external_id: str
    title: str
    author: str
    category: str
    content_text: str
    content_html: str
    source_url: str
    published_at: datetime | None
    render_payload: dict[str, Any]
    raw_payload: dict[str, Any]


@dataclass(frozen=True)
class DeliveryTarget:
    id: int | None
    scene_type: SceneType
    target_openid: str
    enabled: bool = True
    message_mode: MessageMode = MessageMode.IMAGE
    render_mode: str = "playwright"


@dataclass(frozen=True)
class PollResult:
    source_key: str
    discovered: int = 0
    inserted: int = 0
    delivery_tasks: int = 0
    error: str = ""
