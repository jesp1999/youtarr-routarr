"""View data for the web interface. Reads only: the filing rules live in filing.py."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import LIBRARY_LABELS, Config
from .filing import Plan, undo_blocker
from .media import QueueItem, sort_key

TYPE_LABELS = {"show": "Show", "movie": "Movie", "video": "Video", "music": "Audio"}
UNTOUCHED = "The download in the queue folder is never touched."
FOOTNOTES = {
    "link": f"Hardlinked — takes no extra space. {UNTOUCHED}",
    "audio": f"A new file is written. {UNTOUCHED}",
    "bulk": "Hardlinked — no extra space. Specials never set the show’s year, poster or plot.",
}
# Short names for the files listed beside each episode in a bulk preview.
SIDECARS = {"nfo": "nfo", "thumbnail": "thumb", "subtitle": "subtitles", "fanart": "fanart"}
MAIN_ROLES = ("video", "audio")


def groups(items: list[QueueItem]) -> list[tuple[str, list[QueueItem]]]:
    """Channels in alphabetical order, each channel's downloads in filing order."""
    found: dict[str, list[QueueItem]] = {}
    for item in sorted(items, key=sort_key):
        found.setdefault(item.channel, []).append(item)
    return sorted(found.items(), key=lambda pair: pair[0].casefold())


def display_order(items: list[QueueItem]) -> list[str]:
    return [item.video_id for _channel, members in groups(items) for item in members]


def next_after(order: list[str], done: list[str]) -> str | None:
    """The first download after the ones just handled, wrapping to the top."""
    positions = [order.index(video_id) for video_id in done if video_id in order]
    start = max(positions) + 1 if positions else 0
    return next((video_id for video_id in order[start:] + order[:start] if video_id not in done), None)


def ages(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    """Whole days each download has been in the queue."""
    return {row["video_id"]: max(0, (now - datetime.fromisoformat(row["first_seen"])).days)
            for row in conn.execute("SELECT video_id, first_seen FROM queue_seen")}


def preview(plan: Plan, bulk: bool) -> dict | None:
    """What a plan would write, taken from the very operations that filing runs."""
    if not plan.entries or not plan.ok:
        return None
    root = plan.entries[0].root

    def line(op, note: str) -> dict:
        path = op.dst.relative_to(root) if op.dst.is_relative_to(root) else op.dst
        return {"path": path.as_posix(), "note": note, "main": op.role in MAIN_ROLES}

    lines = [line(op, "if new") for show in plan.shows.values() for op in show.ops]
    for entry in plan.entries:
        for op in sorted(entry.ops, key=lambda op: op.role not in MAIN_ROLES):
            if op.role in MAIN_ROLES:
                note = "audio copied + tagged" if op.kind == "extract" else "hardlink"
                if bulk:
                    extras = dict.fromkeys(SIDECARS.get(o.role, o.role) for o in entry.ops
                                           if o.role not in MAIN_ROLES)
                    note = "+ " + ", ".join(extras) if extras else note
                lines.append(line(op, note))
            elif not bulk:
                lines.append(line(op, ""))
    return {"root": f"{root.as_posix()}/", "lines": lines, "count": len(plan.all_ops())}


def history(cfg: Config, conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT f.*, s.name AS show_name FROM filings f LEFT JOIN shows s ON s.id = f.show_id"
        " ORDER BY f.filed_at DESC, f.video_id").fetchall()
    found = []
    for row in rows:
        library = cfg.library_dir(row["type"])
        dest = Path(row["dest_path"])
        if library is not None and dest.is_relative_to(library):
            dest = dest.relative_to(library)
        episode = ""
        if row["type"] == "show":
            episode = f"{row['show_name']} S{row['season']:02d}E{row['episode']:02d}"
        found.append({
            "video_id": row["video_id"], "title": filed_title(row), "episode": episode,
            "when": datetime.fromisoformat(row["filed_at"]).astimezone().strftime("%Y-%m-%d %H:%M"),
            "library": LIBRARY_LABELS[row["type"]], "path": dest.as_posix(), "blocked": undo_blocker(row),
        })
    return found


def filed_title(row: sqlite3.Row) -> str:
    metadata = json.loads(row["metadata"])
    return metadata.get("name") or metadata.get("title") or row["video_id"]


def filed_toast(conn: sqlite3.Connection, video_ids: list[str]) -> dict | None:
    """The message after filing, built from what the database says was filed."""
    rows = [row for video_id in video_ids
            for row in conn.execute("SELECT * FROM filings WHERE video_id = ?", (video_id,))]
    if not rows:
        return None
    if len(rows) == 1:
        return {"text": f"Filed “{filed_title(rows[0])}”.", "undo": rows[0]["video_id"]}
    return {"text": f"Filed {len(rows)} items.", "undo": None}
