from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _int_env(name: str, default: int, minimum: int = 0) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return max(int(value), minimum)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc


@dataclass(frozen=True)
class Settings:
    database_path: Path
    encryption_key: str
    admin_username: str
    admin_password: str
    host: str
    port: int
    yysls_poll_interval_seconds: int
    http_timeout_seconds: float
    qqbot_api_base_url: str
    qqbot_app_id: str
    qqbot_app_secret: str

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> Settings:
        if env_file:
            load_dotenv(dotenv_path=env_file, override=False)

        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "./data/yysls_news.db")),
            encryption_key=os.getenv("APP_ENCRYPTION_KEY", ""),
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            host=os.getenv("HOST", "127.0.0.1"),
            port=_int_env("PORT", 43100, minimum=1),
            yysls_poll_interval_seconds=_int_env("YYSLS_POLL_INTERVAL_SECONDS", 600, minimum=60),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
            qqbot_api_base_url=os.getenv("QQBOT_API_BASE_URL", "https://api.bot.qq.com").rstrip(
                "/"
            ),
            qqbot_app_id=os.getenv("QQBOT_APP_ID", ""),
            qqbot_app_secret=os.getenv("QQBOT_APP_SECRET", ""),
        )
