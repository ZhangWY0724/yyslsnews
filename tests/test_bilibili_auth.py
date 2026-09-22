import httpx
import pytest

from yysls_news.collectors.bilibili.auth import BilibiliQrLoginClient, _select_cookie_values


@pytest.mark.asyncio
async def test_select_cookie_values_prefers_bilibili_main_domain() -> None:
    async with httpx.AsyncClient() as client:
        client.cookies.set("SESSDATA", "passport-value", domain="passport.bilibili.com", path="/")
        client.cookies.set("SESSDATA", "main-value", domain=".bilibili.com", path="/")

        selected = _select_cookie_values(client, [])

    assert selected["SESSDATA"] == "main-value"


def test_qrcode_data_url_is_available_on_login_client() -> None:
    value = BilibiliQrLoginClient._qrcode_data_url("https://example.com/login")

    assert value.startswith("data:image/png;base64,")
