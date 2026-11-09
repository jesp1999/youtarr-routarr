"""Discord ping when downloads wait too long. Ping only: this never files anything."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .db import connect, log_event, now_iso
from .media import scan_queue

log = logging.getLogger(__name__)
RENOTIFY_AFTER = timedelta(hours=24)
CHECK_EVERY_SECONDS = 3600


def record_seen(conn: sqlite3.Connection, video_ids: list[str]) -> None:
    """Age is tracked here because youtarr sets file mtimes to the upload date."""
    with conn:
        conn.executemany("INSERT OR IGNORE INTO queue_seen (video_id, first_seen) VALUES (?, ?)",
                         [(video_id, now_iso()) for video_id in video_ids])


def aged_ids(conn: sqlite3.Connection, video_ids: list[str], days: int, now: datetime) -> list[str]:
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
    seen = {row["video_id"]: row["first_seen"] for row in conn.execute("SELECT * FROM queue_seen")}
    return [video_id for video_id in video_ids if video_id in seen and seen[video_id] <= cutoff]


def _post(url: str, content: str) -> None:
    request = urllib.request.Request(
        url, data=json.dumps({"content": content}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "youtarr-routarr"})
    with urllib.request.urlopen(request, timeout=15):
        pass


def check_queue(cfg: Config, conn: sqlite3.Connection, now: datetime | None = None,
                post: Callable[[str, str], None] = _post) -> bool:
    """Ping at most once per RENOTIFY_AFTER. Returns True when a ping was sent."""
    if not cfg.discord_webhook_url:
        return False
    now = now or datetime.now(timezone.utc)
    filed = {row["video_id"] for row in conn.execute("SELECT video_id FROM filings")}
    pending = [item.video_id for item in scan_queue(cfg.queue_dir, cfg.info_dir)
               if item.video_id not in filed]
    record_seen(conn, pending)
    aged = aged_ids(conn, pending, cfg.queue_ping_days, now)
    if not aged:
        return False
    row = conn.execute("SELECT value FROM kv WHERE key = 'last_ping'").fetchone()
    if row and now - datetime.fromisoformat(row["value"]) < RENOTIFY_AFTER:
        return False
    post(cfg.discord_webhook_url,
         f"YoutarrRoutarr: {len(aged)} of {len(pending)} queued downloads have waited more than "
         f"{cfg.queue_ping_days} days.")
    with conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('last_ping', ?)",
                     (now.isoformat(timespec="seconds"),))
        log_event(conn, None, "ping", {"aged": len(aged), "pending": len(pending)})
    return True


def start_worker(cfg: Config) -> threading.Thread:
    def loop() -> None:
        while True:
            try:
                conn = connect(cfg.db_path)
                try:
                    check_queue(cfg, conn)
                finally:
                    conn.close()
            except Exception:
                log.exception("Queue age check failed")
            time.sleep(CHECK_EVERY_SECONDS)

    thread = threading.Thread(target=loop, name="queue-ping", daemon=True)
    thread.start()
    return thread
