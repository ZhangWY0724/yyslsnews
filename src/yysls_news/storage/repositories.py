from __future__ import annotations

import hashlib
from typing import Any

from yysls_news.domain.models import (
    DeliveryStatus,
    MessageMode,
    NormalizedContent,
    SceneType,
)
from yysls_news.storage.database import Database, utc_now


class AppSettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, key: str, default: str = "") -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set(self, key: str, value: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )


class CredentialRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def save(self, encrypted_json: str, status: str, last_error: str = "") -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO bilibili_credentials(id, status, encrypted_json, updated_at, last_error)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    encrypted_json=excluded.encrypted_json,
                    updated_at=excluded.updated_at,
                    last_error=excluded.last_error
                """,
                (status, encrypted_json, now, last_error),
            )

    def get(self) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status, encrypted_json, updated_at, last_error "
                "FROM bilibili_credentials WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    def update_status(self, status: str, last_error: str = "") -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE bilibili_credentials
                SET status = ?, last_error = ?, updated_at = ?
                WHERE id = 1
                """,
                (status, last_error, utc_now()),
            )


class BilibiliSubscriptionRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(
        self,
        uid: int,
        display_name: str,
        enabled: bool,
        poll_interval_seconds: int,
        filter_types: list[str] | None = None,
        filter_keywords: list[str] | None = None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO bilibili_subscriptions(
                    uid, display_name, enabled, poll_interval_seconds,
                    filter_types_json, filter_keywords_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(uid) DO UPDATE SET
                    display_name=excluded.display_name,
                    enabled=excluded.enabled,
                    poll_interval_seconds=excluded.poll_interval_seconds,
                    filter_types_json=excluded.filter_types_json,
                    filter_keywords_json=excluded.filter_keywords_json
                """,
                (
                    uid,
                    display_name,
                    int(enabled),
                    max(poll_interval_seconds, 60),
                    self.database.dumps(filter_types or []),
                    self.database.dumps(filter_keywords or []),
                ),
            )

    def list_enabled(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bilibili_subscriptions WHERE enabled = 1 ORDER BY uid"
            ).fetchall()
        return [self._deserialize(row) for row in rows]

    def list_all(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM bilibili_subscriptions ORDER BY uid"
            ).fetchall()
        return [self._deserialize(row) for row in rows]

    def delete(self, uid: int) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM bilibili_subscriptions WHERE uid = ?", (uid,))

    def update_display_name(self, uid: int, display_name: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE bilibili_subscriptions SET display_name = ? WHERE uid = ?",
                (display_name, uid),
            )

    def update_cursor(
        self,
        uid: int,
        last_dynamic_id: str,
        recent_dynamic_ids: list[str],
        next_poll_at: str,
        last_error: str = "",
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE bilibili_subscriptions
                SET last_dynamic_id = ?, recent_dynamic_ids_json = ?,
                    next_poll_at = ?, last_success_at = ?, last_error = ?
                WHERE uid = ?
                """,
                (
                    last_dynamic_id,
                    self.database.dumps(recent_dynamic_ids),
                    next_poll_at,
                    utc_now() if not last_error else None,
                    last_error,
                    uid,
                ),
            )

    def update_error(self, uid: int, error: str, next_poll_at: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE bilibili_subscriptions
                SET last_error = ?, next_poll_at = ?
                WHERE uid = ?
                """,
                (error, next_poll_at, uid),
            )

    def _deserialize(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["filter_types"] = self.database.loads(item.pop("filter_types_json"), [])
        item["filter_keywords"] = self.database.loads(item.pop("filter_keywords_json"), [])
        item["recent_dynamic_ids"] = self.database.loads(item.pop("recent_dynamic_ids_json"), [])
        return item


class WatchSourceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(
        self,
        source_type: str,
        source_key: str,
        name: str,
        url: str,
        category: str,
        enabled: bool,
        poll_interval_seconds: int,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO watch_sources(
                    source_type, source_key, name, url, category, enabled,
                    poll_interval_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_key) DO UPDATE SET
                    name=excluded.name,
                    url=excluded.url,
                    category=excluded.category,
                    enabled=excluded.enabled,
                    poll_interval_seconds=excluded.poll_interval_seconds
                """,
                (
                    source_type,
                    source_key,
                    name,
                    url,
                    category,
                    int(enabled),
                    max(poll_interval_seconds, 60),
                ),
            )

    def exists(self, source_type: str, source_key: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM watch_sources
                WHERE source_type = ? AND source_key = ?
                """,
                (source_type, source_key),
            ).fetchone()
        return row is not None

    def list_enabled(self, source_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM watch_sources WHERE enabled = 1"
        params: tuple[Any, ...] = ()
        if source_type:
            query += " AND source_type = ?"
            params = (source_type,)
        query += " ORDER BY id"
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_all(self, source_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM watch_sources"
        params: tuple[Any, ...] = ()
        if source_type:
            query += " WHERE source_type = ?"
            params = (source_type,)
        query += " ORDER BY id"
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_poll_state(
        self,
        source_id: int,
        next_poll_at: str,
        error: str = "",
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE watch_sources
                SET next_poll_at = ?,
                    last_success_at = CASE WHEN ? = '' THEN ? ELSE last_success_at END,
                    last_error = ?
                WHERE id = ?
                """,
                (next_poll_at, error, utc_now(), error, source_id),
            )


class DeliveryTargetRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(
        self,
        scene_type: SceneType,
        target_openid: str,
        enabled: bool = True,
        message_mode: MessageMode = MessageMode.IMAGE,
        render_mode: str = "playwright",
    ) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_targets(
                    scene_type, target_openid, enabled, message_mode,
                    render_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scene_type, target_openid) DO UPDATE SET
                    enabled=excluded.enabled,
                    message_mode=excluded.message_mode,
                    render_mode=excluded.render_mode,
                    updated_at=excluded.updated_at
                """,
                (
                    scene_type.value,
                    target_openid,
                    int(enabled),
                    message_mode.value,
                    render_mode,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM delivery_targets WHERE scene_type = ? AND target_openid = ?",
                (scene_type.value, target_openid),
            ).fetchone()
        if row is None:
            raise RuntimeError("保存推送目标后未找到记录")
        return int(row["id"])

    def list_all(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM delivery_targets ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def list_enabled(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_targets WHERE enabled = 1 ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]


class QQBindingRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        code_hash: str,
        scene_type: SceneType,
        message_mode: MessageMode,
        expires_at: str,
    ) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO qq_binding_codes(
                    code_hash, scene_type, message_mode, status,
                    created_at, expires_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (code_hash, scene_type.value, message_mode.value, now, expires_at),
            )
        return int(cursor.lastrowid)

    def get(self, binding_id: int) -> dict[str, Any] | None:
        self.expire_pending()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM qq_binding_codes WHERE id = ?", (binding_id,)
            ).fetchone()
        return dict(row) if row else None

    def expire_pending(self, now: str | None = None) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE qq_binding_codes
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now or utc_now(),),
            )
        return cursor.rowcount

    def consume(
        self,
        code_hash: str,
        scene_type: SceneType,
        target_openid: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE qq_binding_codes
                SET status = 'expired'
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now,),
            )
            row = connection.execute(
                """
                SELECT * FROM qq_binding_codes
                WHERE code_hash = ? AND scene_type = ? AND status = 'pending'
                  AND expires_at > ?
                """,
                (code_hash, scene_type.value, now),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                """
                UPDATE qq_binding_codes
                SET status = 'bound', consumed_at = ?, target_openid = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now, target_openid, int(row["id"])),
            )
            if cursor.rowcount != 1:
                return None
        result = dict(row)
        result.update(status="bound", consumed_at=now, target_openid=target_openid)
        return result

    def cancel(self, binding_id: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE qq_binding_codes
                SET status = 'cancelled'
                WHERE id = ? AND status = 'pending'
                """,
                (binding_id,),
            )
        return cursor.rowcount == 1


class ContentRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def get_by_id(self, content_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM content_items WHERE id = ?", (content_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_with_outbox(
        self,
        content: NormalizedContent,
        create_tasks: bool = True,
    ) -> tuple[int, bool, int]:
        """在同一事务中保存内容并为当前启用目标创建推送任务。"""
        created_at = utc_now()
        content_hash = hashlib.sha256(
            f"{content.title}\n{content.content_text}\n{content.source_url}".encode()
        ).hexdigest()
        published_at = content.published_at.isoformat() if content.published_at else None

        with self.database.connect() as connection:
            connection.execute("BEGIN")
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO content_items(
                        source_type, source_key, external_id, title, author, category,
                        content_text, content_html, render_payload_json, raw_payload_json,
                        source_url, published_at, content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content.source_type.value,
                        content.source_key,
                        content.external_id,
                        content.title,
                        content.author,
                        content.category,
                        content.content_text,
                        content.content_html,
                        self.database.dumps(content.render_payload),
                        self.database.dumps(content.raw_payload),
                        content.source_url,
                        published_at,
                        content_hash,
                        created_at,
                    ),
                )
                inserted = cursor.rowcount == 1
                row = connection.execute(
                    """
                    SELECT id FROM content_items
                    WHERE source_type = ? AND source_key = ? AND external_id = ?
                    """,
                    (content.source_type.value, content.source_key, content.external_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("内容入库后无法读取内容 ID")
                content_id = int(row["id"])
                task_count = 0
                if inserted and create_tasks:
                    targets = connection.execute(
                        "SELECT id FROM delivery_targets WHERE enabled = 1"
                    ).fetchall()
                    for target in targets:
                        task = connection.execute(
                            """
                            INSERT OR IGNORE INTO delivery_tasks(
                                content_item_id, delivery_target_id, status, created_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                content_id,
                                int(target["id"]),
                                DeliveryStatus.PENDING.value,
                                created_at,
                            ),
                        )
                        task_count += task.rowcount
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return content_id, inserted, task_count

    def exists(self, source_type: str, source_key: str, external_id: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM content_items
                WHERE source_type = ? AND source_key = ? AND external_id = ?
                """,
                (source_type, source_key, external_id),
            ).fetchone()
        return row is not None

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM content_items ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


class DeliveryTaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT task.*, content_items.source_type, content_items.source_key,
                       content_items.external_id, content_items.title,
                       content_items.content_text, content_items.render_payload_json,
                       content_items.raw_payload_json, content_items.source_url,
                       content_items.content_html,
                       delivery_targets.scene_type, delivery_targets.target_openid,
                       delivery_targets.message_mode, delivery_targets.render_mode
                FROM delivery_tasks AS task
                JOIN content_items ON content_items.id = task.content_item_id
                JOIN delivery_targets ON delivery_targets.id = task.delivery_target_id
                WHERE task.status IN ('pending', 'retry')
                  AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?)
                  AND delivery_targets.enabled = 1
                ORDER BY task.created_at
                LIMIT ?
                """,
                (utc_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processing(self, task_id: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delivery_tasks SET status = 'processing'
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (task_id,),
            )
        return cursor.rowcount == 1

    def mark_sent(self, task_id: int, message_id: str, trace_id: str = "") -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'sent', qq_message_id = ?, qq_trace_id = ?,
                    last_error = '', sent_at = ?
                WHERE id = ?
                """,
                (message_id, trace_id, utc_now(), task_id),
            )

    def mark_retry(self, task_id: int, retry_count: int, next_retry_at: str, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'retry', retry_count = ?, next_retry_at = ?, last_error = ?
                WHERE id = ?
                """,
                (retry_count, next_retry_at, error, task_id),
            )

    def mark_failed(self, task_id: int, retry_count: int, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'failed', retry_count = ?, last_error = ?
                WHERE id = ?
                """,
                (retry_count, error, task_id),
            )

    def count_by_status(self) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM delivery_tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}
