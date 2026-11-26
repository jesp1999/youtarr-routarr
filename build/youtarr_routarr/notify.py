"""Discord messages: a ping when downloads wait too long, and an alert when media is added.

Nothing here files anything, and a Discord failure never affects a filing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import Config
from .db import connect, log_event, now_iso
from .media import scan_queue

if TYPE_CHECKING:
    from .filing import Entry, Plan

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


# --- "Added" alerts, laid out like Seerr's: title, description, fields, image, timestamp ---------

COLORS = {"show": 0x3B82F6, "movie": 0xE5A00D, "video": 0x8B5CF6, "music": 0x22C55E}
DESCRIPTION_MAX = 350
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # Discord's upload limit
EMBEDS_PER_MESSAGE = 10  # Discord's limit, and each embed can carry one image


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _library_name(entry: "Entry") -> str:
    """The library's folder name, with underscores read as spaces."""
    return entry.root.name.replace("_", " ")


def _episode_code(entry: "Entry") -> str:
    return f"S{entry.season:02d}E{entry.episode:02d}"


def _embed(kind: str, title: str, description: str, fields: list[tuple[str, str]], entry: "Entry",
           now: datetime) -> dict:
    item = entry.item
    embed = {
        "title": _clip(title, 250),
        "description": _clip(description, DESCRIPTION_MAX) if description else "",
        "color": COLORS[kind],
        "fields": [{"name": name, "value": _clip(value, 200), "inline": True}
                   for name, value in fields if value],
        "timestamp": now.isoformat(timespec="seconds"),
    }
    if item.has_real_id:
        embed["url"] = f"https://www.youtube.com/watch?v={item.video_id}"
    return embed


def added_embeds(plan: "Plan", now: datetime | None = None) -> list[tuple[dict, Path | None]]:
    """One embed per filed video, or one per show when a batch files several episodes of it."""
    now = now or datetime.now(timezone.utc)
    out: list[tuple[dict, Path | None]] = []
    episodes: dict[str, list["Entry"]] = {}
    for entry in plan.entries:
        if entry.cls.type == "show":
            episodes.setdefault(entry.show.name.casefold(), []).append(entry)
    done: set[str] = set()
    for entry in plan.entries:
        item, cls = entry.item, entry.cls
        if cls.type == "show":
            key = entry.show.name.casefold()
            group = episodes[key]
            if len(group) > 1:
                if key in done:
                    continue
                done.add(key)
                lines = [f"{_episode_code(e)} · {e.cls.title}" for e in group]
                shown = lines[:8] + ([f"…and {len(lines) - 8} more"] if len(lines) > 8 else [])
                embed = {**_embed("show", f"Episodes Added: {entry.show.name} ({len(group)})", "",
                                  [("Channel", item.channel), ("Library", _library_name(entry))], entry, now),
                         "description": "\n".join(shown)}
                out.append((embed, item.thumb))
                continue
            embed = _embed("show", f"Episode Added: {entry.show.name} {_episode_code(entry)} - {cls.title}",
                           item.plot, [("Channel", item.channel), ("Library", _library_name(entry))], entry, now)
        elif cls.type == "movie":
            embed = _embed("movie", f"Movie Added: {cls.title} ({cls.year})", item.plot,
                           [("Channel", item.channel), ("Library", _library_name(entry))], entry, now)
        elif cls.type == "music":
            embed = _embed("music", f"Audio Added: {cls.artist} - {cls.title}", "",
                           [("Album", cls.album or ""), ("Track", str(cls.track or "")),
                            ("Channel", item.channel), ("Library", _library_name(entry))], entry, now)
        else:
            embed = _embed("video", f"Video Added: {cls.name or cls.title}", item.plot,
                           [("Channel", item.channel), ("Library", _library_name(entry))], entry, now)
        out.append((embed, item.thumb))
    return out


def _post_message(url: str, embeds: list[dict], images: list[tuple[str, bytes]]) -> None:
    """POST embeds, uploading images as attachments that the embeds reference by name."""
    payload = json.dumps({"embeds": embeds}).encode("utf-8")
    boundary = uuid.uuid4().hex
    body = bytearray()

    def part(headers: str, data: bytes) -> None:
        body.extend(f"--{boundary}\r\n{headers}\r\n\r\n".encode() + data + b"\r\n")

    part('Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json', payload)
    for index, (name, data) in enumerate(images):
        part(f'Content-Disposition: form-data; name="files[{index}]"; filename="{name}"\r\n'
             "Content-Type: application/octet-stream", data)
    body.extend(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "youtarr-routarr"})
    with urllib.request.urlopen(request, timeout=15):
        pass


def notify_added(cfg: Config, plan: "Plan", post: Callable[[str, list[dict], list[tuple[str, bytes]]], None] = _post_message,
                 now: datetime | None = None) -> bool:
    """Send an alert for what a plan just filed. Never raises. Returns True when everything was sent."""
    if not cfg.discord_added_webhook_url:
        return False
    try:
        built = added_embeds(plan, now)
        for start in range(0, len(built), EMBEDS_PER_MESSAGE):
            embeds, images = [], []
            for embed, thumb in built[start:start + EMBEDS_PER_MESSAGE]:
                data = None
                if thumb:
                    try:
                        if thumb.stat().st_size <= MAX_IMAGE_BYTES:
                            data = thumb.read_bytes()
                    except OSError:
                        pass
                if data:
                    name = f"image{len(images)}{thumb.suffix.lower()}"
                    images.append((name, data))
                    embed["image"] = {"url": f"attachment://{name}"}
                embeds.append(embed)
            post(cfg.discord_added_webhook_url, embeds, images)
        return True
    except Exception:
        log.exception("Discord added-alert failed")
        return False
