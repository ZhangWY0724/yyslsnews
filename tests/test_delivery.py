import logging

import pytest
from cryptography.fernet import Fernet

from yysls_news.application import ApplicationContext
from yysls_news.config import Settings
from yysls_news.delivery.qqbot.client import QQBotApiError, QQMessageResult, QQTarget
from yysls_news.domain.models import MessageMode, NormalizedContent, SceneType, SourceType
from yysls_news.rendering.renderer import ImageRenderError, PlaywrightRenderer
from yysls_news.services.delivery import DeliveryWorker


def _settings(database_path) -> Settings:
    return Settings(
        database_path=database_path,
        encryption_key=Fernet.generate_key().decode(),
        admin_username="admin",
        admin_password="password",
        host="127.0.0.1",
        port=43100,
        yysls_poll_interval_seconds=600,
        http_timeout_seconds=5,
        qqbot_api_base_url="https://api.bot.qq.com",
        qqbot_app_id="",
        qqbot_app_secret="",
    )


def _content() -> dict[str, object]:
    return {
        "id": None,
        "source_type": SourceType.BILIBILI.value,
        "title": "历史动态",
        "source_url": "https://example.com/content/10",
    }


def _target() -> dict[str, object]:
    return {
        "id": None,
        "scene_type": SceneType.GROUP.value,
        "target_openid": "group-openid",
        "display_name": "测试群",
        "message_mode": MessageMode.TEXT.value,
    }


@pytest.mark.asyncio
async def test_historical_test_push_records_success_and_failure(tmp_path, monkeypatch) -> None:
    context = ApplicationContext.create(_settings(tmp_path / "delivery.db"))
    worker = DeliveryWorker(
        tasks=context.tasks,
        push_history=context.push_history,
        runtime_config=context.runtime_config,
        image_renderer=PlaywrightRenderer(),
    )

    async def fake_get_client(self):
        return object()

    async def fake_send_task(self, client, task):
        return QQMessageResult(message_id="message-1", timestamp="", trace_id="trace-1")

    monkeypatch.setattr(DeliveryWorker, "_get_client", fake_get_client)
    monkeypatch.setattr(DeliveryWorker, "_send_task", fake_send_task)
    result = await worker.send_test(_content(), _target())
    assert result.message_id == "message-1"
    assert context.push_history.list_recent()[0]["status"] == "sent"

    async def fake_failed_send_task(self, client, task):
        raise RuntimeError("测试失败")

    monkeypatch.setattr(DeliveryWorker, "_send_task", fake_failed_send_task)
    with pytest.raises(RuntimeError, match="测试失败"):
        await worker.send_test(_content(), _target())
    records = context.push_history.list_recent()
    assert records[0]["status"] == "failed"
    assert "测试失败" in records[0]["error"]


@pytest.mark.asyncio
async def test_bilibili_pages_upload_before_sending_and_cleanup(tmp_path, caplog) -> None:
    context = ApplicationContext.create(_settings(tmp_path / "delivery.db"))
    output_dir = tmp_path / "screenshots"

    class FakeRenderer:
        async def render_bilibili(self, source_url, output_path):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"original")
            return output_path

        def paginate_bilibili_image(self, path):
            pages = [path.with_name(f"{path.stem}-part-{index:02d}.jpg") for index in (1, 2)]
            for page in pages:
                page.write_bytes(b"page")
            return pages

    class FakeClient:
        def __init__(self, fail_upload_at=0):
            self.fail_upload_at = fail_upload_at
            self.events = []
            self.upload_count = 0
            self.send_count = 0

        async def upload_image(self, target, path):
            assert path.is_file()
            self.upload_count += 1
            self.events.append(("upload", path.name))
            if self.upload_count == self.fail_upload_at:
                raise QQBotApiError("上传失败")
            return f"file-info-{self.upload_count}"

        async def send_uploaded_image(self, target, file_info):
            self.send_count += 1
            self.events.append(("send", file_info))
            return QQMessageResult(
                message_id=f"message-{self.send_count}",
                timestamp="now",
                trace_id=f"trace-{self.send_count}",
            )

    worker = DeliveryWorker(
        tasks=context.tasks,
        push_history=context.push_history,
        runtime_config=context.runtime_config,
        image_renderer=FakeRenderer(),
        output_dir=output_dir,
    )
    target = QQTarget("group", "group-openid")
    task = {"id": 1, "source_type": SourceType.BILIBILI.value, "source_url": "https://example.com"}

    client = FakeClient()
    with caplog.at_level(logging.INFO, logger="yysls_news.services.delivery"):
        result = await worker._send_image(client, target, task)
    assert [event[0] for event in client.events] == ["upload", "upload", "send", "send"]
    assert result.message_id == "message-1,message-2"
    assert result.trace_id == "trace-1,trace-2"
    assert "任务图片分页上传开始" in caplog.text
    assert "任务图片分页发送完成" in caplog.text
    assert "group-openid" not in caplog.text
    assert not list(output_dir.iterdir())

    failing_client = FakeClient(fail_upload_at=2)
    with pytest.raises(QQBotApiError, match="上传失败"):
        await worker._send_image(failing_client, target, task)
    assert [event[0] for event in failing_client.events] == ["upload", "upload"]
    assert not list(output_dir.iterdir())


