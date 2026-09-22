from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from yysls_news.delivery.qqbot.client import (
    QQBotApiError,
    QQBotClient,
    QQMessageResult,
    QQTarget,
)
from yysls_news.domain.models import MessageMode, SourceType
from yysls_news.rendering.renderer import ImageRenderError, PlaywrightRenderer
from yysls_news.services.runtime_config import RuntimeConfigService
from yysls_news.storage.repositories import DeliveryTaskRepository

LOGGER = logging.getLogger(__name__)


class DeliveryWorker:
    """消费 Outbox 任务，负责渲染、上传和发送。"""

    def __init__(
        self,
        tasks: DeliveryTaskRepository,
        runtime_config: RuntimeConfigService,
        image_renderer: PlaywrightRenderer,
        output_dir: str | Path = "./data/screenshots",
        max_attempts: int = 5,
    ) -> None:
        self.tasks = tasks
        self.runtime_config = runtime_config
        self.image_renderer = image_renderer
        self.output_dir = Path(output_dir)
        self.max_attempts = max(1, max_attempts)
        self._client: QQBotClient | None = None
        self._client_signature: tuple[str, str, str] | None = None

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
            self._client_signature = None

    async def process_pending(self, limit: int = 10) -> int:
        try:
            client = await self._get_client()
        except Exception as exc:
            # 未配置 QQBot 时保留 pending，等管理员完成配置后自动继续。
            LOGGER.info("暂不消费推送队列: %s", type(exc).__name__)
            return 0

        processed = 0
        for task in self.tasks.list_pending(limit):
            if not self.tasks.mark_processing(int(task["id"])):
                continue
            processed += 1
            try:
                result = await self._send_task(client, task)
                self.tasks.mark_sent(int(task["id"]), result.message_id, result.trace_id)
            except Exception as exc:
                self._record_failure(task, exc)
        return processed

    async def send_test(
        self,
        content: dict[str, Any],
        target: dict[str, Any],
    ) -> QQMessageResult:
        """直接发送一条历史内容测试消息，不创建正式推送任务。"""
        client = await self._get_client()
        task = dict(content)
        task.update(
            {
                "id": f"test-{uuid.uuid4().hex}",
                "scene_type": target["scene_type"],
                "target_openid": target["target_openid"],
                "message_mode": target.get("message_mode") or MessageMode.IMAGE.value,
                "render_mode": target.get("render_mode") or "playwright",
            }
        )
        return await self._send_task(client, task)

    async def run_forever(self, stop_event: Any = None) -> None:
        import asyncio

        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.process_pending()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                continue

    async def _get_client(self) -> QQBotClient:
        config = self.runtime_config.qqbot()
        if not config.app_id or not config.app_secret:
            raise RuntimeError("未配置 QQBot AppID 或 AppSecret")
        signature = (config.base_url, config.app_id, config.app_secret)
        if self._client and signature != self._client_signature:
            await self._client.aclose()
            self._client = None
        if not self._client:
            self._client = QQBotClient(
                base_url=config.base_url,
                app_id=config.app_id,
                app_secret=config.app_secret,
            )
            self._client_signature = signature
        return self._client

    async def _send_task(self, client: QQBotClient, task: dict[str, Any]):
        target = QQTarget(
            scene=str(task["scene_type"]),
            openid=str(task["target_openid"]),
        )
        mode = MessageMode(str(task.get("message_mode") or MessageMode.IMAGE.value))
        if mode is MessageMode.IMAGE:
            try:
                return await self._send_image(client, target, task)
            except ImageRenderError:
                LOGGER.warning("任务 %s 图片渲染失败，回退 Markdown", task.get("id", "test"))
                return await self._send_markdown_or_text(client, target, task)
        if mode is MessageMode.MARKDOWN:
            return await self._send_markdown_or_text(client, target, task)
        return await client.send_text(target, _plain_text(task))

    async def _send_image(self, client: QQBotClient, target: QQTarget, task: dict[str, Any]):
        payload = _payload(task)
        output = self.output_dir / f"task-{task.get('id', uuid.uuid4().hex)}.png"
        source_type = str(task["source_type"])
        if source_type == SourceType.BILIBILI.value:
            output = await self.image_renderer.render_bilibili(payload, output)
        else:
            output = await self.image_renderer.render_yysls(payload, output)
        return await client.send_image(target, output)

    async def _send_markdown_or_text(
        self, client: QQBotClient, target: QQTarget, task: dict[str, Any]
    ):
        try:
            return await client.send_markdown(target, _markdown(task))
        except QQBotApiError:
            return await client.send_text(target, _plain_text(task))

    def _record_failure(self, task: dict[str, Any], exc: Exception) -> None:
        retry_count = int(task.get("retry_count") or 0) + 1
        message = f"{type(exc).__name__}: {exc}"[:500]
        retryable = not isinstance(exc, QQBotApiError) or exc.retryable
        if retryable and retry_count < self.max_attempts:
            self.tasks.mark_retry(
                int(task["id"]),
                retry_count,
                _retry_at(retry_count),
                message,
            )
        else:
            self.tasks.mark_failed(int(task["id"]), retry_count, message)
        LOGGER.warning("推送任务 %s 失败: %s", task["id"], str(exc)[:500])


def _payload(task: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(task.get("render_payload_json") or "{}"))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _markdown(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "资讯更新")
    content = str(task.get("content_text") or "")
    source_url = str(task.get("source_url") or "")
    return f"## {title}\n\n{content}\n\n[查看原文]({source_url})"


def _plain_text(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "资讯更新")
    content = str(task.get("content_text") or "")
    source_url = str(task.get("source_url") or "")
    return f"{title}\n\n{content}\n\n原文：{source_url}".strip()


def _retry_at(retry_count: int) -> str:
    delays = (60, 300, 1800, 7200)
    delay = delays[min(max(retry_count - 1, 0), len(delays) - 1)]
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
