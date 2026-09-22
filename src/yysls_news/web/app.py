from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from yysls_news.application import ApplicationContext
from yysls_news.collectors.bilibili.auth import BilibiliLoginError
from yysls_news.collectors.bilibili.client import BilibiliClient, BilibiliDependencyError
from yysls_news.delivery.qqbot.client import QQBotClient, QQTarget
from yysls_news.domain.models import MessageMode, SceneType
from yysls_news.services.qq_binding import BINDING_CODE_TTL_SECONDS


class BilibiliSubscriptionInput(BaseModel):
    uid: int = Field(gt=0)
    display_name: str = ""
    enabled: bool = True
    poll_interval_seconds: int = Field(default=300, ge=60)
    filter_types: list[str] = Field(default_factory=list)
    filter_keywords: list[str] = Field(default_factory=list)


class YyslsSourceInput(BaseModel):
    source_key: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=10, max_length=500)
    category: str = "最新"
    enabled: bool = True
    poll_interval_seconds: int = Field(default=600, ge=60)


class DeliveryTargetInput(BaseModel):
    scene_type: SceneType
    target_openid: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    message_mode: MessageMode = MessageMode.IMAGE
    render_mode: str = Field(default="playwright", pattern="^playwright$")


class QQBotConfigInput(BaseModel):
    app_id: str = Field(min_length=1, max_length=100)
    app_secret: str = ""
    base_url: str | None = None


class PollConfigInput(BaseModel):
    bilibili_poll_interval_seconds: int = Field(ge=60)
    yysls_poll_interval_seconds: int = Field(ge=60)


class LoginPollInput(BaseModel):
    session_id: str = Field(min_length=1)


class BilibiliCookieInput(BaseModel):
    sessdata: str = Field(min_length=1)
    bili_jct: str = Field(min_length=1)
    dedeuserid: str = Field(min_length=1)
    buvid3: str = ""
    buvid4: str = ""
    ac_time_value: str = ""


class QQBotTestInput(BaseModel):
    scene_type: SceneType
    target_openid: str = Field(min_length=1)
    content: str = Field(default="yysls-news 测试消息", max_length=2000)


class QQBindingCodeInput(BaseModel):
    scene_type: SceneType
    message_mode: MessageMode = MessageMode.IMAGE


