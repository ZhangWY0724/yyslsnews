from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretConfigurationError(RuntimeError):
    """应用加密密钥未配置或无效。"""


class SecretBox:
    """使用 Fernet 加密 JSON 凭据。"""

    def __init__(self, key: str) -> None:
        if not key:
            raise SecretConfigurationError("未配置 APP_ENCRYPTION_KEY")
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise SecretConfigurationError("APP_ENCRYPTION_KEY 不是有效的 Fernet 密钥") from exc

    def encrypt(self, value: dict[str, Any]) -> str:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, value: str) -> dict[str, Any]:
        try:
            payload = self._fernet.decrypt(value.encode("ascii"))
            result = json.loads(payload.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise SecretConfigurationError("凭据解密失败") from exc
        if not isinstance(result, dict):
            raise SecretConfigurationError("解密后的凭据不是对象")
        return result
