from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yysls_news.delivery.qqbot.client import (
    QQBotApiError,
    QQBotClient,
    QQMessageResult,
    QQTarget,
)
from yysls_news.domain.models import MessageMode, SourceType
from yysls_news.rendering.renderer import ImageRenderError, PlaywrightRenderer
from yysls_news.services.runtime_config import RuntimeConfigService
from yysls_news.storage.repositories import (
    DeliveryTaskRepository,
    PushHistoryRepository,
    RenderArtifactRepository,
)

LOGGER = logging.getLogger(__name__)
RENDER_ARTIFACT_VERSION = 1


class DeliveryWorker:
    """消费 Outbox 任务，负责渲染、上传和发送。"""

    def __init__(
        self,
        tasks: DeliveryTaskRepository,
        push_history: PushHistoryRepository,
        runtime_config: RuntimeConfigService,
        image_renderer: PlaywrightRenderer,
        output_dir: str | Path = "./data/screenshots",
        max_attempts: int = 5,
    ) -> None:
        self.tasks = tasks
        self.artifacts = RenderArtifactRepository(tasks.database)
        self.push_history = push_history
        self.runtime_config = runtime_config
        self.image_renderer = image_renderer
        self.output_dir = Path(output_dir)
        self.max_attempts = max(1, max_attempts)
        self._client: QQBotClient | None = None
        self._client_signature: tuple[str, str, str] | None = None
        self._client_unavailable_logged = False

    async def aclose(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
            self._client_signature = None

    async def process_pending(self, limit: int = 10) -> int:
        pending = self.tasks.list_pending(limit)
        if not pending:
            return 0
        try:
            client = await self._get_client()
        except Exception as exc:
            # 未配置 QQBot 时保留 pending，等管理员完成配置后自动继续。
            if not self._client_unavailable_logged:
                LOGGER.info("推送队列暂停: due=%s error=%s", len(pending), type(exc).__name__)
            self._client_unavailable_logged = True
            return 0
        self._client_unavailable_logged = False
        LOGGER.info("推送队列开始处理: due=%s", len(pending))

        processed = 0
        for task in pending:
            if not self.tasks.mark_processing(int(task["id"])):
                continue
            processed += 1
            started = time.monotonic()
            LOGGER.info(
                "推送任务开始: task=%s content=%s target=%s source=%s scene=%s mode=%s attempt=%s",
                task["id"],
                task["content_item_id"],
                task["delivery_target_id"],
                task["source_type"],
                task["scene_type"],
                task["message_mode"],
                int(task.get("retry_count") or 0) + 1,
            )
            history_id = self.push_history.create_attempt(
                content_item_id=int(task["content_item_id"]),
                delivery_target_id=int(task["delivery_target_id"]),
                trigger_type="scheduled",
                source_type=str(task.get("source_type") or ""),
                title=str(task.get("title") or ""),
                scene_type=str(task["scene_type"]),
                target_openid=str(task["target_openid"]),
                target_display_name=str(task.get("target_display_name") or ""),
                attempt_number=int(task.get("retry_count") or 0) + 1,
            )
            try:
                result = await self._send_task(client, task)
                self.push_history.mark_sent(history_id, result.message_id, result.trace_id)
                self.tasks.mark_sent(int(task["id"]), result.message_id, result.trace_id)
                LOGGER.info(
                    "推送任务完成: task=%s messages=%s duration_ms=%s",
                    task["id"],
                    len(result.message_id.split(",")) if result.message_id else 0,
                    int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                message = self._record_failure(task, exc)
                self.push_history.mark_failed(history_id, message)
                LOGGER.warning(
                    "推送任务结束于失败: task=%s duration_ms=%s",
                    task["id"],
                    int((time.monotonic() - started) * 1000),
                )
            finally:
                self._cleanup_finished_artifact(int(task["content_item_id"]))
        return processed

    async def send_test(
        self,
        content: dict[str, Any],
        target: dict[str, Any],
    ) -> QQMessageResult:
        """直接发送一条历史内容测试消息，并记录推送结果。"""
        task = dict(content)
        task.update(
            {
                "id": f"test-{uuid.uuid4().hex}",
                "content_item_id": content.get("id"),
                "delivery_target_id": target.get("id"),
                "scene_type": target["scene_type"],
                "target_openid": target["target_openid"],
                "target_display_name": target.get("display_name") or "",
                "message_mode": target.get("message_mode") or MessageMode.IMAGE.value,
            }
        )
        history_id = self.push_history.create_attempt(
            content_item_id=_optional_int(task.get("content_item_id")),
            delivery_target_id=_optional_int(task.get("delivery_target_id")),
            trigger_type="historical_test",
            source_type=str(task.get("source_type") or ""),
            title=str(task.get("title") or ""),
            scene_type=str(task["scene_type"]),
            target_openid=str(task["target_openid"]),
            target_display_name=str(task.get("target_display_name") or ""),
        )
        started = time.monotonic()
        LOGGER.info(
            "历史测试推送开始: task=%s content=%s target=%s source=%s scene=%s mode=%s",
            task["id"],
            task.get("content_item_id"),
            task.get("delivery_target_id"),
            task.get("source_type"),
            task.get("scene_type"),
            task.get("message_mode"),
        )
        try:
            client = await self._get_client()
            result = await self._send_task(client, task)
            self.push_history.mark_sent(history_id, result.message_id, result.trace_id)
            LOGGER.info(
                "历史测试推送完成: task=%s messages=%s duration_ms=%s",
                task["id"],
                len(result.message_id.split(",")) if result.message_id else 0,
                int((time.monotonic() - started) * 1000),
            )
            return result
        except Exception as exc:
            self.push_history.mark_failed(history_id, _failure_message(exc))
            LOGGER.warning(
                "历史测试推送失败: task=%s error=%s duration_ms=%s",
                task["id"],
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            raise

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
            LOGGER.info("QQBot 推送客户端已就绪")
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
            except Exception as exc:
                detail = (
                    f"HTTP={exc.http_status} err_code={exc.err_code} trace_id={exc.trace_id or '-'}"
                    if isinstance(exc, QQBotApiError)
                    else str(exc)[:200]
                )
                LOGGER.warning(
                    "任务 %s 图片推送失败，改发原文链接: %s: %s",
                    task.get("id", "test"),
                    type(exc).__name__,
                    detail,
                )
                result = await client.send_text(target, _plain_text(task))
                LOGGER.info(
                    "任务文本兜底发送成功: task=%s message_id_tail=%s",
                    task.get("id", "test"),
                    result.message_id[-8:] or "-",
                )
                return result
        if mode is MessageMode.MARKDOWN:
            return await self._send_markdown_or_text(client, target, task)
        return await client.send_text(target, _plain_text(task))

    async def _send_image(self, client: QQBotClient, target: QQTarget, task: dict[str, Any]):
        shared = isinstance(task.get("id"), int) and task.get("content_item_id") is not None
        output_name = (
            f"content-{task['content_item_id']}-v{RENDER_ARTIFACT_VERSION}"
            if shared
            else f"task-{task.get('id', uuid.uuid4().hex)}"
        )
        output = self.output_dir / f"{output_name}.png"
        source_type = str(task["source_type"])
        task_id = task.get("id", "test")
        started = time.monotonic()
        LOGGER.info("任务图片准备开始: task=%s source=%s", task_id, source_type)
        image_path = (
            output.with_suffix(".jpg")
            if source_type == SourceType.BILIBILI.value
            else output
        )
        image_paths: list[Path] = []
        try:
            if shared:
                image_paths = await self._get_or_render_shared(task, image_path)
            else:
                image_paths = await self._render_pages(task, image_path)
            LOGGER.info(
                "任务图片已就绪: task=%s pages=%s bytes=%s duration_ms=%s",
                task_id,
                len(image_paths),
                sum(path.stat().st_size for path in image_paths),
                int((time.monotonic() - started) * 1000),
            )
            if len(image_paths) == 1:
                LOGGER.info("任务图片发送开始: task=%s page=1/1", task_id)
                result = await client.send_image(target, image_paths[0])
                LOGGER.info(
                    "任务图片发送完成: task=%s page=1/1 message_id_tail=%s",
                    task_id,
                    result.message_id[-8:] or "-",
                )
                return result

            # 先完成全部分页上传，避免上传失败后只发出前几页。
            file_infos: list[str] = []
            for index, path in enumerate(image_paths, start=1):
                LOGGER.info(
                    "任务图片分页上传开始: task=%s page=%s/%s bytes=%s",
                    task_id,
                    index,
                    len(image_paths),
                    path.stat().st_size,
                )
                file_infos.append(await client.upload_image(target, path))
                LOGGER.info(
                    "任务图片分页上传完成: task=%s page=%s/%s",
                    task_id,
                    index,
                    len(image_paths),
                )
            results: list[QQMessageResult] = []
            for index, info in enumerate(file_infos, start=1):
                LOGGER.info(
                    "任务图片分页发送开始: task=%s page=%s/%s",
                    task_id,
                    index,
                    len(file_infos),
                )
                result = await client.send_uploaded_image(target, info)
                results.append(result)
                LOGGER.info(
                    "任务图片分页发送完成: task=%s page=%s/%s message_id_tail=%s",
                    task_id,
                    index,
                    len(file_infos),
                    result.message_id[-8:] or "-",
                )
            return QQMessageResult(
                message_id=",".join(result.message_id for result in results if result.message_id),
                timestamp=results[-1].timestamp,
                trace_id=",".join(result.trace_id for result in results if result.trace_id),
            )
        finally:
            if not shared:
                self._cleanup_render_files(image_path)

    async def _render_pages(self, task: dict[str, Any], output: Path) -> list[Path]:
        source_type = str(task["source_type"])
        source_url = _source_link(task)
        if source_type == SourceType.BILIBILI.value:
            if str(task.get("category") or "") == "视频":
                original = await self.image_renderer.render_bilibili_video(
                    title=str(task.get("title") or ""),
                    author=str(task.get("author") or ""),
                    body_text=str(task.get("body_text") or ""),
                    cover_url=str(task.get("video_cover_url") or ""),
                    video_url=str(task.get("video_url") or source_url),
                    output_path=output,
                )
            else:
                original = await self.image_renderer.render_bilibili(source_url, output)
            pages = self.image_renderer.paginate_bilibili_image(original)
            if len(pages) > 1:
                original.unlink(missing_ok=True)
            return pages
        return await self.image_renderer.render_yysls(source_url, output)

    async def _get_or_render_shared(self, task: dict[str, Any], output: Path) -> list[Path]:
        content_id = int(task["content_item_id"])
        for _ in range(1200):
            state = self.artifacts.claim(content_id, RENDER_ARTIFACT_VERSION)
            if state["status"] == "ready":
                names = state["pages"]
                pages = [self.output_dir / str(name) for name in names]
                if all(
                    path.parent == self.output_dir
                    and path.is_file()
                    and path.stat().st_size > 0
                    for path in pages
                ):
                    LOGGER.info("内容图片复用: content=%s pages=%s", content_id, len(pages))
                    return pages
                self.artifacts.invalidate(content_id, RENDER_ARTIFACT_VERSION)
                continue
            if state["status"] == "failed":
                raise ImageRenderError(str(state.get("error") or "内容图片渲染失败"))
            if state["status"] == "claimed":
                self._cleanup_render_files(output)
                started = time.monotonic()
                LOGGER.info("内容图片渲染开始: content=%s", content_id)
                try:
                    pages = await self._render_pages(task, output)
                    if not pages:
                        raise ImageRenderError("内容截图没有生成图片")
                    self.artifacts.mark_ready(
                        content_id, RENDER_ARTIFACT_VERSION, [page.name for page in pages]
                    )
                except Exception as exc:
                    self._cleanup_render_files(output)
                    self.artifacts.mark_failed(
                        content_id, RENDER_ARTIFACT_VERSION, _failure_message(exc)
                    )
                    raise
                LOGGER.info(
                    "内容图片渲染完成: content=%s pages=%s duration_ms=%s",
                    content_id,
                    len(pages),
                    int((time.monotonic() - started) * 1000),
                )
                return pages
            await asyncio.sleep(0.5)
        raise ImageRenderError("等待共享截图生成超时")

    def _cleanup_finished_artifact(self, content_id: int) -> None:
        try:
            names = self.artifacts.cleanup_if_finished(content_id)
        except Exception as exc:
            LOGGER.warning(
                "共享截图清理状态检查失败: content=%s error=%s",
                content_id,
                type(exc).__name__,
            )
            return
        for name in names:
            if Path(name).name != name:
                LOGGER.warning("共享截图文件名无效: content=%s", content_id)
                continue
            try:
                (self.output_dir / name).unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning(
                    "共享截图清理失败: content=%s error=%s",
                    content_id,
                    type(exc).__name__,
                )
        if names:
            LOGGER.info("内容截图清理完成: content=%s pages=%s", content_id, len(names))

    @staticmethod
    def _cleanup_render_files(output: Path) -> None:
        files = [output, *output.parent.glob(f"{output.stem}-part-*{output.suffix}")]
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                LOGGER.warning("临时截图清理失败: error=%s", type(exc).__name__)

    async def _send_markdown_or_text(
        self, client: QQBotClient, target: QQTarget, task: dict[str, Any]
    ):
        try:
            return await client.send_markdown(target, _markdown(task))
        except QQBotApiError as exc:
            LOGGER.warning(
                "任务 Markdown 发送失败，改发文本: task=%s HTTP=%s err_code=%s trace_id=%s",
                task.get("id", "test"),
                exc.http_status,
                exc.err_code,
                exc.trace_id or "-",
            )
            return await client.send_text(target, _plain_text(task))

    def _record_failure(self, task: dict[str, Any], exc: Exception) -> str:
        retry_count = int(task.get("retry_count") or 0) + 1
        message = _failure_message(exc)
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
        LOGGER.warning(
            "推送任务失败: task=%s error=%s HTTP=%s err_code=%s "
            "trace_id=%s retryable=%s attempt=%s",
            task["id"],
            type(exc).__name__,
            exc.http_status if isinstance(exc, QQBotApiError) else None,
            exc.err_code if isinstance(exc, QQBotApiError) else None,
            exc.trace_id if isinstance(exc, QQBotApiError) else "-",
            retryable,
            retry_count,
        )
        return message


def _failure_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _markdown(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "资讯更新")
    source_url = _source_link(task)
    return f"## {title}已更新\n\n[点击查看]({source_url})"


def _plain_text(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "资讯更新")
    source_url = _source_link(task)
    return f"{title}已更新，点击查看：\n{source_url}"


def _source_link(task: dict[str, Any]) -> str:
    source_url = str(task.get("source_url") or "")
    if str(task.get("source_type") or "") != SourceType.BILIBILI.value:
        return source_url
    if str(task.get("category") or "") == "视频" and task.get("video_url"):
        return str(task["video_url"])
    dynamic_id = str(task.get("external_id") or "")
    if str(task.get("category") or "") == "图文" and dynamic_id.isascii():
        try:
            hostname = urlsplit(source_url).hostname
        except ValueError:
            hostname = ""
        if dynamic_id.isdigit() and hostname == "t.bilibili.com":
            return f"https://www.bilibili.com/opus/{dynamic_id}"
    return source_url


def _retry_at(retry_count: int) -> str:
    delays = (60, 300, 1800, 7200)
    delay = delays[min(max(retry_count - 1, 0), len(delays) - 1)]
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
