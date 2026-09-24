from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from yysls_news.application import ApplicationContext
from yysls_news.collectors.bilibili.auth import BilibiliLoginError
from yysls_news.collectors.bilibili.client import BilibiliClient, BilibiliDependencyError
from yysls_news.delivery.qqbot.client import QQBotApiError, QQBotClient, QQTarget
from yysls_news.domain.models import MessageMode, SceneType
from yysls_news.rendering.renderer import ImageRenderError, PlaywrightRenderer
from yysls_news.security.passwords import hash_password, verify_password
from yysls_news.services.delivery import DeliveryWorker
from yysls_news.services.qq_binding import BINDING_CODE_TTL_SECONDS
from yysls_news.services.runtime_logs import attach_runtime_log_handler

LOGGER = logging.getLogger(__name__)


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


class YyslsSourceUpdateInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=10, max_length=500)
    category: str = "最新"
    enabled: bool = True
    poll_interval_seconds: int = Field(default=600, ge=60)


class DeliveryTargetInput(BaseModel):
    scene_type: SceneType
    target_openid: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="", max_length=200)
    enabled: bool = True
    message_mode: MessageMode = MessageMode.IMAGE


class DeliveryTargetUpdateInput(BaseModel):
    display_name: str = Field(default="", max_length=200)
    enabled: bool = True
    message_mode: MessageMode = MessageMode.IMAGE


class QQBotConfigInput(BaseModel):
    app_id: str = Field(min_length=1, max_length=100)
    app_secret: str = ""
    base_url: str | None = None


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


class HistoricalContentTestPushInput(BaseModel):
    target_id: int = Field(gt=0)


class QQBindingCodeInput(BaseModel):
    scene_type: SceneType
    message_mode: MessageMode = MessageMode.IMAGE


