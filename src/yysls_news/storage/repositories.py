from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from yysls_news.domain.models import (
    DeliveryStatus,
    MessageMode,
    NormalizedContent,
    SceneType,
)
from yysls_news.storage.database import Database, utc_now

PROCESSING_LEASE_SECONDS = 600


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

    def delete(self, uid: int) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM bilibili_subscriptions WHERE uid = ?", (uid,)
            )
        return cursor.rowcount == 1

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
                    poll_interval_seconds=excluded.poll_interval_seconds,
                    deleted_at=NULL
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

    def update(
        self,
        source_id: int,
        source_type: str,
        name: str,
        url: str,
        category: str,
        enabled: bool,
        poll_interval_seconds: int,
    ) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE watch_sources
                SET name = ?, url = ?, category = ?, enabled = ?,
                    poll_interval_seconds = ?
                WHERE id = ? AND source_type = ?
                  AND deleted_at IS NULL
                """,
                (
                    name.strip(),
                    url.strip(),
                    category.strip(),
                    int(enabled),
                    max(poll_interval_seconds, 60),
                    source_id,
                    source_type,
                ),
            )
        return cursor.rowcount == 1

    def delete(self, source_id: int, source_type: str) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE watch_sources
                SET enabled = 0, deleted_at = ?
                WHERE id = ? AND source_type = ? AND deleted_at IS NULL
                """,
                (utc_now(), source_id, source_type),
            )
        return cursor.rowcount == 1

    def list_enabled(self, source_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM watch_sources WHERE enabled = 1 AND deleted_at IS NULL"
        params: tuple[Any, ...] = ()
        if source_type:
            query += " AND source_type = ?"
            params = (source_type,)
        query += " ORDER BY id"
        with self.database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_all(self, source_type: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM watch_sources WHERE deleted_at IS NULL"
        params: tuple[Any, ...] = ()
        if source_type:
            query += " AND source_type = ?"
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
        display_name: str = "",
    ) -> int:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_targets(
                    scene_type, target_openid, display_name, enabled, message_mode,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scene_type, target_openid) DO UPDATE SET
                    display_name = CASE
                        WHEN excluded.display_name <> '' THEN excluded.display_name
                        ELSE delivery_targets.display_name
                    END,
                    enabled=excluded.enabled,
                    message_mode=excluded.message_mode,
                    updated_at=excluded.updated_at,
                    deleted_at=NULL
                """,
                (
                    scene_type.value,
                    target_openid,
                    display_name.strip(),
                    int(enabled),
                    message_mode.value,
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

    def update_display_name(
        self, scene_type: SceneType, target_openid: str, display_name: str
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_targets
                SET display_name = ?, updated_at = ?
                WHERE scene_type = ? AND target_openid = ? AND deleted_at IS NULL
                """,
                (display_name.strip(), utc_now(), scene_type.value, target_openid),
            )

    def get_by_id(self, target_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_targets WHERE id = ? AND deleted_at IS NULL",
                (target_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_settings(
        self,
        target_id: int,
        display_name: str,
        enabled: bool,
        message_mode: MessageMode,
    ) -> bool:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delivery_targets
                SET display_name = ?, enabled = ?, message_mode = ?, updated_at = ?
                WHERE id = ? AND deleted_at IS NULL
                """,
                (
                    display_name.strip(),
                    int(enabled),
                    message_mode.value,
                    utc_now(),
                    target_id,
                ),
            )
        return cursor.rowcount == 1

    def delete(self, target_id: int) -> bool:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT id FROM delivery_targets "
                    "WHERE id = ? AND deleted_at IS NULL",
                    (target_id,),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    """
                    UPDATE delivery_tasks
                    SET status = 'failed', last_error = '推送目标已删除',
                        processing_started_at = NULL
                    WHERE delivery_target_id = ?
                      AND status IN ('pending', 'processing', 'retry')
                    """,
                    (target_id,),
                )
                connection.execute(
                    """
                    UPDATE delivery_targets
                    SET enabled = 0, deleted_at = ?, updated_at = ?
                    WHERE id = ? AND deleted_at IS NULL
                    """,
                    (now, now, target_id),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def list_all(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_targets WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_enabled(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM delivery_targets "
                "WHERE enabled = 1 AND deleted_at IS NULL ORDER BY id"
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
                """SELECT id, source_type, source_key, external_id, title, author,
                          category, source_url, published_at, body_text,
                          video_cover_url, video_url, created_at
                   FROM content_items WHERE id = ?""",
                (content_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_with_outbox(
        self,
        content: NormalizedContent,
        create_tasks: bool = True,
    ) -> tuple[int, bool, int]:
        """在同一事务中保存内容并为当前启用目标创建推送任务。"""
        created_at = utc_now()
        published_at = content.published_at.isoformat() if content.published_at else None

        with self.database.connect() as connection:
            connection.execute("BEGIN")
            try:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO content_items(
                        source_type, source_key, external_id, title, author, category,
                        source_url, published_at, body_text, video_cover_url,
                        video_url, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        content.source_type.value,
                        content.source_key,
                        content.external_id,
                        content.title,
                        content.author,
                        content.category,
                        content.source_url,
                        published_at,
                        content.body_text,
                        content.video_cover_url,
                        content.video_url,
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
                """SELECT id, source_type, source_key, external_id, title, author,
                          category, source_url, published_at, created_at
                   FROM content_items ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class RenderArtifactRepository:
    """为同一内容的所有图片推送任务共享渲染结果。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def claim(self, content_id: int, version: int, lease_seconds: int = 600) -> dict[str, Any]:
        now = utc_now()
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)).isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT version, status, pages_json, last_error, updated_at "
                    "FROM render_artifacts WHERE content_item_id = ?",
                    (content_id,),
                ).fetchone()
                if row and int(row["version"]) == version:
                    status = str(row["status"])
                    if status == "ready":
                        pages = self.database.loads(str(row["pages_json"]), [])
                        if isinstance(pages, list) and pages:
                            connection.execute("COMMIT")
                            return {"status": "ready", "pages": pages}
                    elif status == "failed":
                        connection.execute("COMMIT")
                        return {"status": "failed", "error": str(row["last_error"])}
                    elif status == "rendering" and str(row["updated_at"]) >= stale_before:
                        connection.execute("COMMIT")
                        return {"status": "busy"}

                connection.execute(
                    """
                    INSERT INTO render_artifacts(
                        content_item_id, version, status, pages_json, last_error, updated_at
                    ) VALUES (?, ?, 'rendering', '[]', '', ?)
                    ON CONFLICT(content_item_id) DO UPDATE SET
                        version=excluded.version,
                        status=excluded.status,
                        pages_json='[]',
                        last_error='',
                        updated_at=excluded.updated_at
                    """,
                    (content_id, version, now),
                )
                connection.execute("COMMIT")
                return {"status": "claimed"}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def mark_ready(self, content_id: int, version: int, pages: list[str]) -> None:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE render_artifacts
                SET status = 'ready', pages_json = ?, last_error = '', updated_at = ?
                WHERE content_item_id = ? AND version = ? AND status = 'rendering'
                """,
                (self.database.dumps(pages), utc_now(), content_id, version),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("渲染产物状态已改变，无法保存图片")

    def mark_failed(self, content_id: int, version: int, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE render_artifacts
                SET status = 'failed', pages_json = '[]', last_error = ?, updated_at = ?
                WHERE content_item_id = ? AND version = ? AND status = 'rendering'
                """,
                (error[:500], utc_now(), content_id, version),
            )

    def invalidate(self, content_id: int, version: int) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM render_artifacts "
                "WHERE content_item_id = ? AND version = ? AND status = 'ready'",
                (content_id, version),
            )

    def cleanup_if_finished(self, content_id: int) -> list[str]:
        """全部启用目标完成推送后，返回待清理的共享截图文件名。"""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    """
                    SELECT 1 FROM delivery_tasks AS task
                    JOIN delivery_targets AS target ON target.id = task.delivery_target_id
                    WHERE task.content_item_id = ?
                      AND task.status IN ('pending', 'processing', 'retry')
                      AND target.enabled = 1
                    LIMIT 1
                    """,
                    (content_id,),
                ).fetchone()
                if active:
                    connection.execute("COMMIT")
                    return []
                row = connection.execute(
                    "SELECT pages_json FROM render_artifacts WHERE content_item_id = ?",
                    (content_id,),
                ).fetchone()
                connection.execute(
                    "DELETE FROM render_artifacts WHERE content_item_id = ?",
                    (content_id,),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        pages = self.database.loads(str(row["pages_json"]), []) if row else []
        return [str(page) for page in pages] if isinstance(pages, list) else []


class DeliveryTaskRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        now = utc_now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=PROCESSING_LEASE_SECONDS)
        ).isoformat()
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT task.*, content_items.source_type, content_items.source_key,
                       content_items.external_id, content_items.title,
                       content_items.source_url, content_items.author,
                       content_items.category, content_items.body_text,
                       content_items.video_cover_url, content_items.video_url,
                       delivery_targets.scene_type, delivery_targets.target_openid,
                       delivery_targets.display_name AS target_display_name,
                       delivery_targets.message_mode
                FROM delivery_tasks AS task
                JOIN content_items ON content_items.id = task.content_item_id
                JOIN delivery_targets ON delivery_targets.id = task.delivery_target_id
                WHERE (
                    (task.status IN ('pending', 'retry')
                     AND (task.next_retry_at IS NULL OR task.next_retry_at <= ?))
                    OR (task.status = 'processing'
                        AND (task.processing_started_at IS NULL
                             OR task.processing_started_at <= ?))
                )
                  AND delivery_targets.enabled = 1
                ORDER BY task.created_at
                LIMIT ?
                """,
                (now, stale_before, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_processing(self, task_id: int) -> bool:
        now = utc_now()
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=PROCESSING_LEASE_SECONDS)
        ).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'processing', processing_started_at = ?
                WHERE id = ? AND (
                    status IN ('pending', 'retry')
                    OR (status = 'processing'
                        AND (processing_started_at IS NULL OR processing_started_at <= ?))
                )
                """,
                (now, task_id, stale_before),
            )
        return cursor.rowcount == 1

    def mark_sent(self, task_id: int, message_id: str, trace_id: str = "") -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'sent', qq_message_id = ?, qq_trace_id = ?,
                    last_error = '', sent_at = ?, processing_started_at = NULL
                WHERE id = ?
                """,
                (message_id, trace_id, utc_now(), task_id),
            )

    def mark_retry(self, task_id: int, retry_count: int, next_retry_at: str, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'retry', retry_count = ?, next_retry_at = ?,
                    last_error = ?, processing_started_at = NULL
                WHERE id = ?
                """,
                (retry_count, next_retry_at, error, task_id),
            )

    def mark_failed(self, task_id: int, retry_count: int, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE delivery_tasks
                SET status = 'failed', retry_count = ?, last_error = ?,
                    processing_started_at = NULL
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


class PushHistoryRepository:
    """记录每一次实际推送尝试，供管理页面查询历史结果。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def create_attempt(
        self,
        *,
        content_item_id: int | None,
        delivery_target_id: int | None,
        trigger_type: str,
        source_type: str,
        title: str,
        scene_type: str,
        target_openid: str,
        target_display_name: str = "",
        attempt_number: int = 1,
    ) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO push_history(
                    content_item_id, delivery_target_id, trigger_type, source_type,
                    title, scene_type, target_openid, target_display_name,
                    attempt_number, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', ?)
                """,
                (
                    content_item_id,
                    delivery_target_id,
                    trigger_type,
                    source_type,
                    title,
                    scene_type,
                    target_openid,
                    target_display_name,
                    max(attempt_number, 1),
                    utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def mark_sent(self, history_id: int, message_id: str, trace_id: str = "") -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE push_history
                SET status = 'sent', qq_message_id = ?, qq_trace_id = ?,
                    error = '', finished_at = ?
                WHERE id = ?
                """,
                (message_id, trace_id, utc_now(), history_id),
            )

    def mark_failed(self, history_id: int, error: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE push_history
                SET status = 'failed', error = ?, finished_at = ?
                WHERE id = ?
                """,
                (error[:1000], utc_now(), history_id),
            )

    def list_recent(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM push_history"
        parameters: list[Any] = []
        if status:
            query += " WHERE status = ?"
            parameters.append(status)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        parameters.append(max(1, min(limit, 500)))
        with self.database.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM push_history GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}
