"""SQLite state: what was filed where, so a filing can be undone or reclassified."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    folder TEXT NOT NULL UNIQUE,
    premiered TEXT,
    created_by_app INTEGER NOT NULL,
    manifest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filings (
    video_id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('show', 'movie', 'video')),
    source_path TEXT NOT NULL,
    dest_path TEXT NOT NULL,
    show_id INTEGER REFERENCES shows(id),
    season INTEGER,
    episode INTEGER,
    metadata TEXT NOT NULL,
    manifest TEXT NOT NULL,
    filed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS filings_show ON filings(show_id, season, episode);
CREATE TABLE IF NOT EXISTS queue_seen (
    video_id TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    video_id TEXT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn: sqlite3.Connection, video_id: str | None, action: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO events (at, video_id, action, detail) VALUES (?, ?, ?, ?)",
        (now_iso(), video_id, action, json.dumps(detail, ensure_ascii=False)),
    )
