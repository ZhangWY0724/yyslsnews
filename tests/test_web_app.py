import logging
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from yysls_news.application import ApplicationContext
from yysls_news.collectors.bilibili.auth import BilibiliLoginError, LoginPollResult
from yysls_news.collectors.bilibili.client import BilibiliClient
from yysls_news.config import Settings
from yysls_news.delivery.qqbot.client import QQMessageResult
from yysls_news.domain.models import MessageMode, NormalizedContent, SceneType, SourceType
from yysls_news.security.passwords import hash_password
from yysls_news.services.delivery import DeliveryWorker
from yysls_news.web.app import create_app


def _mark_admin_password_changed(context: ApplicationContext) -> None:
    context.runtime_config.save_admin_password_hash(hash_password(context.settings.admin_password))


def test_initial_admin_password_requires_change(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "initial-password.db",
        encryption_key=Fernet.generate_key().decode(),
        admin_username="admin",
        admin_password="initial-password",
        host="127.0.0.1",
        port=43100,
        yysls_poll_interval_seconds=600,
        http_timeout_seconds=5,
        qqbot_api_base_url="https://api.bot.qq.com",
        qqbot_app_id="",
        qqbot_app_secret="",
    )
    app = create_app(ApplicationContext.create(settings))

    with TestClient(app) as client:
        page = client.get("/", auth=("admin", "initial-password"))
        assert page.status_code == 200
        assert "首次登录，请修改管理密码" in page.text

        blocked = client.get("/api/dashboard", auth=("admin", "initial-password"))
        assert blocked.status_code == 428

        changed = client.post(
            "/api/auth/password",
            auth=("admin", "initial-password"),
            json={"new_password": "new-password", "confirm_password": "new-password"},
        )
        assert changed.status_code == 200
        assert changed.json() == {"changed": True}

        assert client.get("/api/dashboard", auth=("admin", "initial-password")).status_code == 401
        assert client.get("/api/dashboard", auth=("admin", "new-password")).status_code == 200
        assert "系统总览" in client.get("/", auth=("admin", "new-password")).text

        logging.getLogger("tests.runtime.logs").warning("运行日志测试记录")
        logs = client.get("/api/logs", auth=("admin", "new-password"))
        assert logs.status_code == 200
        assert any(item["message"] == "运行日志测试记录" for item in logs.json()["items"])

        history_id = app.state.context.push_history.create_attempt(
            content_item_id=None,
            delivery_target_id=None,
            trigger_type="qqbot_test",
            source_type="",
            title="测试消息",
            scene_type="group",
            target_openid="group-openid",
        )
        app.state.context.push_history.mark_sent(history_id, "message-1", "trace-1")
        history = client.get("/api/push-history?status=sent", auth=("admin", "new-password"))
        assert history.status_code == 200
        assert history.json()["items"][0]["qq_message_id"] == "message-1"