def create_app(context: ApplicationContext | None = None) -> FastAPI:
    context = context or ApplicationContext.create()
    app = FastAPI(title="资讯监控推送服务", version="0.1.0")
    app.state.context = context
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    basic = HTTPBasic(auto_error=False)

    def require_admin(
        credentials: HTTPBasicCredentials | None = Depends(basic),
    ) -> None:
        if not context.settings.admin_password:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="未配置 ADMIN_PASSWORD，管理页面暂不可用",
            )
        if not credentials or not (
            secrets.compare_digest(credentials.username, context.settings.admin_username)
            and secrets.compare_digest(credentials.password, context.settings.admin_password)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="管理页面认证失败",
                headers={"WWW-Authenticate": "Basic"},
            )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "yysls-news"}

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, _: None = Depends(require_admin)) -> Any:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"title": "资讯监控推送服务"},
        )

    @app.get("/api/dashboard")
    async def dashboard(_: None = Depends(require_admin)) -> dict[str, Any]:
        credential_status = await _load_bilibili_status(context)
        subscriptions = await _enrich_subscription_names(
            context, context.subscriptions.list_all()
        )
        return {
            "credential": credential_status,
            "subscriptions": subscriptions,
            "sources": context.sources.list_all(),
            "targets": context.targets.list_all(),
            "delivery_counts": context.tasks.count_by_status(),
            "recent_contents": context.contents.list_recent(20),
            "qqbot": context.runtime_config.qqbot_public(),
            "qq_listener": context.qq_listener.status(),
            "poll_defaults": context.runtime_config.poll_defaults(),
        }

    @app.get("/api/bilibili/subscriptions")
    async def list_subscriptions(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return await _enrich_subscription_names(context, context.subscriptions.list_all())

    @app.post("/api/bilibili/subscriptions")
    async def save_subscription(
        payload: BilibiliSubscriptionInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        display_name = payload.display_name.strip()
        if not display_name:
            try:
                display_name = await _get_bilibili_user_name(context, payload.uid)
            except BilibiliDependencyError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"获取 UID={payload.uid} 的 UP 主名称失败，请检查网络或 UID",
                ) from exc
            if not display_name:
                raise HTTPException(
                    status_code=502,
                    detail=f"B站未返回 UID={payload.uid} 的 UP 主名称，请确认 UID 正确",
                )
        context.subscriptions.upsert(
            uid=payload.uid,
            display_name=display_name,
            enabled=payload.enabled,
            poll_interval_seconds=payload.poll_interval_seconds,
            filter_types=payload.filter_types,
            filter_keywords=payload.filter_keywords,
        )
        return next(item for item in context.subscriptions.list_all() if item["uid"] == payload.uid)

    @app.delete("/api/bilibili/subscriptions/{uid}")
    async def delete_subscription(uid: int, _: None = Depends(require_admin)) -> dict[str, bool]:
        context.subscriptions.delete(uid)
        return {"deleted": True}

    @app.get("/api/bilibili/login/status")
    async def bili_login_status(_: None = Depends(require_admin)) -> dict[str, Any]:
        return await _load_bilibili_status(context)

    @app.post("/api/bilibili/login/start")
    async def bili_login_start(_: None = Depends(require_admin)) -> dict[str, str]:
        _require_credentials_service(context)
        try:
            info = await context.qr_login.generate()
        except BilibiliLoginError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "session_id": info.session_id,
            "url": info.url,
            "image_data_url": info.image_data_url,
        }

    @app.post("/api/bilibili/login/poll")
    async def bili_login_poll(
        payload: LoginPollInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        service = _require_credentials_service(context)
        try:
            result = await context.qr_login.poll(payload.session_id)
        except BilibiliLoginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result.is_done and result.credential:
            credential = dict(result.credential)
            username = await _try_get_bilibili_user_name(context, credential)
            if username:
                credential["username"] = username
            service.save(credential)
            return {
                "status": "logged_in",
                "username": username,
                "message": result.message or "B站登录成功",
            }
        return {
            "status": "expired" if result.is_expired else "waiting_scan",
            "code": result.code,
            "message": result.message,
        }

    @app.post("/api/bilibili/login/cookie")
    async def bili_login_cookie(
        payload: BilibiliCookieInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        service = _require_credentials_service(context)
        credential = payload.model_dump()
        username = await _try_get_bilibili_user_name(context, credential)
        if username:
            credential["username"] = username
        service.save(credential, status="logged_in")
        return service.status()

    @app.get("/api/sources")
    async def list_sources(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return context.sources.list_all("yysls")

    @app.post("/api/sources")
    async def save_source(
        payload: YyslsSourceInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        if not payload.url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="来源 URL 必须是 HTTP(S) 地址")
        context.sources.upsert(
            source_type="yysls",
            source_key=payload.source_key,
            name=payload.name,
            url=payload.url,
            category=payload.category,
            enabled=payload.enabled,
            poll_interval_seconds=payload.poll_interval_seconds,
        )
        return next(
            item
            for item in context.sources.list_all("yysls")
            if item["source_key"] == payload.source_key
        )

    @app.get("/api/targets")
    async def list_targets(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return context.targets.list_all()

    @app.post("/api/targets/bindings")
    async def create_target_binding(
        payload: QQBindingCodeInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        binding = context.qq_bindings.create(payload.scene_type, payload.message_mode)
        return {
            "id": binding.id,
            "code": binding.code,
            "scene_type": binding.scene_type.value,
            "message_mode": binding.message_mode.value,
            "expires_at": binding.expires_at,
            "ttl_seconds": BINDING_CODE_TTL_SECONDS,
        }

    @app.get("/api/targets/bindings/{binding_id}")
    async def target_binding_status(
        binding_id: int,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        result = context.qq_bindings.get_status(binding_id)
        if result is None:
            raise HTTPException(status_code=404, detail="绑定码不存在")
        return result

    @app.delete("/api/targets/bindings/{binding_id}")
    async def cancel_target_binding(
        binding_id: int,
        _: None = Depends(require_admin),
    ) -> dict[str, bool]:
        if not context.qq_bindings.cancel(binding_id):
            raise HTTPException(status_code=409, detail="绑定码已使用、已过期或不存在")
        return {"cancelled": True}

    @app.post("/api/targets")
    async def save_target(
        payload: DeliveryTargetInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        target_id = context.targets.upsert(
            scene_type=payload.scene_type,
            target_openid=payload.target_openid,
            enabled=payload.enabled,
            message_mode=payload.message_mode,
            render_mode=payload.render_mode,
        )
        return {"id": target_id, **payload.model_dump(mode="json")}

    @app.get("/api/qqbot/config")
    async def qqbot_config(_: None = Depends(require_admin)) -> dict[str, str | bool]:
        return context.runtime_config.qqbot_public()

    @app.post("/api/qqbot/config")
    async def save_qqbot_config(
        payload: QQBotConfigInput,
        _: None = Depends(require_admin),
    ) -> dict[str, str | bool]:
        try:
            context.runtime_config.save_qqbot(payload.app_id, payload.app_secret, payload.base_url)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return context.runtime_config.qqbot_public()

    @app.get("/api/settings/poll")
    async def poll_config(_: None = Depends(require_admin)) -> dict[str, int]:
        return context.runtime_config.poll_defaults()

    @app.post("/api/settings/poll")
    async def save_poll_config(
        payload: PollConfigInput,
        _: None = Depends(require_admin),
    ) -> dict[str, int]:
        return context.runtime_config.save_poll_defaults(
            payload.bilibili_poll_interval_seconds,
            payload.yysls_poll_interval_seconds,
        )

    @app.post("/api/qqbot/test")
    async def test_qqbot(
        payload: QQBotTestInput,
        _: None = Depends(require_admin),
    ) -> dict[str, str]:
        config = context.runtime_config.qqbot()
        if not config.app_id or not config.app_secret:
            raise HTTPException(status_code=400, detail="尚未配置 QQBot AppID/AppSecret")
        client = QQBotClient(config.base_url, config.app_id, config.app_secret)
        try:
            result = await client.send_text(
                QQTarget(payload.scene_type.value, payload.target_openid), payload.content
            )
            return {"message_id": result.message_id, "trace_id": result.trace_id}
        finally:
            await client.aclose()

    @app.post("/api/monitor/poll")
    async def manual_poll(_: None = Depends(require_admin)) -> dict[str, Any]:
        results = await context.monitor().poll_once(force=True)
        return {"results": [result.__dict__ for result in results]}

    return app


def _require_credentials_service(context: ApplicationContext):
    if not context.credentials:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="未配置 APP_ENCRYPTION_KEY，不能保存或使用 B站登录凭据",
        )
    return context.credentials


async def _load_bilibili_status(context: ApplicationContext) -> dict[str, Any]:
    if not context.credentials:
        return {
            "status": "not_configured",
            "last_error": "未配置 APP_ENCRYPTION_KEY",
            "username": "",
        }
    result = context.credentials.status()
    if result.get("status") != "logged_in" or result.get("username"):
        return result

    try:
        credential = context.credentials.load()
        username = await _get_bilibili_user_name(context, credential or {})
    except (BilibiliDependencyError, ValueError, TypeError):
        return result
    if not username or not credential:
        return result

    credential["username"] = username
    context.credentials.save(credential, status="logged_in")
    return context.credentials.status()


async def _enrich_subscription_names(
    context: ApplicationContext,
    subscriptions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for subscription in subscriptions:
        uid = int(subscription["uid"])
        current_name = str(subscription.get("display_name") or "").strip()
        if current_name and current_name != str(uid):
            continue
        try:
            display_name = await _get_bilibili_user_name(context, uid)
        except BilibiliDependencyError:
            continue
        if display_name:
            context.subscriptions.update_display_name(uid, display_name)
            subscription["display_name"] = display_name
    return subscriptions


async def _try_get_bilibili_user_name(
    context: ApplicationContext,
    credential: dict[str, Any],
) -> str:
    try:
        uid = int(credential.get("dedeuserid") or 0)
        if not uid:
            return ""
        return await _get_bilibili_user_name(context, uid, credential)
    except (BilibiliDependencyError, TypeError, ValueError):
        return ""


async def _get_bilibili_user_name(
    context: ApplicationContext,
    uid: int,
    credential: dict[str, Any] | None = None,
) -> str:
    if credential is None and context.credentials:
        try:
            credential = context.credentials.load()
        except Exception:
            credential = None
    client = BilibiliClient(credential=credential)
    user_info = await client.get_user_info(uid)
    return _extract_bilibili_user_name(user_info)


def _extract_bilibili_user_name(user_info: Any) -> str:
    if not isinstance(user_info, dict):
        return ""
    candidates: list[dict[str, Any]] = [user_info]
    nested = user_info.get("data")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        for key in ("name", "uname", "username", "nickname"):
            value = str(candidate.get(key) or "").strip()
            if value:
                return value
    return ""
