import logging

from yysls_news.services.runtime_logs import RuntimeLogBuffer, RuntimeLogHandler


def _record(name: str, message: str) -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, message, (), None)


def test_runtime_logs_read_oldest_pending_entries_without_gaps() -> None:
    buffer = RuntimeLogBuffer(max_entries=3)
    for index in range(5):
        buffer.append(_record("yysls_news.test", f"事件 {index}"))

    first_page = buffer.read(after_id=0, limit=2)
    second_page = buffer.read(after_id=first_page[-1]["id"], limit=2)

    assert [entry["id"] for entry in first_page] == [3, 4]
    assert [entry["id"] for entry in second_page] == [5]


def test_runtime_logs_ignore_log_polling_access_requests() -> None:
    buffer = RuntimeLogBuffer()
    handler = RuntimeLogHandler(buffer)

    handler.handle(_record("uvicorn.access", '127.0.0.1 - "GET /api/logs?after_id=0 HTTP/1.1" 200'))
    handler.handle(_record("uvicorn.access", '127.0.0.1 - "GET /health HTTP/1.1" 200'))
    handler.handle(_record("httpx", "第三方请求详情"))
    handler.handle(_record("yysls_news.services.delivery", "推送任务开始"))

    assert buffer.latest_id() == 1
    assert buffer.read()[0]["message"] == "推送任务开始"


def test_runtime_logs_redact_credentials_and_signed_urls() -> None:
    buffer = RuntimeLogBuffer()
    buffer.append(
        _record(
            "yysls_news.test",
            "Authorization: QQBot token-secret SESSDATA=cookie-secret "
            "https://cos.example/upload?sign=signed-secret",
        )
    )

    message = buffer.read()[0]["message"]
    assert "token-secret" not in message
    assert "cookie-secret" not in message
    assert "signed-secret" not in message
    assert "[REDACTED]" in message