class AdminPasswordChangeInput(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


def create_app(context: ApplicationContext | None = None) -> FastAPI:
    context = context or ApplicationContext.create()
    app = FastAPI(title="资讯监控推送服务", version="0.1.0")
    app.state.context = context
    app.state.runtime_log_handler = attach_runtime_log_handler(context.runtime_logs)
    templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
    basic = HTTPBasic(auto_error=False)

    def require_authenticated_admin(
        credentials: HTTPBasicCredentials | None = Depends(basic),
    ) -> HTTPBasicCredentials:
        stored_password_hash = context.runtime_config.admin_password_hash()
        if not stored_password_hash and not context.settings.admin_password:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="未配置 ADMIN_PASSWORD，管理页面暂不可用",
            )
        if not credentials or not secrets.compare_digest(
            credentials.username, context.settings.admin_username
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="管理页面认证失败",
                headers={"WWW-Authenticate": "Basic"},
            )
        password_valid = (
            verify_password(credentials.password, stored_password_hash)
            if stored_password_hash
            else secrets.compare_digest(credentials.password, context.settings.admin_password)
        )
        if not password_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="管理页面认证失败",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials

    def require_admin(
        credentials: HTTPBasicCredentials = Depends(require_authenticated_admin),
    ) -> HTTPBasicCredentials:
        if not context.runtime_config.admin_password_hash():
            raise HTTPException(
                status_code=status.HTTP_428_PRECONDITION_REQUIRED,
                detail="首次登录必须先修改管理密码",
            )
        return credentials

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "yysls-news"}

    @app.get("/api/logs")
    async def runtime_logs(
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        return {
            "items": context.runtime_logs.read(after_id=after_id, limit=limit),
            "latest_id": context.runtime_logs.latest_id(),
        }

    @app.get("/api/push-history")
    async def push_history(
        limit: int = Query(default=100, ge=1, le=500),
        status_filter: str | None = Query(default=None, alias="status"),
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        allowed_statuses = {"processing", "sent", "failed"}
        if status_filter and status_filter not in allowed_statuses:
            raise HTTPException(status_code=400, detail="无效的推送记录状态")
        return {
            "items": context.push_history.list_recent(limit, status_filter),
            "counts": context.push_history.count_by_status(),
        }

    @app.get("/", response_class=HTMLResponse)
    async def index(
        request: Request,
        _: HTTPBasicCredentials = Depends(require_authenticated_admin),
    ) -> Any:
        if not context.runtime_config.admin_password_hash():
            return templates.TemplateResponse(
                request=request,
                name="password_change.html",
                context={"title": "首次登录 - 修改管理密码"},
            )
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"title": "资讯监控推送服务"},
        )

    @app.post("/api/auth/password")
    async def change_admin_password(
        payload: AdminPasswordChangeInput,
        _: HTTPBasicCredentials = Depends(require_authenticated_admin),
    ) -> dict[str, bool]:
        if payload.new_password != payload.confirm_password:
            raise HTTPException(status_code=400, detail="两次输入的新密码不一致")
        if secrets.compare_digest(payload.new_password, context.settings.admin_password):
            raise HTTPException(status_code=400, detail="新密码不能与初始密码相同")
        context.runtime_config.save_admin_password_hash(hash_password(payload.new_password))
        return {"changed": True}

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
        }

    @app.get("/api/bilibili/subscriptions")
    async def list_subscriptions(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return await _enrich_subscription_names(context, context.subscriptions.list_all())

    async def persist_subscription(payload: BilibiliSubscriptionInput) -> dict[str, Any]:
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

    @app.post("/api/bilibili/subscriptions")
    async def save_subscription(
        payload: BilibiliSubscriptionInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        return await persist_subscription(payload)

    @app.put("/api/bilibili/subscriptions/{uid}")
    async def update_subscription(
        uid: int,
        payload: BilibiliSubscriptionInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        if uid != payload.uid:
            raise HTTPException(status_code=400, detail="路径 UID 与订阅 UID 不一致")
        if not any(int(item["uid"]) == uid for item in context.subscriptions.list_all()):
            raise HTTPException(status_code=404, detail="B站订阅不存在")
        return await persist_subscription(payload)

    @app.delete("/api/bilibili/subscriptions/{uid}")
    async def delete_subscription(uid: int, _: None = Depends(require_admin)) -> dict[str, bool]:
        if not context.subscriptions.delete(uid):
            raise HTTPException(status_code=404, detail="B站订阅不存在")
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

    @app.put("/api/sources/{source_id}")
    async def update_source(
        source_id: int,
        payload: YyslsSourceUpdateInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        if not payload.url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="来源 URL 必须是 HTTP(S) 地址")
        if not context.sources.update(
            source_id=source_id,
            source_type="yysls",
            name=payload.name,
            url=payload.url,
            category=payload.category,
            enabled=payload.enabled,
            poll_interval_seconds=payload.poll_interval_seconds,
        ):
            raise HTTPException(status_code=404, detail="官网来源不存在")
        return next(
            item
            for item in context.sources.list_all("yysls")
            if int(item["id"]) == source_id
        )

    @app.delete("/api/sources/{source_id}")
    async def delete_source(
        source_id: int,
        _: None = Depends(require_admin),
    ) -> dict[str, bool]:
        if not context.sources.delete(source_id, "yysls"):
            raise HTTPException(status_code=404, detail="官网来源不存在")
        return {"deleted": True}

    @app.get("/api/targets")
    async def list_targets(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
        return context.targets.list_all()

    @app.post("/api/contents/{content_id}/test-push")
    async def test_historical_content_push(
        content_id: int,
        payload: HistoricalContentTestPushInput,
        _: None = Depends(require_admin),
    ) -> dict[str, str | int]:
        content = context.contents.get_by_id(content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="历史内容不存在")

        target = next(
            (
                item
                for item in context.targets.list_enabled()
                if int(item["id"]) == payload.target_id
            ),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="推送目标不存在或已停用")

        config = context.runtime_config.qqbot()
        if not config.app_id or not config.app_secret:
            raise HTTPException(status_code=400, detail="尚未配置 QQBot AppID/AppSecret")

        worker = DeliveryWorker(
            tasks=context.tasks,
            push_history=context.push_history,
            runtime_config=context.runtime_config,
            image_renderer=PlaywrightRenderer(
                timeout_ms=int(context.settings.http_timeout_seconds * 1000)
            ),
        )
        try:
            result = await worker.send_test(content, target)
        except ImageRenderError as exc:
            LOGGER.exception("历史内容测试推送图片渲染失败: content_id=%s", content_id)
            raise HTTPException(status_code=502, detail=f"历史内容图片渲染失败：{exc}") from exc
        except QQBotApiError as exc:
            LOGGER.exception("历史内容测试推送 QQBot 发送失败: content_id=%s", content_id)
            raise HTTPException(status_code=502, detail=f"QQBot 测试推送失败：{exc}") from exc
        finally:
            await worker.aclose()
        return {
            "content_id": content_id,
            "target_id": payload.target_id,
            "message_id": result.message_id,
            "trace_id": result.trace_id,
        }

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
            display_name=payload.display_name,
            enabled=payload.enabled,
            message_mode=payload.message_mode,
        )
        return {"id": target_id, **payload.model_dump(mode="json")}

    @app.put("/api/targets/{target_id}")
    async def update_target(
        target_id: int,
        payload: DeliveryTargetUpdateInput,
        _: None = Depends(require_admin),
    ) -> dict[str, Any]:
        if not context.targets.update_settings(
            target_id=target_id,
            display_name=payload.display_name,
            enabled=payload.enabled,
            message_mode=payload.message_mode,
        ):
            raise HTTPException(status_code=404, detail="推送目标不存在")
        return {"id": target_id, **payload.model_dump(mode="json")}

    @app.delete("/api/targets/{target_id}")
    async def delete_target(
        target_id: int,
        _: None = Depends(require_admin),
    ) -> dict[str, bool]:
        if not context.targets.delete(target_id):
            raise HTTPException(status_code=404, detail="推送目标不存在")
        return {"deleted": True}

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

    @app.post("/api/qqbot/test")
    async def test_qqbot(
        payload: QQBotTestInput,
        _: None = Depends(require_admin),
    ) -> dict[str, str]:
        config = context.runtime_config.qqbot()
        if not config.app_id or not config.app_secret:
            raise HTTPException(status_code=400, detail="尚未配置 QQBot AppID/AppSecret")
        target = next(
            (
                item
                for item in context.targets.list_all()
                if item["scene_type"] == payload.scene_type.value
                and item["target_openid"] == payload.target_openid
            ),
            None,
        )
        history_id = context.push_history.create_attempt(
            content_item_id=None,
            delivery_target_id=int(target["id"]) if target else None,
            trigger_type="qqbot_test",
            source_type="",
            title="QQBot 测试消息",
            scene_type=payload.scene_type.value,
            target_openid=payload.target_openid,
            target_display_name=str(target.get("display_name") or "") if target else "",
        )
        LOGGER.info(
            "QQBot 测试消息开始: history=%s target=%s scene=%s",
            history_id,
            target["id"] if target else "unbound",
            payload.scene_type.value,
        )
        client = QQBotClient(config.base_url, config.app_id, config.app_secret)
        try:
            result = await client.send_text(
                QQTarget(payload.scene_type.value, payload.target_openid), payload.content
            )
            context.push_history.mark_sent(history_id, result.message_id, result.trace_id)
            LOGGER.info(
                "QQBot 测试消息完成: history=%s message_id_tail=%s trace_id=%s",
                history_id,
                result.message_id[-8:] or "-",
                result.trace_id or "-",
            )
            return {"message_id": result.message_id, "trace_id": result.trace_id}
        except Exception as exc:
            context.push_history.mark_failed(history_id, f"{type(exc).__name__}: {exc}")
            LOGGER.warning(
                "QQBot 测试消息失败: history=%s error=%s HTTP=%s err_code=%s trace_id=%s",
                history_id,
                type(exc).__name__,
                exc.http_status if isinstance(exc, QQBotApiError) else None,
                exc.err_code if isinstance(exc, QQBotApiError) else None,
                exc.trace_id if isinstance(exc, QQBotApiError) else "-",
            )
            raise
        finally:
            await client.aclose()

    @app.post("/api/monitor/poll")
    async def manual_poll(_: None = Depends(require_admin)) -> dict[str, Any]:
        LOGGER.info("管理员手动轮询开始")
        results = await context.monitor().poll_once(force=True)
        LOGGER.info(
            "管理员手动轮询完成: sources=%s failures=%s",
            len(results),
            sum(bool(result.error) for result in results),
        )
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
