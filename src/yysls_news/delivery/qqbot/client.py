from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

Scene = Literal["group", "user"]


@dataclass(frozen=True)
class QQTarget:
    scene: Scene
    openid: str

    @property
    def resource(self) -> str:
        return "groups" if self.scene == "group" else "users"


@dataclass(frozen=True)
class QQMessageResult:
    message_id: str
    timestamp: str
    trace_id: str = ""


class QQBotApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        err_code: int | None = None,
        trace_id: str = "",
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.err_code = err_code
        self.trace_id = trace_id

    @property
    def retryable(self) -> bool:
        return self.http_status in {408, 429, 500, 502, 503, 504} or self.err_code in {
            50055002,
            40093001,
            40093002,
        }


class AccessTokenManager:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        app_id: str,
        app_secret: str,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token and time.monotonic() < self._expires_at:
            return self._token
        async with self._lock:
            if not force_refresh and self._token and time.monotonic() < self._expires_at:
                return self._token
            if not self.app_id or not self.app_secret:
                raise QQBotApiError("未配置 QQBot AppID 或 AppSecret")
            try:
                response = await self.client.post(
                    f"{self.base_url}/app/getAppAccessToken",
                    json={"appId": self.app_id, "clientSecret": self.app_secret},
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPError as exc:
                raise QQBotApiError("获取 QQBot AccessToken 网络失败") from exc
            if not _is_zero(payload.get("code", 0)) or not payload.get("access_token"):
                raise QQBotApiError(
                    str(payload.get("message") or "获取 QQBot AccessToken 失败"),
                    http_status=response.status_code,
                    err_code=payload.get("code"),
                )
            self._token = str(payload["access_token"])
            expires_in = max(int(payload.get("expires_in", 7200)), 120)
            self._expires_at = time.monotonic() + expires_in - 60
            return self._token

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0


class QQBotClient:
    """QQ 官方 Bot OpenAPI v2 客户端。"""

    def __init__(
        self,
        base_url: str,
        app_id: str,
        app_secret: str,
        timeout_seconds: float = 20,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self.tokens = AccessTokenManager(self.client, base_url, app_id, app_secret)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def send_text(self, target: QQTarget, content: str) -> QQMessageResult:
        payload = await self._request(
            "POST",
            self._message_path(target),
            json={"msg_type": 0, "content": content},
        )
        return self._message_result(payload)

    async def send_markdown(self, target: QQTarget, content: str) -> QQMessageResult:
        payload = await self._request(
            "POST",
            self._message_path(target),
            json={"msg_type": 2, "markdown": {"content": content}},
        )
        return self._message_result(payload)

    async def send_image(self, target: QQTarget, file_path: str | Path) -> QQMessageResult:
        file_info = await self.upload_image(target, file_path)
        payload = await self._request(
            "POST",
            self._message_path(target),
            json={"msg_type": 7, "media": {"file_info": file_info}},
        )
        return self._message_result(payload)

    async def get_group_info(self, group_openid: str) -> dict[str, Any]:
        return await self._request("GET", f"/v2/groups/{group_openid}/info")

    async def upload_image(self, target: QQTarget, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise QQBotApiError(f"图片文件不存在: {path}")
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise QQBotApiError("QQBot 第一版图片上传只接受 PNG/JPG")

        raw = path.read_bytes()
        file_size = len(raw)
        prepare = await self._request(
            "POST",
            f"/v2/{target.resource}/{target.openid}/upload_prepare",
            json={
                "file_type": 1,
                "file_size": file_size,
                "file_name": path.name,
                "md5": hashlib.md5(raw).hexdigest(),
                "sha1": hashlib.sha1(raw).hexdigest(),
                "md5_10m": hashlib.md5(raw[: 10 * 1024 * 1024]).hexdigest(),
            },
        )
        upload_id = str(prepare.get("upload_id") or "")
        block_size = int(prepare.get("block_size") or 0)
        parts = prepare.get("parts") or []
        if not upload_id or block_size <= 0 or not isinstance(parts, list):
            raise QQBotApiError("QQBot 预上传响应缺少分片信息")

        for part in parts:
            index = int(part.get("index", 0))
            if index < 1:
                raise QQBotApiError("QQBot 预上传响应包含无效的分片序号")
            part_block_size = int(part.get("block_size") or block_size)
            start = (index - 1) * block_size
            chunk = raw[start : start + part_block_size]
            if not chunk and raw:
                raise QQBotApiError(f"QQBot 分片 {index} 超出文件范围")
            presigned_url = str(part.get("presigned_url") or "")
            if not presigned_url:
                raise QQBotApiError(f"QQBot 分片 {index} 缺少预签名 URL")
            try:
                response = await self.client.put(presigned_url, content=chunk)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise QQBotApiError(f"QQBot 分片 {index} 上传失败") from exc
            await self._request(
                "POST",
                f"/v2/{target.resource}/{target.openid}/upload_part_finish",
                json={
                    "upload_id": upload_id,
                    "part_index": index,
                    "block_size": len(chunk),
                    "md5": hashlib.md5(chunk).hexdigest(),
                },
            )

        completed = await self._request(
            "POST",
            f"/v2/{target.resource}/{target.openid}/files",
            json={"upload_id": upload_id},
        )
        file_info = str(completed.get("file_info") or "")
        if not file_info:
            raise QQBotApiError("QQBot 图片上传响应缺少 file_info")
        return file_info

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        token = await self.tokens.get()
        try:
            response = await self.client.request(
                method,
                f"{self.tokens.base_url}{path}",
                headers={"Authorization": f"QQBot {token}"},
                json=json,
            )
            payload = response.json() if response.content else {}
        except (httpx.HTTPError, ValueError) as exc:
            raise QQBotApiError("QQBot API 请求失败") from exc

        if response.status_code == 401 and retry_auth:
            self.tokens.invalidate()
            return await self._request(method, path, json=json, retry_auth=False)

        err_code = payload.get("err_code")
        if err_code is None:
            err_code = payload.get("code")
        if response.status_code >= 400 or (err_code is not None and not _is_zero(err_code)):
            message = str(
                payload.get("message")
                or payload.get("msg")
                or f"QQBot API 返回 HTTP {response.status_code}"
            )
            if err_code is not None:
                message = f"{message}（err_code={err_code}）"
            raise QQBotApiError(
                message,
                http_status=response.status_code,
                err_code=err_code,
                trace_id=str(payload.get("trace_id") or response.headers.get("X-Tps-trace-ID", "")),
            )
        return payload

    @staticmethod
    def _message_path(target: QQTarget) -> str:
        return f"/v2/{target.resource}/{target.openid}/messages"

    @staticmethod
    def _message_result(payload: dict[str, Any]) -> QQMessageResult:
        return QQMessageResult(
            message_id=str(payload.get("id") or ""),
            timestamp=str(payload.get("timestamp") or ""),
            trace_id=str(payload.get("trace_id") or ""),
        )


def _is_zero(value: Any) -> bool:
    return value in (0, "0", None, "")
