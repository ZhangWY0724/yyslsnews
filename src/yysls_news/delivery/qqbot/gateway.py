from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import websockets

from yysls_news.delivery.qqbot.client import (
    AccessTokenManager,
    QQBotApiError,
    QQBotClient,
    QQTarget,
)
from yysls_news.domain.models import SceneType
from yysls_news.services.qq_binding import QQBindingService
from yysls_news.services.runtime_config import QQBotConfig, RuntimeConfigService
from yysls_news.storage.database import utc_now

LOGGER = logging.getLogger(__name__)

GATEWAY_PATH = "/gateway"
GROUP_AND_C2C_EVENT_INTENT = 1 << 25


class QQBotGatewayError(RuntimeError):
    """QQ Bot Gateway 连接或协议错误。"""


class QQBotGatewayListener:
    """接收 QQ 单聊和群聊 @ 事件，用于自动绑定推送目标。"""

    def __init__(
        self,
        runtime_config: RuntimeConfigService,
        bindings: QQBindingService,
        timeout_seconds: float = 20,
    ) -> None:
        self.runtime_config = runtime_config
        self.bindings = bindings
        self.timeout_seconds = timeout_seconds
        self._closed = False
        self._socket: Any = None
        self._status: dict[str, str] = {
            "status": "not_started",
            "last_error": "",
            "connected_at": "",
            "last_event_at": "",
        }

    def status(self) -> dict[str, str]:
        return dict(self._status)

    async def aclose(self) -> None:
        self._closed = True
        socket = self._socket
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                LOGGER.debug("关闭 QQ Gateway 连接失败", exc_info=True)
        self._set_status("stopped")

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        self._closed = False
        backoff_seconds = 5.0
        while not stop_event.is_set() and not self._closed:
            try:
                config = self.runtime_config.qqbot()
            except Exception as exc:
                self._set_status("error", f"读取 QQBot 配置失败: {type(exc).__name__}")
                await _wait_for_stop(stop_event, backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60.0)
                continue

            if not config.app_id or not config.app_secret:
                self._set_status("not_configured")
                await _wait_for_stop(stop_event, 5.0)
                continue

            try:
                await self._run_connection(config, stop_event)
                backoff_seconds = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_status("error", f"QQ Gateway 连接失败: {type(exc).__name__}")
                LOGGER.warning("QQ Gateway 连接失败: %s", type(exc).__name__)
                await _wait_for_stop(stop_event, backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60.0)

    async def _run_connection(
        self,
        config: QQBotConfig,
        stop_event: asyncio.Event,
    ) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as http_client:
            token_manager = AccessTokenManager(
                http_client,
                config.base_url,
                config.app_id,
                config.app_secret,
            )
            gateway_url = await self._get_gateway_url(token_manager)
            async with websockets.connect(
                gateway_url,
                ping_interval=None,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
            ) as socket:
                self._socket = socket
                try:
                    hello = await _receive_json(socket)
                    if int(hello.get("op", -1)) != 10:
                        raise QQBotGatewayError("QQ Gateway 首包不是 Hello")
                    heartbeat_interval = int(
                        (hello.get("d") or {}).get("heartbeat_interval") or 45000
                    )
                    token = await token_manager.get()
                    await _send_json(
                        socket,
                        {
                            "op": 2,
                            "d": {
                                "token": f"QQBot {token}",
                                "intents": GROUP_AND_C2C_EVENT_INTENT,
                                "shard": [0, 1],
                                "properties": {
                                    "$os": "windows",
                                    "$browser": "yysls-news",
                                    "$device": "yysls-news",
                                },
                            },
                        },
                    )
                    self._set_status("connected")
                    sequence: dict[str, int | None] = {"value": None}
                    heartbeat_task = asyncio.create_task(
                        _heartbeat(socket, heartbeat_interval, sequence, stop_event)
                    )
                    try:
                        await self._receive_events(socket, stop_event, sequence, config)
                    finally:
                        heartbeat_task.cancel()
                        await asyncio.gather(heartbeat_task, return_exceptions=True)
                finally:
                    self._socket = None

    async def _get_gateway_url(self, token_manager: AccessTokenManager) -> str:
        token = await token_manager.get()
        try:
            response = await token_manager.client.get(
                f"{token_manager.base_url}{GATEWAY_PATH}",
                headers={"Authorization": f"QQBot {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise QQBotGatewayError("获取 QQ Gateway 地址失败") from exc
        url = str(payload.get("url") or "")
        if not url:
            raise QQBotGatewayError("QQ Gateway 响应缺少 url")
        return url

    async def _receive_events(
        self,
        socket: Any,
        stop_event: asyncio.Event,
        sequence: dict[str, int | None],
        config: QQBotConfig,
    ) -> None:
        while not stop_event.is_set() and not self._closed:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
            except TimeoutError:
                continue
            payload = _decode_payload(raw)
            if payload.get("s") is not None:
                sequence["value"] = int(payload["s"])
            opcode = int(payload.get("op", -1))
            if opcode == 7:
                raise QQBotGatewayError("QQ Gateway 要求重连")
            if opcode == 9:
                raise QQBotGatewayError("QQ Gateway 鉴权会话无效")
            if opcode == 1:
                await _send_json(socket, {"op": 1, "d": sequence["value"]})
            elif opcode == 0:
                await self._handle_dispatch(payload, config)

    async def _handle_dispatch(
        self,
        payload: dict[str, Any],
        config: QQBotConfig,
    ) -> None:
        event = _extract_binding_event(str(payload.get("t") or ""), payload.get("d") or {})
        if event is None:
            return
        scene_type, code, target_openid = event
        try:
            result = self.bindings.consume(scene_type, code, target_openid)
        except Exception:
            LOGGER.exception("处理 QQ 绑定事件失败")
            return
        if result is None:
            return
        self._status["last_event_at"] = utc_now()
        LOGGER.info("QQBot 绑定成功: scene=%s", result.scene_type.value)
        try:
            client = QQBotClient(
                config.base_url,
                config.app_id,
                config.app_secret,
                timeout_seconds=self.timeout_seconds,
            )
            try:
                await client.send_text(
                    QQTarget(result.scene_type.value, result.target_openid),
                    "绑定成功，后续资讯将推送到当前目标。",
                )
            finally:
                await client.aclose()
        except (QQBotApiError, httpx.HTTPError) as exc:
            LOGGER.warning("QQBot 绑定成功确认消息发送失败: %s", type(exc).__name__)

    def _set_status(self, value: str, error: str = "") -> None:
        self._status["status"] = value
        self._status["last_error"] = error
        if value == "connected":
            self._status["connected_at"] = utc_now()


def _extract_binding_event(
    event_name: str,
    data: dict[str, Any],
) -> tuple[SceneType, str, str] | None:
    content = str(data.get("content") or "").strip()
    if not content:
        return None
    if event_name == "C2C_MESSAGE_CREATE":
        author = data.get("author") or {}
        target_openid = str(author.get("user_openid") or "").strip()
        return (SceneType.USER, content, target_openid) if target_openid else None
    if event_name in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
        target_openid = str(data.get("group_openid") or "").strip()
        return (SceneType.GROUP, content, target_openid) if target_openid else None
    return None


async def _heartbeat(
    socket: Any,
    interval_ms: int,
    sequence: dict[str, int | None],
    stop_event: asyncio.Event,
) -> None:
    interval_seconds = max(interval_ms / 1000, 5.0)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            await _send_json(socket, {"op": 1, "d": sequence["value"]})


async def _send_json(socket: Any, payload: dict[str, Any]) -> None:
    await socket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


async def _receive_json(socket: Any) -> dict[str, Any]:
    return _decode_payload(await socket.recv())


def _decode_payload(raw: str | bytes) -> dict[str, Any]:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise QQBotGatewayError("QQ Gateway 返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise QQBotGatewayError("QQ Gateway 返回了无效消息")
    return payload


async def _wait_for_stop(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        pass
