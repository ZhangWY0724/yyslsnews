import sqlite3
from datetime import datetime, timezone

from yysls_news.domain.models import MessageMode, NormalizedContent, SceneType, SourceType
from yysls_news.storage.database import Database
from yysls_news.storage.repositories import (
    ContentRepository,
    DeliveryTargetRepository,
    DeliveryTaskRepository,
    PushHistoryRepository,
)


def _content(external_id: str = "1") -> NormalizedContent:
    return NormalizedContent(
        source_type=SourceType.BILIBILI,
        source_key="42",
        external_id=external_id,
        title="标题",
        author="UP",
        category="文字",
        content_text="内容",
        content_html="<p>内容</p>",
        source_url="https://t.bilibili.com/1",
        published_at=datetime.now(timezone.utc),
        render_payload={"title": "标题", "content": "内容"},
        raw_payload={"id_str": external_id},
    )


def test_content_and_outbox_are_inserted_once(tmp_path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()
    targets = DeliveryTargetRepository(database)
    targets.upsert(SceneType.GROUP, "group-openid", message_mode=MessageMode.IMAGE)
    contents = ContentRepository(database)

    content_id, inserted, task_count = contents.insert_with_outbox(_content())
    duplicate_id, duplicate, duplicate_tasks = contents.insert_with_outbox(_content())

    assert content_id == duplicate_id
    assert inserted is True
    assert task_count == 1
    assert duplicate is False
    assert duplicate_tasks == 0
    assert DeliveryTaskRepository(database).count_by_status() == {"pending": 1}


def test_existing_delivery_targets_get_display_name_column(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE delivery_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_type TEXT NOT NULL,
                target_openid TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                message_mode TEXT NOT NULL DEFAULT 'image',
                render_mode TEXT NOT NULL DEFAULT 'playwright',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(scene_type, target_openid)
            )
            """
        )

    database = Database(path)
    database.initialize()

    with database.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(delivery_targets)").fetchall()
        }
    assert "display_name" in columns


def test_push_history_records_success_and_failure(tmp_path) -> None:
    database = Database(tmp_path / "push-history.db")
    database.initialize()
    history = PushHistoryRepository(database)

    success_id = history.create_attempt(
        content_item_id=None,
        delivery_target_id=None,
        trigger_type="historical_test",
        source_type="bilibili",
        title="历史动态",
        scene_type="group",
        target_openid="group-openid",
        target_display_name="测试群",
    )
    history.mark_sent(success_id, "message-1", "trace-1")
    failed_id = history.create_attempt(
        content_item_id=None,
        delivery_target_id=None,
        trigger_type="scheduled",
        source_type="yysls",
        title="官网新闻",
        scene_type="user",
        target_openid="user-openid",
    )
    history.mark_failed(failed_id, "QQBotApiError: 目标不存在")

    records = history.list_recent()
    assert [record["status"] for record in records] == ["failed", "sent"]
    assert records[1]["qq_message_id"] == "message-1"
    assert records[0]["error"] == "QQBotApiError: 目标不存在"
    assert history.count_by_status() == {"failed": 1, "sent": 1}
