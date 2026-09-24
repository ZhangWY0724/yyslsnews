from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from yysls_news.collectors.bilibili.client import BilibiliClient
from yysls_news.collectors.bilibili.lifecycle import BilibiliCredentialLifecycle
from yysls_news.collectors.bilibili.parser import (
    extract_items,
    extract_new_items,
    matches_filters,
    parse_dynamic,
    to_normalized_content,
)
from yysls_news.collectors.yysls.client import YyslsClient
from yysls_news.domain.models import NormalizedContent, PollResult, SourceType
from yysls_news.services.credentials import CredentialService
from yysls_news.storage.repositories import (
    BilibiliSubscriptionRepository,
    ContentRepository,
    WatchSourceRepository,
)

LOGGER = logging.getLogger(__name__)


class BilibiliIngestionService:
    def __init__(
        self,
        client: BilibiliClient,
        subscriptions: BilibiliSubscriptionRepository,
        contents: ContentRepository,
    ) -> None:
        self.client = client
        self.subscriptions = subscriptions
        self.contents = contents

    async def poll(self, subscription: dict[str, Any]) -> PollResult:
        uid = int(subscription["uid"])
        source_key = str(uid)
        started = time.monotonic()
        LOGGER.info(
            "B站轮询开始: uid=%s cursor=%s",
            uid,
            subscription.get("last_dynamic_id") or "-",
        )
        try:
            raw = await self.client.get_latest_dynamics(uid)
            items = extract_new_items(
                raw,
                last_dynamic_id=str(subscription.get("last_dynamic_id") or ""),
                recent_dynamic_ids=subscription.get("recent_dynamic_ids") or [],
            )
            ids = _dynamic_ids(items)
            LOGGER.info(
                "B站结构化接口返回: uid=%s total=%s new=%s",
                uid,
                len(extract_items(raw)),
                len(items),
            )
            if not items:
                self.subscriptions.update_cursor(
                    uid,
                    str(subscription.get("last_dynamic_id") or ""),
                    list(subscription.get("recent_dynamic_ids") or []),
                    _next_poll(subscription["poll_interval_seconds"]),
                )
                LOGGER.info(
                    "B站轮询完成: uid=%s discovered=0 duration_ms=%s",
                    uid,
                    int((time.monotonic() - started) * 1000),
                )
                return PollResult(source_key=source_key)

            # 首次轮询只建立基线，不把已有历史动态推送出去。
            is_bootstrap = not subscription.get("last_dynamic_id") and not subscription.get(
                "recent_dynamic_ids"
            )
            if is_bootstrap:
                self.subscriptions.update_cursor(
                    uid,
                    ids[0] if ids else "",
                    _merge_recent(ids, subscription.get("recent_dynamic_ids") or []),
                    _next_poll(subscription["poll_interval_seconds"]),
                )
                LOGGER.info(
                    "B站订阅基线已建立: uid=%s discovered=%s duration_ms=%s",
                    uid,
                    len(items),
                    int((time.monotonic() - started) * 1000),
                )
                return PollResult(source_key=source_key, discovered=len(items))

            inserted = 0
            delivery_tasks = 0
            skipped = 0
            for item in reversed(items):
                model = parse_dynamic(item, uid)
                if not model or not matches_filters(
                    model,
                    subscription.get("filter_types") or [],
                    subscription.get("filter_keywords") or [],
                ):
                    skipped += 1
                    continue
                _, was_inserted, task_count = self.contents.insert_with_outbox(
                    to_normalized_content(model)
                )
                inserted += int(was_inserted)
                delivery_tasks += task_count

            self.subscriptions.update_cursor(
                uid,
                ids[0] if ids else str(subscription.get("last_dynamic_id") or ""),
                _merge_recent(ids, subscription.get("recent_dynamic_ids") or []),
                _next_poll(subscription["poll_interval_seconds"]),
            )
            LOGGER.info(
                "B站轮询完成: uid=%s discovered=%s inserted=%s tasks=%s skipped=%s duration_ms=%s",
                uid,
                len(items),
                inserted,
                delivery_tasks,
                skipped,
                int((time.monotonic() - started) * 1000),
            )
            return PollResult(
                source_key=source_key,
                discovered=len(items),
                inserted=inserted,
                delivery_tasks=delivery_tasks,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            LOGGER.warning(
                "B站轮询失败: uid=%s error=%s duration_ms=%s",
                uid,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            self.subscriptions.update_error(
                uid,
                message[:500],
                _next_poll(subscription["poll_interval_seconds"]),
            )
            return PollResult(source_key=source_key, error=message[:500])


class YyslsIngestionService:
    def __init__(
        self,
        client: YyslsClient,
        sources: WatchSourceRepository,
        contents: ContentRepository,
        max_articles_per_poll: int = 30,
    ) -> None:
        self.client = client
        self.sources = sources
        self.contents = contents
        self.max_articles_per_poll = max(1, max_articles_per_poll)

    async def poll(self, source: dict[str, Any]) -> PollResult:
        source_key = str(source["source_key"])
        started = time.monotonic()
        LOGGER.info(
            "官网轮询开始: source=%s category=%s bootstrap=%s",
            source_key,
            source.get("category") or "最新",
            not source.get("last_success_at"),
        )
        try:
            items = await self.client.discover(
                list_url=str(source["url"]),
                category=str(source.get("category") or "最新"),
            )
            items = items[: self.max_articles_per_poll]
            bootstrap = not source.get("last_success_at")
            discovered = 0
            inserted = 0
            delivery_tasks = 0
            errors: list[str] = []

            for list_item in items:
                if self.contents.exists(SourceType.YYSLS.value, source_key, list_item.url):
                    continue
                discovered += 1
                try:
                    content = NormalizedContent(
                        source_type=SourceType.YYSLS,
                        source_key=source_key,
                        external_id=list_item.url,
                        title=list_item.title,
                        author="燕云十六声官网",
                        category=list_item.category,
                        source_url=list_item.url,
                        published_at=list_item.published_at,
                    )
                    _, was_inserted, task_count = self.contents.insert_with_outbox(
                        content, create_tasks=not bootstrap
                    )
                    inserted += int(was_inserted)
                    delivery_tasks += task_count
                except Exception as exc:
                    errors.append(f"{list_item.url}: {type(exc).__name__}")

            error = "; ".join(errors)[:500]
            self.sources.update_poll_state(
                int(source["id"]),
                _next_poll(source["poll_interval_seconds"]),
                error,
            )
            LOGGER.info(
                "官网轮询完成: source=%s fetched=%s discovered=%s inserted=%s "
                "tasks=%s errors=%s duration_ms=%s",
                source_key,
                len(items),
                discovered,
                inserted,
                delivery_tasks,
                len(errors),
                int((time.monotonic() - started) * 1000),
            )
            return PollResult(
                source_key=source_key,
                discovered=discovered,
                inserted=inserted,
                delivery_tasks=delivery_tasks,
                error=error,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            LOGGER.warning(
                "官网轮询失败: source=%s error=%s duration_ms=%s",
                source_key,
                type(exc).__name__,
                int((time.monotonic() - started) * 1000),
            )
            self.sources.update_poll_state(
                int(source["id"]),
                _next_poll(source["poll_interval_seconds"]),
                message[:500],
            )
            return PollResult(source_key=source_key, error=message[:500])


class MonitorService:
    """按数据库中的来源状态执行一轮采集。"""

    def __init__(
        self,
        credentials: CredentialService | None,
        subscriptions: BilibiliSubscriptionRepository,
        sources: WatchSourceRepository,
        contents: ContentRepository,
        yysls_client: YyslsClient,
        timeout_seconds: float = 20,
        bili_proxy: str = "",
        credential_lifecycle: BilibiliCredentialLifecycle | None = None,
    ) -> None:
        self.credentials = credentials
        self.subscriptions = subscriptions
        self.sources = sources
        self.contents = contents
        self.yysls_client = yysls_client
        self.timeout_seconds = timeout_seconds
        self.bili_proxy = bili_proxy
        self.credential_lifecycle = credential_lifecycle
        self._bili_paused = False

    async def poll_once(self, force: bool = False) -> list[PollResult]:
        results: list[PollResult] = []
        now = datetime.now(timezone.utc)

        subscription_rows = self.subscriptions.list_enabled()
        credential = self.credentials.load() if self.credentials else None
        if credential and self.credential_lifecycle:
            if not await self.credential_lifecycle.ensure_valid():
                credential = None
        if subscription_rows and not credential:
            if not self._bili_paused:
                LOGGER.info(
                    "B站轮询暂停: enabled_subscriptions=%s 登录态不可用",
                    len(subscription_rows),
                )
            self._bili_paused = True
        elif self._bili_paused:
            if subscription_rows:
                LOGGER.info("B站轮询恢复: enabled_subscriptions=%s", len(subscription_rows))
            self._bili_paused = False
        if subscription_rows and credential:
            bili_client = BilibiliClient(credential=credential, proxy=self.bili_proxy)
            bili_service = BilibiliIngestionService(bili_client, self.subscriptions, self.contents)
            for row in subscription_rows:
                if force or _is_due(row.get("next_poll_at"), now):
                    results.append(await bili_service.poll(row))

        yysls_service = YyslsIngestionService(self.yysls_client, self.sources, self.contents)
        for row in self.sources.list_enabled(SourceType.YYSLS.value):
            if force or _is_due(row.get("next_poll_at"), now):
                results.append(await yysls_service.poll(row))
        return results

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue


def _dynamic_ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id_str") or item.get("id")) for item in items]


def _merge_recent(current: list[str], previous: list[str], limit: int = 64) -> list[str]:
    result: list[str] = []
    for value in [*current, *previous]:
        if value and value not in result:
            result.append(str(value))
    return result[:limit]


def _is_due(value: Any, now: datetime) -> bool:
    if not value:
        return True
    try:
        scheduled = datetime.fromisoformat(str(value))
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        return scheduled <= now
    except ValueError:
        return True


def _next_poll(interval_seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(int(interval_seconds), 60))
    ).isoformat()
