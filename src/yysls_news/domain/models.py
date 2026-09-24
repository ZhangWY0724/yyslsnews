from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


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
    publish_time: datetime | None = None
    title: str = ""
    content: str = ""
    source_url: str = ""
    video_bvid: str = ""
    video_cover_url: str = ""
    video_url: str = ""


@dataclass(frozen=True)
class NormalizedContent:
    source_type: SourceType
    source_key: str
    external_id: str
    title: str
    author: str
    category: str
    source_url: str
    published_at: datetime | None
    body_text: str = ""
    video_cover_url: str = ""
    video_url: str = ""


@dataclass(frozen=True)
class PollResult:
    source_key: str
    discovered: int = 0
    inserted: int = 0
    delivery_tasks: int = 0
    error: str = ""