def test_web_configures_subscription_target_and_qqbot(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_path=tmp_path / "web.db",
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
    context = ApplicationContext.create(settings)
    _mark_admin_password_changed(context)
    app = create_app(context)

    async def fake_get_user_info(self, uid: int) -> dict[str, str]:
        return {"name": f"测试UP-{uid}"}

    monkeypatch.setattr(BilibiliClient, "get_user_info", fake_get_user_info)

    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/", auth=("admin", "password")).status_code == 200
        response = client.post(
            "/api/bilibili/subscriptions",
            auth=("admin", "password"),
            json={"uid": 42, "poll_interval_seconds": 300},
        )
        assert response.status_code == 200
        assert response.json()["display_name"] == "测试UP-42"
        app.state.context.subscriptions.upsert(
            uid=43,
            display_name="43",
            enabled=True,
            poll_interval_seconds=300,
        )
        subscriptions = client.get(
            "/api/bilibili/subscriptions", auth=("admin", "password")
        ).json()
        assert (
            next(item for item in subscriptions if item["uid"] == 43)["display_name"]
            == "测试UP-43"
        )
        response = client.post(
            "/api/qqbot/config",
            auth=("admin", "password"),
            json={"app_id": "app", "app_secret": "secret"},
        )
        assert response.status_code == 200
        assert response.json()["app_secret"] != "secret"

        binding = client.post(
            "/api/targets/bindings",
            auth=("admin", "password"),
            json={"scene_type": "group", "message_mode": "image"},
        )
        assert binding.status_code == 200
        binding_payload = binding.json()
        assert len(binding_payload["code"]) == 8
        assert binding_payload["ttl_seconds"] == 180
        assert client.get(
            f"/api/targets/bindings/{binding_payload['id']}",
            auth=("admin", "password"),
        ).json()["status"] == "pending"
        app.state.context.qq_bindings.consume(
            SceneType.GROUP, binding_payload["code"], "group-openid"
        )
        dashboard = client.get("/api/dashboard", auth=("admin", "password")).json()
        assert dashboard["targets"][0]["target_openid"] == "group-openid"

        html = client.get("/", auth=("admin", "password")).text
        assert "系统总览" in html
        assert "source-table" in html
        assert '<pre id="dashboard">' not in html
        assert "当前账号" in html
        assert "每 2 秒自动检查状态" in html
        assert "自动绑定目标" in html
        assert "已绑定目标" in html
        assert "测试推送" in html
        assert "推送记录" in html


def test_historical_content_test_push_uses_selected_target(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_path=tmp_path / "historical-test-push.db",
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
    context = ApplicationContext.create(settings)
    _mark_admin_password_changed(context)
    context.runtime_config.save_qqbot("app", "secret")
    target_id = context.targets.upsert(
        SceneType.GROUP,
        "group-openid",
        message_mode=MessageMode.IMAGE,
    )
    content_id, inserted, _ = context.contents.insert_with_outbox(
        NormalizedContent(
            source_type=SourceType.BILIBILI,
            source_key="42",
            external_id="dynamic-1",
            title="历史动态",
            author="测试UP",
            category="文字",
            source_url="https://www.bilibili.com/opus/dynamic-1",
            published_at=None,
        ),
        create_tasks=False,
    )
    assert inserted is True
    captured: dict[str, object] = {}

    async def fake_send_test(self, content, target):
        captured["content_id"] = content["id"]
        captured["target_id"] = target["id"]
        return QQMessageResult(message_id="message-1", timestamp="", trace_id="trace-1")

    monkeypatch.setattr(DeliveryWorker, "send_test", fake_send_test)
    app = create_app(context)

    with TestClient(app) as client:
        response = client.post(
            f"/api/contents/{content_id}/test-push",
            auth=("admin", "password"),
            json={"target_id": target_id},
        )

    assert response.status_code == 200
    assert response.json() == {
        "content_id": content_id,
        "target_id": target_id,
        "message_id": "message-1",
        "trace_id": "trace-1",
    }
    assert captured == {"content_id": content_id, "target_id": target_id}


def test_bilibili_login_saves_and_exposes_username(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_path=tmp_path / "login-username.db",
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
    context = ApplicationContext.create(settings)
    _mark_admin_password_changed(context)
    context.qr_login.poll = AsyncMock(
        return_value=LoginPollResult(
            session_id="test-session",
            code=0,
            message="登录成功",
            credential={
                "sessdata": "sessdata-value",
                "bili_jct": "bili-jct-value",
                "dedeuserid": "42",
            },
        )
    )

    async def fake_get_user_info(self, uid: int) -> dict[str, str]:
        return {"name": "已登录用户"}

    monkeypatch.setattr(BilibiliClient, "get_user_info", fake_get_user_info)
    app = create_app(context)

    with TestClient(app) as client:
        response = client.post(
            "/api/bilibili/login/poll",
            auth=("admin", "password"),
            json={"session_id": "test-session"},
        )
        assert response.status_code == 200
        assert response.json()["username"] == "已登录用户"

        status_response = client.get("/api/bilibili/login/status", auth=("admin", "password"))
        assert status_response.json()["username"] == "已登录用户"


def test_bilibili_login_error_returns_readable_http_error(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "login.db",
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
    context = ApplicationContext.create(settings)
    _mark_admin_password_changed(context)
    context.qr_login.poll = AsyncMock(side_effect=BilibiliLoginError("测试扫码错误"))
    app = create_app(context)

    with TestClient(app) as client:
        response = client.post(
            "/api/bilibili/login/poll",
            auth=("admin", "password"),
            json={"session_id": "test-session"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "测试扫码错误"
