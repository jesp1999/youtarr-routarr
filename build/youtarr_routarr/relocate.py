"""Move filed shows and movies to new library roots (SHOWS_DIR / MOVIES_DIR) and update the DB.

    python -m youtarr_routarr.relocate --old-shows DIR --old-movies DIR           # dry run
    python -m youtarr_routarr.relocate --old-shows DIR --old-movies DIR --apply

Run inside the container after SHOWS_DIR and MOVIES_DIR point at the new roots. Each folder is
renamed (same filesystem, so inodes, queue hardlinks and undo records stay valid) and every path
in its filings and manifests is rewritten. Only folders this app created are moved: a show filed
into a folder that already existed (e.g. one Sonarr owns) is reported and left in place.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .config import Config
from .db import connect, log_event


def swap(path: str, old: Path, new: Path) -> str:
    text, prefix = str(path), str(old)
    if text == prefix or text.startswith(prefix + "/"):
        return str(new) + text[len(prefix):]
    return text


def swap_manifest(raw: str, old: Path, new: Path) -> str:
    manifest = json.loads(raw)
    for record in manifest.get("files", []):
        record["path"] = swap(record["path"], old, new)
    manifest["dirs"] = [swap(d, old, new) for d in manifest.get("dirs", [])]
    return json.dumps(manifest, ensure_ascii=False)


def _move(conn: sqlite3.Connection, old: Path, new: Path, apply: bool, report: list[str],
          updates: list[tuple[str, tuple]]) -> bool:
    """Rename one folder and apply its DB updates in one transaction; undo the rename on failure."""
    if new.exists() or new.is_symlink():
        report.append(f"CONFLICT {old.name}: {new} already exists; left in place")
        return False
    report.append(f"move     {old}  ->  {new}")
    if not apply:
        return True
    os.rename(old, new)
    try:
        with conn:
            for sql, params in updates:
                conn.execute(sql, params)
    except Exception:
        os.rename(new, old)
        raise
    return True


def relocate(cfg: Config, conn: sqlite3.Connection, old_shows: Path, old_movies: Path,
             apply: bool) -> list[str]:
    report: list[str] = []
    for new_root, old_root in ((cfg.shows_dir, old_shows), (cfg.movies_dir, old_movies)):
        if not new_root.is_dir() or not old_root.is_dir():
            report.append(f"ERROR    missing {new_root if not new_root.is_dir() else old_root}; create it first")
            return report
        if os.stat(new_root).st_dev != os.stat(old_root).st_dev:
            report.append(f"ERROR    {new_root} and {old_root} are on different filesystems; refusing")
            return report

    moved = 0
    for show in conn.execute("SELECT * FROM shows").fetchall():
        old = old_shows / show["folder"]
        new = cfg.shows_dir / show["folder"]
        if not old.is_dir():
            if new.is_dir():
                continue  # already relocated
            report.append(f"MISSING  show folder {old}")
            continue
        if not show["created_by_app"]:
            report.append(f"SKIP     {old.name}: folder existed before filing (not ours to move)")
            continue
        updates = [("UPDATE shows SET manifest = ? WHERE id = ?",
                    (swap_manifest(show["manifest"], old, new), show["id"]))]
        for row in conn.execute("SELECT video_id, dest_path, manifest FROM filings WHERE show_id = ?",
                                (show["id"],)).fetchall():
            updates.append(("UPDATE filings SET dest_path = ?, manifest = ? WHERE video_id = ?",
                            (swap(row["dest_path"], old, new), swap_manifest(row["manifest"], old, new),
                             row["video_id"])))
        moved += _move(conn, old, new, apply, report, updates)

    for row in conn.execute("SELECT * FROM filings WHERE type = 'movie'").fetchall():
        old = Path(row["dest_path"]).parent
        if old.parent != old_movies:
            continue  # already relocated, or filed elsewhere
        new = cfg.movies_dir / old.name
        if str(old) not in json.loads(row["manifest"]).get("dirs", []):
            report.append(f"SKIP     {old.name}: folder not created by this app")
            continue
        if not old.is_dir():
            report.append(f"MISSING  movie folder {old}")
            continue
        updates = [("UPDATE filings SET dest_path = ?, manifest = ? WHERE video_id = ?",
                    (swap(row["dest_path"], old, new), swap_manifest(row["manifest"], old, new),
                     row["video_id"]))]
        moved += _move(conn, old, new, apply, report, updates)

    if apply and moved:
        with conn:
            log_event(conn, None, "relocate", {"folders": moved, "shows": str(cfg.shows_dir),
                                               "movies": str(cfg.movies_dir)})
    verb = "Moved" if apply else "Would move"
    report.append(f"\n{verb} {moved} folders." + ("" if apply else " Re-run with --apply."))
    return report


def main(argv: list[str], cfg: Config | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m youtarr_routarr.relocate")
    parser.add_argument("--old-shows", type=Path, required=True)
    parser.add_argument("--old-movies", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    cfg = cfg or Config.from_env()
    if {args.old_shows, args.old_movies} & {cfg.shows_dir, cfg.movies_dir}:
        parser.error("SHOWS_DIR/MOVIES_DIR still point at the old roots; update the compose file first")
    conn = connect(cfg.db_path)
    report = relocate(cfg, conn, args.old_shows, args.old_movies, args.apply)
    print("\n".join(report))
    return 1 if any(line.startswith(("CONFLICT", "MISSING", "ERROR")) for line in report) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
