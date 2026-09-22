from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
LOGIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
CROSS_DOMAIN_HOSTS = {"passport.biligame.com", "passport.bilibili.com"}


class BilibiliLoginError(RuntimeError):
    """B站扫码登录错误。"""


@dataclass(frozen=True)
class QrCodeInfo:
    session_id: str
    url: str
    qrcode_key: str
    image_data_url: str


@dataclass(frozen=True)
class LoginPollResult:
    session_id: str
    code: int
    message: str
    credential: dict[str, Any] | None = None

    @property
    def is_done(self) -> bool:
        return self.code == 0 and self.credential is not None

    @property
    def is_expired(self) -> bool:
        return self.code == 86038


def build_credential(cookies: dict[str, str], refresh_token: str = "") -> dict[str, Any]:
    return {
        "sessdata": cookies.get("SESSDATA", ""),
        "bili_jct": cookies.get("bili_jct", ""),
        "buvid3": cookies.get("buvid3"),
        "buvid4": cookies.get("buvid4"),
        "dedeuserid": cookies.get("DedeUserID", ""),
        "ac_time_value": refresh_token,
    }


def credential_is_complete(credential: dict[str, Any] | None) -> bool:
    required = ("sessdata", "bili_jct", "dedeuserid")
    return bool(credential) and all(str(credential.get(key) or "") for key in required)


class BilibiliQrLoginClient:
    """服务端 B站扫码登录客户端；二维码会话只保存在当前进程内。"""

    def __init__(self, timeout_seconds: float = 10, proxy: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.proxy = proxy.strip() or None
        self._sessions: dict[str, dict[str, Any]] = {}

    async def generate(self) -> QrCodeInfo:
        try:
            async with self._client() as client:
                response = await client.get(QR_GENERATE_URL)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliLoginError("请求B站二维码接口失败，请检查网络或代理配置") from exc
        if payload.get("code") != 0:
            raise BilibiliLoginError(payload.get("message") or "生成B站二维码失败")
        data = payload.get("data") or {}
        url = str(data.get("url") or "")
        qrcode_key = str(data.get("qrcode_key") or "")
        if not url or not qrcode_key:
            raise BilibiliLoginError("B站二维码响应缺少 url 或 qrcode_key")
        session_id = os.urandom(16).hex()
        self._sessions[session_id] = {"qrcode_key": qrcode_key, "url": url}
        return QrCodeInfo(
            session_id=session_id,
            url=url,
            qrcode_key=qrcode_key,
            image_data_url=self._qrcode_data_url(url),
        )

    async def poll(self, session_id: str) -> LoginPollResult:
        session = self._sessions.get(session_id)
        if not session:
            raise BilibiliLoginError("扫码会话不存在或已过期")
        try:
            async with self._client() as client:
                response = await client.get(
                    QR_POLL_URL,
                    params={"qrcode_key": session["qrcode_key"]},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    raise BilibiliLoginError(payload.get("message") or "轮询B站扫码状态失败")
                data = payload.get("data") or {}
                code = int(data.get("code", -1))
                message = str(data.get("message") or "")
                if code != 0:
                    return LoginPollResult(session_id, code, message)
                credential = await self._extract_credential(client, data)
        except BilibiliLoginError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliLoginError("请求B站扫码状态失败，请检查网络或代理配置") from exc
        if not credential_is_complete(credential):
            raise BilibiliLoginError("扫码成功但未获取完整 B站 Credential")
        self._sessions.pop(session_id, None)
        return LoginPollResult(session_id, 0, message, credential)

    def discard(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _client(self) -> httpx.AsyncClient:
        proxies = self.proxy if self.proxy else None
        return httpx.AsyncClient(
            headers=LOGIN_HEADERS,
            timeout=self.timeout_seconds,
            follow_redirects=True,
            proxy=proxies,
        )

    async def _extract_credential(
        self, client: httpx.AsyncClient, data: dict[str, Any]
    ) -> dict[str, Any]:
        login_url = str(data.get("url") or "")
        refresh_token = str(data.get("refresh_token") or "")
        parsed = urlparse(login_url)
        if parsed.hostname in CROSS_DOMAIN_HOSTS and parsed.path.endswith("/crossDomain"):
            try:
                response = await client.get(login_url)
            except httpx.HTTPError as exc:
                raise BilibiliLoginError("获取B站登录凭据失败，请检查网络或代理配置") from exc
            cookies = _select_cookie_values(client, response.headers.get_list("set-cookie"))
            return build_credential(cookies, refresh_token)
        query = parse_qs(parsed.query)
        cookies = {
            "SESSDATA": query.get("SESSDATA", [""])[0],
            "bili_jct": query.get("bili_jct", [""])[0],
            "buvid3": query.get("buvid3", [""])[0],
            "buvid4": query.get("buvid4", [""])[0],
            "DedeUserID": query.get("DedeUserID", [""])[0],
        }
        return build_credential(cookies, refresh_token)

    @staticmethod
    def _qrcode_data_url(url: str) -> str:
        try:
            import qrcode
        except ImportError as exc:
            raise BilibiliLoginError("未安装 qrcode，无法生成二维码图片") from exc
        image = qrcode.make(url)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"


def _select_cookie_values(
    client: httpx.AsyncClient,
    set_cookie_headers: list[str],
) -> dict[str, str]:
    """从不同域名/路径的同名 Cookie 中选择 B站主域凭据。

    httpx.Cookies 不能直接转成 dict：当 passport 和 bilibili 域同时存在
    SESSDATA 时会抛出 CookieConflict。这里保留域名信息后再选择主域 Cookie。
    """
    candidates: list[tuple[str, str, str, str]] = []
    for cookie in client.cookies.jar:
        candidates.append((cookie.name, cookie.value, cookie.domain, cookie.path))
    for header in set_cookie_headers:
        parsed_cookie = SimpleCookie()
        parsed_cookie.load(header)
        for name, morsel in parsed_cookie.items():
            candidates.append(
                (
                    name,
                    morsel.value,
                    str(morsel["domain"] or ""),
                    str(morsel["path"] or "/"),
                )
            )

    selected: dict[str, str] = {}
    for name in ("SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID"):
        matching = [candidate for candidate in candidates if candidate[0] == name]
        if matching:
            _, value, _, _ = max(matching, key=lambda item: _cookie_priority(item[2], item[3]))
            selected[name] = value
    return selected


def _cookie_priority(domain: str, path: str) -> tuple[int, int, int]:
    normalized = domain.lower().lstrip(".")
    if normalized == "bilibili.com":
        domain_score = 30
    elif normalized.endswith(".bilibili.com"):
        domain_score = 20
    elif normalized.endswith("biligame.com"):
        domain_score = 10
    else:
        domain_score = 0
    return domain_score, int(path == "/"), len(normalized)
