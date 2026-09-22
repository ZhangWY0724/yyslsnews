from __future__ import annotations

from typing import Any

from yysls_news.security.secrets import SecretBox
from yysls_news.storage.repositories import CredentialRepository


class CredentialService:
    """负责 B站 Credential 的加密持久化和脱敏状态查询。"""

    def __init__(self, repository: CredentialRepository, secret_box: SecretBox) -> None:
        self.repository = repository
        self.secret_box = secret_box

    def save(self, credential: dict[str, Any], status: str = "logged_in") -> None:
        self.repository.save(self.secret_box.encrypt(credential), status)

    def load(self) -> dict[str, Any] | None:
        record = self.repository.get()
        if not record or not record.get("encrypted_json"):
            return None
        return self.secret_box.decrypt(str(record["encrypted_json"]))

    def status(self) -> dict[str, Any]:
        record = self.repository.get()
        if not record:
            return {
                "status": "not_configured",
                "updated_at": "",
                "last_error": "",
                "username": "",
                "masked": {},
            }
        credential = {}
        try:
            credential = self.secret_box.decrypt(str(record["encrypted_json"]))
        except Exception:
            return {
                "status": "failed",
                "updated_at": str(record.get("updated_at") or ""),
                "last_error": "凭据无法解密，请检查 APP_ENCRYPTION_KEY",
                "username": "",
                "masked": {},
            }
        username = str(credential.get("username") or "")
        return {
            "status": str(record.get("status") or "failed"),
            "updated_at": str(record.get("updated_at") or ""),
            "last_error": str(record.get("last_error") or ""),
            "username": username,
            "masked": {
                key: _mask(value)
                for key, value in credential.items()
                if value and key != "username"
            },
        }

    def mark_status(self, status: str, error: str = "") -> None:
        self.repository.update_status(status, error)


def _mask(value: Any) -> str:
    text = str(value)
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}***{text[-4:]}"
