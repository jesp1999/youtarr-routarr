"""Youtarr's info files reach many megabytes; reading them must not exhaust the container."""
import json
import threading
import time
import tracemalloc

from test_youtarr_routarr import add_info, add_video, cfg  # noqa: F401
from youtarr_routarr import media


def test_concurrent_scans_read_each_info_file_once(cfg, monkeypatch):
    for n in range(3):
        add_video(cfg, "C", f"V{n}", f"AAAAAAAAAA{n}")
        add_info(cfg, f"AAAAAAAAAA{n}", f"V{n}")
    media._info_cache.clear()
    parsed = []
    real = media.json.loads

    def slow(text, **kwargs):
        parsed.append(1)
        time.sleep(0.05)  # long enough for the other scans to arrive
        return real(text, **kwargs)

    monkeypatch.setattr(media.json, "loads", slow)
    scans = [threading.Thread(target=media.scan_queue, args=(cfg.queue_dir, cfg.info_dir)) for _ in range(4)]
    for scan in scans:
        scan.start()
    for scan in scans:
        scan.join()
    assert len(parsed) == 3


def test_unused_parts_of_a_large_info_file_are_dropped_while_parsing(cfg):
    formats = [{"format_id": str(n), "url": f"https://example.invalid/{n}/" + "x" * 150,
                "http_headers": {"User-Agent": "u" * 40, "Accept": "a" * 40}, "fragments": [{"url": "f" * 40}] * 3}
               for n in range(4000)]
    add_video(cfg, "C", "Big", "AAAAAAAAAAA")
    add_info(cfg, "AAAAAAAAAAA", "Big", formats=formats, automatic_captions={"en": formats[:500]})
    path = cfg.info_dir / "AAAAAAAAAAA.info.json"
    size = path.stat().st_size
    media._info_cache.clear()
    tracemalloc.start()
    info = media._read_info(cfg.info_dir, "AAAAAAAAAAA")
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert info["title"] == "Big" and info["tags"] == ["horror", "Horror", "short"]
    assert info["categories"] == ["Film & Animation"] and info["duration"] == 737
    assert peak < size * 3, f"parsing a {size} byte file peaked at {peak} bytes"


def test_a_field_youtube_nests_is_still_ignored_safely(cfg):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    (cfg.info_dir / "AAAAAAAAAAA.info.json").write_text(json.dumps([{"title": "not an object"}]))
    media._info_cache.clear()
    (item,) = media.scan_queue(cfg.queue_dir, cfg.info_dir)
    assert item.title == "A"


def test_container_keeps_one_memory_pool_and_has_headroom():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    # Without this each thread keeps its own pool after parsing, and the peaks add up.
    assert "MALLOC_ARENA_MAX=1" in (root / "build" / "Dockerfile").read_text()
    assert 'memory: "536870912"' in (root / "docker-compose.yml").read_text()
