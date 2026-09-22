from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bilibili_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    encrypted_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS bilibili_subscriptions (
    uid INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
    filter_types_json TEXT NOT NULL DEFAULT '[]',
    filter_keywords_json TEXT NOT NULL DEFAULT '[]',
    last_dynamic_id TEXT NOT NULL DEFAULT '',
    recent_dynamic_ids_json TEXT NOT NULL DEFAULT '[]',
    next_poll_at TEXT,
    last_success_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS watch_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 600,
    next_poll_at TEXT,
    last_success_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(source_type, source_key)
);

CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    content_text TEXT NOT NULL DEFAULT '',
    content_html TEXT NOT NULL DEFAULT '',
    render_payload_json TEXT NOT NULL DEFAULT '{}',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    source_url TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    content_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(source_type, source_key, external_id)
);

CREATE TABLE IF NOT EXISTS delivery_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_type TEXT NOT NULL,
    target_openid TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    message_mode TEXT NOT NULL DEFAULT 'image',
    render_mode TEXT NOT NULL DEFAULT 'playwright',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scene_type, target_openid)
);

CREATE TABLE IF NOT EXISTS qq_binding_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    scene_type TEXT NOT NULL,
    message_mode TEXT NOT NULL DEFAULT 'image',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    target_openid TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_qq_binding_codes_status_expiry
    ON qq_binding_codes(status, expires_at);

CREATE TABLE IF NOT EXISTS delivery_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_item_id INTEGER NOT NULL REFERENCES content_items(id),
    delivery_target_id INTEGER NOT NULL REFERENCES delivery_targets(id),
    status TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    qq_message_id TEXT NOT NULL DEFAULT '',
    qq_trace_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(content_item_id, delivery_target_id)
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def loads(value: str, default: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default
