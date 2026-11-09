"""Bring filings made by older versions up to the current NFO format.

    python -m youtarr_routarr.backfill           # dry run
    python -m youtarr_routarr.backfill --apply

For each filed NFO whose content still matches the hash recorded at filing:
- adds <lockdata>true</lockdata>, so Jellyfin stops filling gaps from TMDB/TVDB;
- adds YouTube fields it lacks (originaltitle, runtime, director, tag) from Youtarr's info.json.
Movies, videos and app-created shows also get Youtarr's -fanart image hardlinked as fanart.jpg.
New hashes and links are written to the manifests, so undo keeps working. Edited NFOs are left
alone. Run inside the container.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .config import Config
from .db import connect, log_event
from .filing import youtube_fields
from .media import QueueItem, scan_queue

NFO_ROLES = {"nfo", "show-nfo"}


def upgraded(data: bytes, item: QueueItem | None) -> bytes | None:
    """The NFO with missing fields added, or None when nothing is missing."""
    root = ET.fromstring(data)
    added = False
    if item is not None and root.tag != "tvshow":
        lock = root.find("lockdata")
        if lock is not None:
            root.remove(lock)  # re-appended last below
        had_tags = root.find("tag") is not None  # never mix into a tag list already there
        for tag, value in youtube_fields(item, root.findtext("title")):
            if (had_tags if tag == "tag" else root.find(tag) is not None):
                continue
            ET.SubElement(root, tag).text = str(value)
            added = True
        if lock is not None:
            root.append(lock)
    if root.find("lockdata") is None:
        ET.SubElement(root, "lockdata").text = "true"
        added = True
    if not added:
        return None
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _rewrite(path: Path, record: dict, new: bytes) -> None:
    tmp = path.with_name(f".{path.name}.backfill")
    tmp.write_bytes(new)
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)
    record["sha256"] = hashlib.sha256(new).hexdigest()


def backfill_manifest(manifest: dict, item: QueueItem | None, fanart_dir: Path | None,
                      apply: bool, report: list[str]) -> bool:
    changed = False
    for record in manifest.get("files", []):
        if record.get("kind") != "write" or record.get("role") not in NFO_ROLES:
            continue
        path = Path(record["path"])
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            report.append(f"missing  {path}")
            continue
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            report.append(f"edited   {path} (changed since filing; left alone)")
            continue
        new = upgraded(data, item if record["role"] == "nfo" else None)
        if new is None:
            continue
        report.append(f"nfo      {path}")
        if apply:
            _rewrite(path, record, new)
        changed = True

    if fanart_dir is not None and item is not None and item.fanart is not None:
        dst = fanart_dir / f"fanart{item.fanart.suffix.lower()}"
        if not os.path.lexists(dst) and not any(r.get("role") == "fanart" for r in manifest.get("files", [])):
            report.append(f"fanart   {dst}")
            if apply:
                os.link(item.fanart, dst, follow_symlinks=False)
                manifest["files"].append({"path": str(dst), "kind": "link", "role": "fanart",
                                          "ino": os.stat(dst).st_ino})
            changed = True
    return changed


def main(argv: list[str], cfg: Config | None = None) -> int:
    apply = "--apply" in argv
    cfg = cfg or Config.from_env()
    items = {item.video_id: item for item in scan_queue(cfg.queue_dir, cfg.info_dir)}
    conn = connect(cfg.db_path)
    report: list[str] = []
    rows = 0
    with conn:
        first_item: dict[int, QueueItem] = {}
        for row in conn.execute("SELECT * FROM filings ORDER BY episode").fetchall():
            item = items.get(row["video_id"])
            if item is None:
                report.append(f"no source in queue for {row['video_id']}; NFO gets lockdata only")
            if row["show_id"] is not None and item is not None:
                first_item.setdefault(row["show_id"], item)
            fanart_dir = Path(row["dest_path"]).parent if row["type"] in ("movie", "video") else None
            manifest = json.loads(row["manifest"])
            if backfill_manifest(manifest, item, fanart_dir, apply, report) and apply:
                conn.execute("UPDATE filings SET manifest = ? WHERE video_id = ?",
                             (json.dumps(manifest, ensure_ascii=False), row["video_id"]))
                rows += 1
        for row in conn.execute("SELECT * FROM shows").fetchall():
            manifest = json.loads(row["manifest"])
            fanart_dir = cfg.shows_dir / row["folder"] if row["created_by_app"] else None
            if backfill_manifest(manifest, first_item.get(row["id"]), fanart_dir, apply, report) and apply:
                conn.execute("UPDATE shows SET manifest = ? WHERE id = ?",
                             (json.dumps(manifest, ensure_ascii=False), row["id"]))
                rows += 1
        if apply and rows:
            log_event(conn, None, "backfill", {"rows": rows})
    print("\n".join(report) or "Nothing to do.")
    nfos = sum(line.startswith("nfo") for line in report)
    fanarts = sum(line.startswith("fanart") for line in report)
    verb = "Updated" if apply else "Would update"
    print(f"\n{verb} {nfos} NFOs and add {fanarts} fanart links." + ("" if apply else " Re-run with --apply."))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
