"""SQLite 持久化层。"""
from __future__ import annotations

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE,
    granularity    TEXT NOT NULL CHECK (granularity IN ('daily', 'hourly')),
    config_version INTEGER NOT NULL DEFAULT 1,
    description    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS dependencies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    downstream_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    upstream_id   INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    offset_days   INTEGER NOT NULL DEFAULT 0 CHECK (offset_days >= 0),
    UNIQUE (downstream_id, upstream_id)
);

CREATE TABLE IF NOT EXISTS run_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    partition      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    config_version INTEGER NOT NULL CHECK (config_version >= 1),
    finished_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_task_part
    ON run_records (task_id, partition, finished_at);

CREATE TABLE IF NOT EXISTS plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    target_name   TEXT NOT NULL,
    biz_date      TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('generated', 'rejected')),
    reject_reason TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    items_json    TEXT NOT NULL DEFAULT '[]',
    summary_json  TEXT NOT NULL DEFAULT '{}'
);
"""


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
