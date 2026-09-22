from datetime import datetime, timezone

from yysls_news.domain.models import MessageMode, NormalizedContent, SceneType, SourceType
from yysls_news.storage.database import Database
from yysls_news.storage.repositories import (
    ContentRepository,
    DeliveryTargetRepository,
    DeliveryTaskRepository,
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