@pytest.mark.asyncio
async def test_one_content_renders_once_across_target_batches(tmp_path, monkeypatch) -> None:
    context = ApplicationContext.create(_settings(tmp_path / "delivery.db"))
    context.targets.upsert(SceneType.GROUP, "group-a")
    context.targets.upsert(SceneType.GROUP, "group-b")
    content_id, inserted, task_count = context.contents.insert_with_outbox(
        NormalizedContent(
            source_type=SourceType.BILIBILI,
            source_key="42",
            external_id="123",
            title="同一条动态",
            author="测试UP",
            category="图文",
            source_url="https://t.bilibili.com/123",
            published_at=None,
        )
    )
    assert inserted and task_count == 2

    class FakeRenderer:
        def __init__(self):
            self.calls = 0
            self.urls = []

        async def render_bilibili(self, source_url, output_path):
            self.calls += 1
            self.urls.append(source_url)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"shared-image")
            return output_path

        def paginate_bilibili_image(self, path):
            return [path]

    class FakeClient:
        def __init__(self):
            self.targets = []

        async def send_image(self, target, path):
            assert path.is_file()
            self.targets.append(target.openid)
            return QQMessageResult(
                message_id=f"message-{len(self.targets)}", timestamp="now", trace_id=""
            )

    fake_renderer = FakeRenderer()
    fake_client = FakeClient()

    async def fake_get_client(self):
        return fake_client

    monkeypatch.setattr(DeliveryWorker, "_get_client", fake_get_client)
    output_dir = tmp_path / "screenshots"
    worker = DeliveryWorker(
        tasks=context.tasks,
        push_history=context.push_history,
        runtime_config=context.runtime_config,
        image_renderer=fake_renderer,
        output_dir=output_dir,
    )

    assert await worker.process_pending(limit=1) == 1
    assert fake_renderer.calls == 1
    assert fake_renderer.urls == ["https://www.bilibili.com/opus/123"]
    assert list(output_dir.iterdir())
    assert await worker.process_pending(limit=1) == 1
    assert fake_renderer.calls == 1
    assert fake_client.targets == ["group-a", "group-b"]
    assert context.tasks.count_by_status()["sent"] == 2
    with context.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM render_artifacts WHERE content_item_id = ?", (content_id,)
        ).fetchone()[0] == 0
    assert not list(output_dir.iterdir())


@pytest.mark.asyncio
async def test_render_failure_is_shared_before_text_fallback(tmp_path, monkeypatch) -> None:
    context = ApplicationContext.create(_settings(tmp_path / "delivery.db"))
    context.targets.upsert(SceneType.GROUP, "group-a")
    context.targets.upsert(SceneType.GROUP, "group-b")
    _, inserted, task_count = context.contents.insert_with_outbox(
        NormalizedContent(
            source_type=SourceType.BILIBILI,
            source_key="42",
            external_id="dynamic-failure",
            title="截图失败动态",
            author="测试UP",
            category="图文",
            source_url="https://www.bilibili.com/opus/dynamic-failure",
            published_at=None,
        )
    )
    assert inserted and task_count == 2

    class FakeRenderer:
        def __init__(self):
            self.calls = 0

        async def render_bilibili(self, source_url, output_path):
            self.calls += 1
            raise ImageRenderError("正文不可访问")

    class FakeClient:
        def __init__(self):
            self.text_targets = []

        async def send_text(self, target, content):
            self.text_targets.append(target.openid)
            return QQMessageResult(
                message_id=f"fallback-{len(self.text_targets)}", timestamp="now", trace_id=""
            )

    fake_renderer = FakeRenderer()
    fake_client = FakeClient()

    async def fake_get_client(self):
        return fake_client

    monkeypatch.setattr(DeliveryWorker, "_get_client", fake_get_client)
    worker = DeliveryWorker(
        tasks=context.tasks,
        push_history=context.push_history,
        runtime_config=context.runtime_config,
        image_renderer=fake_renderer,
        output_dir=tmp_path / "screenshots",
    )

    assert await worker.process_pending(limit=1) == 1
    assert await worker.process_pending(limit=1) == 1
    assert fake_renderer.calls == 1
    assert fake_client.text_targets == ["group-a", "group-b"]
    assert context.tasks.count_by_status()["sent"] == 2
