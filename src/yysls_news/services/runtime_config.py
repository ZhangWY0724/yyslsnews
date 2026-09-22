from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from yysls_news.config import Settings
from yysls_news.security.secrets import SecretBox
from yysls_news.storage.repositories import AppSettingsRepository

QQBOT_CONFIG_KEY = "qqbot_config"
POLL_CONFIG_KEY = "poll_config"
ADMIN_PASSWORD_HASH_KEY = "admin_password_hash"


@dataclass(frozen=True)
class QQBotConfig:
    base_url: str
    app_id: str
    app_secret: str


class RuntimeConfigService:
    """读取环境默认值，并允许管理页面安全地覆盖可运行配置。"""

    def __init__(
        self,
        settings: Settings,
        repository: AppSettingsRepository,
        secret_box: SecretBox | None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.secret_box = secret_box

    def qqbot(self) -> QQBotConfig:
        encoded = self.repository.get(QQBOT_CONFIG_KEY)
        if encoded:
            if not self.secret_box:
                raise RuntimeError("数据库存在 QQBot 动态配置，但未配置 APP_ENCRYPTION_KEY")
            data = self.secret_box.decrypt(encoded)
            return QQBotConfig(
                base_url=str(data.get("base_url") or self.settings.qqbot_api_base_url).rstrip("/"),
                app_id=str(data.get("app_id") or ""),
                app_secret=str(data.get("app_secret") or ""),
            )
        return QQBotConfig(
            base_url=self.settings.qqbot_api_base_url,
            app_id=self.settings.qqbot_app_id,
            app_secret=self.settings.qqbot_app_secret,
        )

    def save_qqbot(
        self,
        app_id: str,
        app_secret: str,
        base_url: str | None = None,
    ) -> QQBotConfig:
        if not self.secret_box:
            raise RuntimeError("保存 QQBot 动态配置前必须配置 APP_ENCRYPTION_KEY")
        current = self.qqbot()
        config = QQBotConfig(
            base_url=(base_url or current.base_url).rstrip("/"),
            app_id=app_id.strip(),
            app_secret=app_secret.strip() or current.app_secret,
        )
        if not config.app_id or not config.app_secret:
            raise ValueError("AppID 和 AppSecret 不能为空")
        self.repository.set(QQBOT_CONFIG_KEY, self.secret_box.encrypt(asdict(config)))
        return config

    def qqbot_public(self) -> dict[str, str]:
        config = self.qqbot()
        return {
            "base_url": config.base_url,
            "app_id": config.app_id,
            "app_secret": _mask(config.app_secret),
            "configured": bool(config.app_id and config.app_secret),
        }

    def poll_defaults(self) -> dict[str, int]:
        raw = self.repository.get(POLL_CONFIG_KEY)
        if raw:
            try:
                data = json.loads(raw)
                return {
                    "bilibili_poll_interval_seconds": max(
                        int(
                            data.get(
                                "bilibili_poll_interval_seconds",
                                self.settings.bilibili_poll_interval_seconds,
                            )
                        ),
                        60,
                    ),
                    "yysls_poll_interval_seconds": max(
                        int(
                            data.get(
                                "yysls_poll_interval_seconds",
                                self.settings.yysls_poll_interval_seconds,
                            )
                        ),
                        60,
                    ),
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return {
            "bilibili_poll_interval_seconds": self.settings.bilibili_poll_interval_seconds,
            "yysls_poll_interval_seconds": self.settings.yysls_poll_interval_seconds,
        }

    def save_poll_defaults(self, bilibili_seconds: int, yysls_seconds: int) -> dict[str, int]:
        values = {
            "bilibili_poll_interval_seconds": max(int(bilibili_seconds), 60),
            "yysls_poll_interval_seconds": max(int(yysls_seconds), 60),
        }
        self.repository.set(POLL_CONFIG_KEY, json.dumps(values, separators=(",", ":")))
        return values

    def admin_password_hash(self) -> str:
        return self.repository.get(ADMIN_PASSWORD_HASH_KEY)

    def save_admin_password_hash(self, password_hash: str) -> None:
        self.repository.set(ADMIN_PASSWORD_HASH_KEY, password_hash)


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"
