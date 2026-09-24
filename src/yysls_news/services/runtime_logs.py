from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

_AUTH_RE = re.compile(
    r"(?i)(\bAuthorization[\"']?\s*[:=]\s*[\"']?(?:(?:QQBot|Bearer)\s+)?)[^\s,;\"']+"
)
_SECRET_RE = re.compile(
    r"(?i)(\b(?:access_token|app_secret|clientSecret|sessdata|bili_jct|"
    r"dedeuserid|ac_time_value|buvid3|buvid4|app_encryption_key|"
    r"presigned_url|cookie)\b[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+"
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _redact_sensitive(message: str) -> str:
    message = _URL_RE.sub(
        lambda match: (
            match.group(0).split("?", 1)[0] + "?[REDACTED]"
            if "?" in match.group(0)
            else match.group(0)
        ),
        message,
    )
    message = _AUTH_RE.sub(r"\1[REDACTED]", message)
    return _SECRET_RE.sub(r"\1[REDACTED]", message)


class RuntimeLogBuffer:
    """保存当前进程最近的日志，供管理页面实时查看。"""

    def __init__(self, max_entries: int = 5000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=max(1, max_entries))
        self._lock = threading.Lock()
        self._next_id = 1

    def append(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self._format_exception(record)}"
        message = _redact_sensitive(message)
        with self._lock:
            entry = {
                "id": self._next_id,
                "timestamp": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).astimezone().isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": message,
            }
            self._entries.append(entry)
            self._next_id += 1

    def read(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            entries = [entry for entry in self._entries if int(entry["id"]) > after_id]
        return entries[: max(1, limit)]

    def latest_id(self) -> int:
        with self._lock:
            return self._next_id - 1

    @staticmethod
    def _format_exception(record: logging.LogRecord) -> str:
        formatter = logging.Formatter()
        return formatter.formatException(record.exc_info) if record.exc_info else ""


class RuntimeLogHandler(logging.Handler):
    def __init__(self, buffer: RuntimeLogBuffer) -> None:
        super().__init__(level=logging.INFO)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not (
                record.name == "yysls_news"
                or record.name.startswith("yysls_news.")
                or record.name == "uvicorn"
                or record.name.startswith("uvicorn.")
            ):
                return
            if record.name == "uvicorn.access" and re.search(
                r'"(?:GET|HEAD) /(?:api/logs|health)(?:[? /])', record.getMessage()
            ):
                return
            seen_buffers = getattr(record, "_runtime_log_buffers", set())
            buffer_id = id(self.buffer)
            if buffer_id in seen_buffers:
                return
            seen_buffers.add(buffer_id)
            record._runtime_log_buffers = seen_buffers
            self.buffer.append(record)
        except Exception:
            self.handleError(record)


def attach_runtime_log_handler(buffer: RuntimeLogBuffer) -> RuntimeLogHandler:
    """把应用和 Uvicorn 日志接入同一个页面日志缓冲区。"""
    handler = RuntimeLogHandler(buffer)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(logger_name).addHandler(handler)
    return handler
