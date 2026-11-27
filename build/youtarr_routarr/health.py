"""The read-only Settings & health page. Secrets are reported as set or not, never shown."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import LIBRARY_LABELS, Config

CARD_LABELS = {**LIBRARY_LABELS, "music": "Music"}


def _mount_point(path: Path) -> str:
    """The mount a path sits on. Hardlinks cannot cross mounts, even on one filesystem."""
    real = os.path.realpath(path)
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return str(os.stat(real).st_dev)
    points = [line.split()[4].encode().decode("unicode_escape") for line in lines]
    inside = [p for p in points if real == p or real.startswith(p.rstrip("/") + "/")]
    return max(inside, key=len, default="/")


def _check(state: str, title: str, detail: str) -> dict:
    return {"state": state, "title": title, "detail": detail}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def library_cards(cfg: Config, conn: sqlite3.Connection) -> list[dict]:
    filed = dict(conn.execute("SELECT type, COUNT(*) FROM filings GROUP BY type").fetchall())
    shows = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    cards = []
    for kind in cfg.enabled_types():
        count = filed.get(kind, 0)
        if kind == "show":
            title = f"{_plural(shows, 'show')} · {_plural(count, 'episode')}"
        else:
            title = _plural(count, {"movie": "movie", "video": "video", "music": "track"}[kind])
        cards.append({"label": CARD_LABELS[kind], "title": title, "path": str(cfg.library_dir(kind))})
    return cards


def checks(cfg: Config, conn: sqlite3.Connection, waiting: int) -> list[dict]:
    found = []
    libraries = [cfg.library_dir(kind) for kind in cfg.enabled_types()]

    missing = [str(p) for p in (cfg.queue_dir, *libraries) if not p.is_dir()]
    if missing:
        found.append(_check("Note", "Queue and libraries share one mount", f"Missing: {', '.join(missing)}"))
    else:
        home = (_mount_point(cfg.queue_dir), cfg.queue_dir.stat().st_dev)
        apart = [str(p) for p in libraries if (_mount_point(p), p.stat().st_dev) != home]
        found.append(_check(
            "Note" if apart else "OK", "Queue and libraries share one mount",
            f"Hardlinks will fail for: {', '.join(apart)}" if apart else f"Hardlinks work under {home[0]}."))

    if cfg.info_dir is None:
        found.append(_check("Off", "Youtarr info folder", "Not set, so titles come from file names and NFOs."))
    elif not cfg.info_dir.is_dir():
        found.append(_check("Note", "Youtarr info folder", f"{cfg.info_dir} is missing."))
    else:
        read_only = bool(os.statvfs(cfg.info_dir).f_flag & os.ST_RDONLY)
        found.append(_check(
            "OK" if read_only else "Note",
            "Youtarr info folder mounted read-only" if read_only else "Youtarr info folder is writable",
            str(cfg.info_dir)))

    user = f"{os.getuid()}:{os.getgid()}"
    locked = [str(p) for p in (cfg.db_path.parent, *libraries) if not os.access(p, os.W_OK | os.X_OK)]
    found.append(_check(
        "Note" if locked else "OK",
        f"Some folders are not writable by {user}" if locked else f"data/ and libraries writable by {user}",
        f"Check: {', '.join(locked)}" if locked else "Use the same user and group IDs as Youtarr."))

    # Startup refuses an overlap, so a running app has none.
    found.append(_check("OK", "No library overlaps the queue folder",
                        "Nothing appears in your media server until filed."))

    row = conn.execute("SELECT value FROM kv WHERE key = 'last_ping'").fetchone()
    if not cfg.discord_webhook_url:
        ping = "The waiting ping is off."
    elif row:
        ping = f"Last ping {datetime.fromisoformat(row['value']).astimezone():%Y-%m-%d %H:%M}. At most once a day."
    else:
        ping = "No ping sent yet. At most once a day."
    found.append(_check("Note" if waiting else "OK",
                        f"{_plural(waiting, 'download')} waiting over {cfg.queue_ping_days} days", ping))

    if cfg.discord_added_webhook_url:
        found.append(_check("OK", "Filing alerts", "A Discord message is sent when media is filed."))
    else:
        found.append(_check("Off", "Filing alerts", "DISCORD_ADDED_WEBHOOK_URL is not set."))
    return found


def environment(cfg: Config) -> list[tuple[str, str, str]]:
    """(name, value, note) rows, as the app received them."""
    def secret(value: str) -> str:
        return "set" if value else "—"

    rows = [("QUEUE_DIR", str(cfg.queue_dir), "")]
    for kind, name in (("show", "SHOWS_DIR"), ("movie", "MOVIES_DIR"), ("video", "VIDEOS_DIR"),
                       ("music", "MUSIC_DIR")):
        path = cfg.library_dir(kind)
        rows.append((name, str(path) if path else "—", "" if path else "off"))
    rows += [
        ("VIDEOS_SUBDIR", cfg.videos_subdir or "—", ""),
        ("INFO_DIR", str(cfg.info_dir) if cfg.info_dir else "—", ""),
        ("DB_PATH", str(cfg.db_path), ""),
        ("PUID / PGID", f"{os.getuid()} / {os.getgid()}", "the user the app runs as"),
        ("TZ", os.environ.get("TZ") or "—", ""),
        ("DRY_RUN", "1" if cfg.dry_run else "0", "no writes" if cfg.dry_run else "writes allowed"),
        ("QUEUE_PING_DAYS", str(cfg.queue_ping_days), ""),
        ("DISCORD_WEBHOOK_URL", secret(cfg.discord_webhook_url), "waiting ping"),
        ("DISCORD_ADDED_WEBHOOK_URL", secret(cfg.discord_added_webhook_url), "filing alerts"),
        ("BASIC_AUTH_USER", cfg.auth_user or "—", ""),
        ("AUTH_DISABLED", "1" if cfg.auth_disabled else "0", ""),
    ]
    return rows
