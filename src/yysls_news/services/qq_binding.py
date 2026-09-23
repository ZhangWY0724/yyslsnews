from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yysls_news.domain.models import MessageMode, SceneType
from yysls_news.storage.repositories import DeliveryTargetRepository, QQBindingRepository

BINDING_CODE_TTL_SECONDS = 180
BINDING_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


@dataclass(frozen=True)
class QQBindingCode:
    id: int
    code: str
    scene_type: SceneType
    message_mode: MessageMode
    expires_at: str


@dataclass(frozen=True)
class QQBindingResult:
    id: int
    scene_type: SceneType
    message_mode: MessageMode
    target_openid: str


class QQBindingService:
    """生成一次性绑定码，并把 QQ 事件中的目标写入推送目标表。"""

    def __init__(
        self,
        repository: QQBindingRepository,
        targets: DeliveryTargetRepository,
    ) -> None:
        self.repository = repository
        self.targets = targets

    def create(
        self,
        scene_type: SceneType,
        message_mode: MessageMode = MessageMode.IMAGE,
    ) -> QQBindingCode:
        self.repository.expire_pending()
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=BINDING_CODE_TTL_SECONDS)
        ).isoformat()
        for _ in range(10):
            code = "".join(secrets.choice(BINDING_CODE_ALPHABET) for _ in range(8))
            try:
                binding_id = self.repository.create(
                    _hash_code(code), scene_type, message_mode, expires_at
                )
            except sqlite3.IntegrityError:
                continue
            return QQBindingCode(binding_id, code, scene_type, message_mode, expires_at)
        raise RuntimeError("生成 QQBot 绑定码失败，请稍后重试")

    def get_status(self, binding_id: int) -> dict[str, Any] | None:
        row = self.repository.get(binding_id)
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "scene_type": str(row["scene_type"]),
            "message_mode": str(row["message_mode"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "expires_at": str(row["expires_at"]),
            "consumed_at": str(row.get("consumed_at") or ""),
        }

    def cancel(self, binding_id: int) -> bool:
        return self.repository.cancel(binding_id)

    def consume(
        self,
        scene_type: SceneType,
        code: str,
        target_openid: str,
        display_name: str = "",
    ) -> QQBindingResult | None:
        normalized_code = code.strip().upper()
        if len(normalized_code) != 8 or any(
            character not in BINDING_CODE_ALPHABET for character in normalized_code
        ):
            return None
        target_openid = target_openid.strip()
        if not target_openid:
            return None
        row = self.repository.consume(_hash_code(normalized_code), scene_type, target_openid)
        if row is None:
            return None
        message_mode = MessageMode(str(row["message_mode"]))
        self.targets.upsert(
            scene_type=scene_type,
            target_openid=target_openid,
            display_name=display_name,
            enabled=True,
            message_mode=message_mode,
        )
        return QQBindingResult(
            id=int(row["id"]),
            scene_type=scene_type,
            message_mode=message_mode,
            target_openid=target_openid,
        )

    def update_target_name(
        self, scene_type: SceneType, target_openid: str, display_name: str
    ) -> None:
        if display_name.strip():
            self.targets.update_display_name(scene_type, target_openid, display_name)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()
