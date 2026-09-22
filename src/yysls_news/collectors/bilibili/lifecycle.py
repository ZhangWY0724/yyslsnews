from __future__ import annotations

import logging
import time
from typing import Any

from yysls_news.services.credentials import CredentialService

LOGGER = logging.getLogger(__name__)


class BilibiliCredentialLifecycle:
    """低频检查并刷新 B站登录态，避免采集循环反复触发认证请求。"""

    def __init__(self, credentials: CredentialService, check_interval_seconds: int = 900) -> None:
        self.credentials = credentials
        self.check_interval_seconds = max(check_interval_seconds, 300)
        self._last_checked = 0.0

    async def ensure_valid(self, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self._last_checked < self.check_interval_seconds:
            return self.credentials.status().get("status") == "logged_in"
        self._last_checked = now
        data = self.credentials.load()
        if not data:
            return False
        try:
            from bilibili_api import Credential

            credential = Credential(**_allowed(data))
            if not await credential.check_valid():
                self.credentials.mark_status("relogin_required", "B站登录态已失效，请重新扫码")
                return False
            if await credential.check_refresh():
                await credential.refresh()
                refreshed = _credential_to_dict(credential)
                if data.get("username"):
                    refreshed["username"] = data["username"]
                self.credentials.save(refreshed)
            return True
        except Exception as exc:
            # 412、429 和网络异常均进入低频重试窗口，不在每次轮询中重复请求。
            self.credentials.mark_status("failed", f"登录态检查失败: {type(exc).__name__}")
            LOGGER.warning("B站登录态检查失败: %s", type(exc).__name__)
            return False


def _allowed(data: dict[str, Any]) -> dict[str, Any]:
    keys = {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"}
    return {key: value for key, value in data.items() if key in keys and value not in (None, "")}


def _credential_to_dict(credential: Any) -> dict[str, Any]:
    return {
        "sessdata": getattr(credential, "sessdata", ""),
        "bili_jct": getattr(credential, "bili_jct", ""),
        "buvid3": getattr(credential, "buvid3", ""),
        "buvid4": getattr(credential, "buvid4", ""),
        "dedeuserid": getattr(credential, "dedeuserid", ""),
        "ac_time_value": getattr(credential, "ac_time_value", ""),
    }
