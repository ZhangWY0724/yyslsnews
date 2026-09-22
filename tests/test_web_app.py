from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from yysls_news.application import ApplicationContext
from yysls_news.collectors.bilibili.auth import BilibiliLoginError, LoginPollResult
from yysls_news.collectors.bilibili.client import BilibiliClient
from yysls_news.config import Settings
from yysls_news.domain.models import SceneType
from yysls_news.web.app import create_app


def test_web_configures_subscription_target_and_qqbot(tmp_path, monkeypatch) -> None:
    settings = Settings(
        app_env="test",
        database_path=tmp_path / "web.db",
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
    app = create_app(ApplicationContext.create(settings))

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


def test_bilibili_login_saves_and_exposes_username(tmp_path, monkeypatch) -> None:
    settings = Settings(
        app_env="test",
        database_path=tmp_path / "login-username.db",
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
    context = ApplicationContext.create(settings)
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
        app_env="test",
        database_path=tmp_path / "login.db",
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
    context = ApplicationContext.create(settings)
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
