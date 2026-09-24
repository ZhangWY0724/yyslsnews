import json
import logging

import httpx
import pytest

from yysls_news.delivery.qqbot.client import QQBotClient, QQTarget


@pytest.mark.parametrize(
    ("msg_id", "expected_body"),
    [
        ("", {"msg_type": 0, "content": "hello"}),
        (
            "incoming-message",
            {"msg_type": 0, "content": "hello", "msg_id": "incoming-message", "msg_seq": 1},
        ),
    ],
)
@pytest.mark.asyncio
async def test_qqbot_fetches_token_and_sends_group_text(msg_id, expected_body) -> None:
    calls: list[tuple[str, str, dict]] = []
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers))
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(
                200,
                json={"access_token": "token-1", "expires_in": 7200},
            )
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "message-1", "timestamp": "now"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = QQBotClient(
        "https://api.bot.qq.com",
        "app-id",
        "app-secret",
        http_client=http_client,
    )
    try:
        result = await client.send_text(QQTarget("group", "group-openid"), "hello", msg_id=msg_id)
    finally:
        await client.aclose()

    assert result.message_id == "message-1"
    assert calls[0][1].endswith("/app/getAppAccessToken")
    assert calls[1][1].endswith("/v2/groups/group-openid/messages")
    assert calls[1][2]["authorization"] == "QQBot token-1"
    assert bodies == [expected_body]


@pytest.mark.asyncio
async def test_qqbot_upload_image_completes_chunked_upload_with_upload_id_only(
    tmp_path, caplog
) -> None:
    image = tmp_path / "news.png"
    image.write_bytes(b"image-bytes")
    requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "cos.example":
            assert request.method == "PUT"
            assert request.content == b"image-bytes"
            return httpx.Response(200)
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, path, body))
        if path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 7200})
        if path.endswith("/upload_prepare"):
            return httpx.Response(
                200,
                json={
                    "upload_id": "upload-1",
                    "block_size": 1024,
                    "parts": [{"index": 1, "presigned_url": "https://cos.example/part-1"}],
                },
            )
        if path.endswith("/upload_part_finish"):
            assert body["upload_id"] == "upload-1"
            assert body["part_index"] == 1
            assert body["block_size"] == len(b"image-bytes")
            return httpx.Response(200, json={})
        if path.endswith("/files"):
            assert body == {"upload_id": "upload-1"}
            return httpx.Response(200, json={"file_info": "file-info-1"})
        if path.endswith("/messages"):
            assert body == {"msg_type": 7, "media": {"file_info": "file-info-1"}}
            return httpx.Response(200, json={"id": "message-1", "timestamp": "now"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = QQBotClient(
        "https://api.bot.qq.com",
        "app-id",
        "app-secret",
        http_client=http_client,
    )
    try:
        with caplog.at_level(logging.INFO, logger="yysls_news.delivery.qqbot.client"):
            result = await client.send_image(QQTarget("user", "user-openid"), image)
    finally:
        await client.aclose()

    assert result.message_id == "message-1"
    assert requests[-2][1].endswith("/files")
    assert requests[-1][1].endswith("/messages")
    assert "QQBot 图片上传完成" in caplog.text
    assert "https://cos.example/part-1" not in caplog.text
    assert "token-1" not in caplog.text
