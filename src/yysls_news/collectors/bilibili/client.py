from __future__ import annotations

from typing import Any


class BilibiliDependencyError(RuntimeError):
    """B站依赖不可用或未正确配置。"""


class BilibiliClient:
    """集中封装 bilibili-api-python，业务层不得直接调用第三方库。"""

    def __init__(self, credential: dict[str, Any] | None = None, proxy: str = "") -> None:
        try:
            from bilibili_api import Credential, request_settings
        except ImportError as exc:
            raise BilibiliDependencyError("未安装 bilibili-api-python，请先安装项目依赖") from exc

        self._credential_type = Credential
        self._request_settings = request_settings
        self.credential = self._build_credential(credential) if credential else None
        self.proxy = proxy.strip()
        if self.proxy:
            self._request_settings.set_proxy(self.proxy)

    def _build_credential(self, credential: dict[str, Any]) -> Any:
        allowed = {
            key: value
            for key, value in credential.items()
            if value not in (None, "")
            and key in {"sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"}
        }
        return self._credential_type(**allowed)

    def set_credential(self, credential: dict[str, Any]) -> None:
        self.credential = self._build_credential(credential)

    async def get_latest_dynamics(self, uid: int) -> dict[str, Any]:
        try:
            from bilibili_api import user

            instance = user.User(uid=uid, credential=self.credential)
            return await instance.get_dynamics_new()
        except Exception as exc:
            raise BilibiliDependencyError(f"获取 UID={uid} 动态失败: {type(exc).__name__}") from exc

    async def get_user_info(self, uid: int) -> dict[str, Any]:
        try:
            from bilibili_api import user

            instance = user.User(uid=uid, credential=self.credential)
            return await instance.get_user_info()
        except Exception as exc:
            raise BilibiliDependencyError(f"获取 UID={uid} 资料失败: {type(exc).__name__}") from exc
