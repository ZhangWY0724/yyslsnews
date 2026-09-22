import pytest
from cryptography.fernet import Fernet

from yysls_news.application import ApplicationContext
from yysls_news.config import Settings
from yysls_news.delivery.qqbot.client import QQMessageResult
from yysls_news.domain.models import MessageMode, SceneType, SourceType
from yysls_news.rendering.renderer import PlaywrightRenderer
from yysls_news.services.delivery import DeliveryWorker


def _settings(database_path) -> Settings:
    return Settings(
        app_env="test",
        database_path=database_path,
        encryption_key=Fernet.generate_key().decode(),
        admin_username="admin",
        admin_password="password",
        host="127.0.0.1",
        port=43100,
        bilibili_poll_interval_seconds=300,
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
        "content_text": "动态内容",
        "source_url": "https://example.com/content/10",
        "render_payload_json": "{}",
    }


def _target() -> dict[str, object]:
    return {
        "id": None,
        "scene_type": SceneType.GROUP.value,
        "target_openid": "group-openid",
        "display_name": "测试群",
        "message_mode": MessageMode.TEXT.value,
        "render_mode": "playwright",
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
