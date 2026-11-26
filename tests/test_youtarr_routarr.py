from __future__ import annotations

import base64
import errno
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "build"))

from youtarr_routarr import create_app, filing  # noqa: E402
from youtarr_routarr.config import Config  # noqa: E402
from youtarr_routarr.db import connect  # noqa: E402
from youtarr_routarr.filing import Classification, FilingError, apply_plan, build_plan, safe_name, undo_filing  # noqa: E402
from youtarr_routarr.media import scan_queue  # noqa: E402
from youtarr_routarr.notify import added_embeds, check_queue, notify_added  # noqa: E402

NFO = """<?xml version="1.0"?><movie><title>{title}</title><plot>A &amp; B plot</plot>
<premiered>{date}</premiered><studio>{channel}</studio></movie>"""


def add_video(cfg: Config, channel: str, title: str, vid: str, date: str = "2022-05-02",
              ext: str = ".mp4", thumb: bool = True, subs: bool = False) -> Path:
    year = date[:4]
    folder = cfg.queue_dir / channel / f"{title} ({year}) - {vid}"
    folder.mkdir(parents=True)
    stem = f"{title} ({year}) [{vid}]"
    video = folder / f"{stem}{ext}"
    video.write_bytes(b"video-" + vid.encode())
    (folder / f"{stem}.nfo").write_text(NFO.format(title=title, date=date, channel=channel))
    if thumb:
        (folder / f"{stem}.jpg").write_bytes(b"jpg")
        (folder / f"{stem}-fanart.jpg").write_bytes(b"fanart")
    if subs:
        (folder / f"{stem}.en.srt").write_text("1\n")
    return video


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(
        queue_dir=tmp_path / "queue", shows_dir=tmp_path / "Shows", movies_dir=tmp_path / "Movies",
        videos_dir=tmp_path / "Videos", music_dir=tmp_path / "Music", info_dir=tmp_path / "info", db_path=tmp_path / "state" / "t.sqlite3",
        dry_run=False, auth_disabled=True)
    for path in (config.queue_dir, config.shows_dir, config.movies_dir, config.videos_dir, config.music_dir,
                 config.info_dir, config.db_path.parent):
        path.mkdir()
    return config


@pytest.fixture
def conn(cfg: Config):
    connection = connect(cfg.db_path)
    yield connection
    connection.close()


def file_one(cfg, conn, item, cls):
    plan = build_plan(cfg, conn, [(item, cls)])
    assert plan.ok, plan.errors
    apply_plan(cfg, conn, plan)
    return plan


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def by_id(cfg):
    return {i.video_id: i for i in scan_queue(cfg.queue_dir, cfg.info_dir)}


def test_scan_reads_metadata_and_ignores_partial_downloads(cfg):
    add_video(cfg, "Morphine", "Spaceman", "5GwpQ5O505s", subs=True)
    busy = add_video(cfg, "Morphine", "Unfinished", "aaaaaaaaaaa")
    (busy.parent / "x.mp4.part").write_bytes(b"")
    (cfg.info_dir / "5GwpQ5O505s.info.json").write_text(
        '{"title": "Info Title", "description": "Info plot", "upload_date": "20220502",'
        ' "playlist_title": "List", "playlist_index": 3}')
    items = scan_queue(cfg.queue_dir, cfg.info_dir)
    assert [i.video_id for i in items] == ["5GwpQ5O505s"]
    item = items[0]
    assert (item.title, item.plot, item.upload_date, item.year) == ("Info Title", "Info plot", "2022-05-02", 2022)
    assert (item.channel, item.playlist, item.playlist_index) == ("Morphine", "List", 3)
    assert item.thumb.name.endswith("[5GwpQ5O505s].jpg")
    assert [s.name.endswith(".en.srt") for s in item.subtitles] == [True]


def test_file_without_youtube_id_gets_stable_key(cfg):
    (cfg.queue_dir / "Loose Title (2020).webm").write_bytes(b"x")
    first, second = scan_queue(cfg.queue_dir), scan_queue(cfg.queue_dir)
    assert first[0].video_id == second[0].video_id
    assert first[0].video_id.startswith("file-") and not first[0].has_real_id
    assert first[0].title == "Loose Title" and first[0].year == 2020


def test_show_layout_nfo_and_untouched_source(cfg, conn):
    video = add_video(cfg, "David Firth", "Salad Fingers 1", "AAAAAAAAAAA", date="2004-07-01", subs=True)
    before = video.read_bytes(), video.stat().st_mtime_ns, sorted(os.listdir(video.parent))
    item = by_id(cfg)["AAAAAAAAAAA"]
    file_one(cfg, conn, item, Classification("show", "Spoons", show="Salad Fingers", season=1))

    base = cfg.shows_dir / "Salad Fingers (2004)"
    dest = base / "Season 01" / "Salad Fingers - S01E01 - Spoons.mp4"
    assert dest.exists() and dest.stat().st_ino == video.stat().st_ino
    assert (base / "poster.jpg").exists()
    assert (dest.with_name(f"{dest.stem}-thumb.jpg")).exists()
    assert (dest.with_name(f"{dest.stem}.en.srt")).exists()
    show = ET.parse(base / "tvshow.nfo").getroot()
    assert (show.tag, show.findtext("title"), show.findtext("premiered")) == ("tvshow", "Salad Fingers", "2004-07-01")
    episode = ET.parse(dest.with_suffix(".nfo")).getroot()
    assert (episode.tag, episode.findtext("title"), episode.findtext("episode")) == ("episodedetails", "Spoons", "1")
    assert episode.findtext("plot") == "A & B plot" and episode.findtext("aired") == "2004-07-01"
    assert episode.find("uniqueid").text == "AAAAAAAAAAA"
    assert before == (video.read_bytes(), video.stat().st_mtime_ns, sorted(os.listdir(video.parent)))


def test_movie_and_video_layouts(cfg, conn):
    add_video(cfg, "C", "Film", "BBBBBBBBBBB", date="2020-01-02")
    add_video(cfg, "C", "Clip", "CCCCCCCCCCC", date="2021-01-02", ext=".webm")
    items = by_id(cfg)
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("movie", "The Film"))
    file_one(cfg, conn, items["CCCCCCCCCCC"], Classification("video", "Clip", name="My: Clip"))
    movie = cfg.movies_dir / "The Film (2020)" / "The Film (2020).mp4"
    assert movie.exists() and ET.parse(movie.with_suffix(".nfo")).getroot().findtext("year") == "2020"
    assert (movie.parent / "poster.jpg").exists()
    video = cfg.videos_dir / "YouTube" / "My - Clip" / "My - Clip.webm"
    assert video.exists() and ET.parse(video.with_suffix(".nfo")).getroot().findtext("title") == "My: Clip"


def test_video_subfolders_are_created_and_reused(cfg, conn):
    existing = cfg.videos_dir / "YouTube" / "Comedy"
    existing.mkdir(parents=True)
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    add_video(cfg, "C", "B", "BBBBBBBBBBB")
    items = by_id(cfg)
    # Typed in a different case: reuses "Comedy" and creates the nested folder like mkdir -p.
    file_one(cfg, conn, items["AAAAAAAAAAA"], Classification("video", "A", name="A", folder="comedy/ Sketch: Shows /"))
    assert (existing / "Sketch - Shows" / "A" / "A.mp4").exists()
    assert not (cfg.videos_dir / "YouTube" / "comedy").exists()
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("video", "B", name="B", folder=""))
    assert (cfg.videos_dir / "YouTube" / "B" / "B.mp4").exists()

    undo_filing(cfg, conn, "AAAAAAAAAAA")
    assert existing.is_dir() and not (existing / "Sketch - Shows").exists()


@pytest.mark.parametrize("folder", ["..", "a/../..", "./."])
def test_video_subfolder_cannot_escape(cfg, conn, folder):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    plan = build_plan(cfg, conn, [(by_id(cfg)["AAAAAAAAAAA"], Classification("video", "A", name="A", folder=folder))])
    assert not plan.ok


def test_video_folders_lists_categories_not_video_folders(cfg):
    base = cfg.videos_dir / "YouTube"
    (base / "YTPs" / "Some Poop").mkdir(parents=True)
    (base / "YTPs" / "Some Poop" / "Some Poop.mp4").write_bytes(b"v")
    (base / "Comedy" / "Sketch").mkdir(parents=True)
    (base / "Vsauce").mkdir()
    assert filing.video_folders(cfg) == ["Comedy", "Comedy/Sketch", "Vsauce", "YTPs"]


def test_movie_year_override_and_missing_year(cfg, conn):
    add_video(cfg, "C", "Film", "BBBBBBBBBBB", date="2020-01-02")
    (cfg.queue_dir / "NoDate.mp4").write_bytes(b"x")
    items = by_id(cfg)
    plan = build_plan(cfg, conn, [(items["BBBBBBBBBBB"], Classification("movie", "Film", year=1999))])
    assert plan.entries[0].dest.name == "Film (1999).mp4"
    loose = next(i for i in items.values() if not i.has_real_id)
    plan = build_plan(cfg, conn, [(loose, Classification("movie", "NoDate"))])
    assert not plan.ok and "enter a year" in plan.errors[0]


def test_next_episode_counts_disk_and_db(cfg, conn):
    existing = cfg.shows_dir / "Old Show (2010)" / "Season 1"
    existing.mkdir(parents=True)
    (existing / "Old Show - S01E04 - Pilot.mkv").write_bytes(b"x")
    (cfg.shows_dir / "Old Show (2010)" / "tvshow.nfo").write_text("mine")
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    add_video(cfg, "C", "B", "BBBBBBBBBBB")
    items = by_id(cfg)
    file_one(cfg, conn, items["AAAAAAAAAAA"], Classification("show", "A", show="Old Show"))
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("show", "B", show="Old Show"))
    assert (existing / "Old Show - S01E05 - A.mp4").exists()
    assert (existing / "Old Show - S01E06 - B.mp4").exists()
    assert (cfg.shows_dir / "Old Show (2010)" / "tvshow.nfo").read_text() == "mine"
    assert not (cfg.shows_dir / "Old Show (2010)" / "Season 01").exists()


def test_batch_uses_earliest_date_for_show_folder_and_writes_one_tvshow(cfg, conn):
    add_video(cfg, "C", "Later", "BBBBBBBBBBB", date="2010-01-01")
    add_video(cfg, "C", "First", "AAAAAAAAAAA", date="2005-01-01")
    items = by_id(cfg)
    pairs = [(items["AAAAAAAAAAA"], Classification("show", "First", show="S", episode=1)),
             (items["BBBBBBBBBBB"], Classification("show", "Later", show="S", episode=2))]
    plan = build_plan(cfg, conn, pairs)
    assert plan.ok, plan.errors
    apply_plan(cfg, conn, plan)
    assert len(list((cfg.shows_dir / "S (2005)").glob("tvshow.nfo"))) == 1
    assert (cfg.shows_dir / "S (2005)" / "Season 01" / "S - S01E02 - Later.mp4").exists()


def test_duplicate_episode_is_refused(cfg, conn):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    add_video(cfg, "C", "B", "BBBBBBBBBBB")
    items = by_id(cfg)
    pairs = [(items["AAAAAAAAAAA"], Classification("show", "A", show="S", episode=1)),
             (items["BBBBBBBBBBB"], Classification("show", "B", show="S", episode=1))]
    plan = build_plan(cfg, conn, pairs)
    assert any("already taken" in error for error in plan.errors)


def test_refuses_other_filesystem(cfg, conn, monkeypatch):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    item = by_id(cfg)["AAAAAAAAAAA"]
    real = os.stat

    def fake(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if Path(path) == cfg.movies_dir:
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(filing.os, "stat", fake)
    plan = build_plan(cfg, conn, [(item, Classification("movie", "A"))])
    assert any("different filesystems" in error for error in plan.errors)
    assert tree(cfg.movies_dir) == set()


def test_link_failure_rolls_back_whole_batch(cfg, conn, monkeypatch):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    add_video(cfg, "C", "B", "BBBBBBBBBBB")
    items = by_id(cfg)
    plan = build_plan(cfg, conn, [(items["AAAAAAAAAAA"], Classification("show", "A", show="S", episode=1)),
                                  (items["BBBBBBBBBBB"], Classification("show", "B", show="S", episode=2))])
    real_link, calls = os.link, []

    def flaky(src, dst, **kwargs):
        calls.append(dst)
        if "S01E02" in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(filing.os, "link", flaky)
    with pytest.raises(FilingError, match="different filesystems"):
        apply_plan(cfg, conn, plan)
    assert tree(cfg.shows_dir) == set()
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0
    assert len(by_id(cfg)) == 2


def test_never_overwrites_existing_folder_or_file(cfg, conn):
    add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2020-01-01")
    item = by_id(cfg)["AAAAAAAAAAA"]
    (cfg.movies_dir / "A (2020)").mkdir()
    (cfg.movies_dir / "A (2020)" / "A (2020).mp4").write_bytes(b"precious")
    plan = build_plan(cfg, conn, [(item, Classification("movie", "A"))])
    assert not plan.ok
    assert (cfg.movies_dir / "A (2020)" / "A (2020).mp4").read_bytes() == b"precious"


@pytest.mark.parametrize("raw,expected", [
    ("../../etc/passwd", "-..-etc-passwd"), ("A: B?", "A - B"), ("ｆｕｌｌ：ｗｉｄｔｈ", "full -width"),
    ("tab\there\n", "tabhere"),
])
def test_safe_name_stays_one_component(raw, expected):
    assert safe_name(raw) == expected
    assert "/" not in safe_name(raw)


@pytest.mark.parametrize("raw", ["", "...", "  ", "\x00"])
def test_safe_name_rejects_empty(raw):
    with pytest.raises(FilingError):
        safe_name(raw)


def test_undo_restores_tree_and_keeps_source(cfg, conn):
    video = add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2020-01-01")
    add_video(cfg, "C", "B", "BBBBBBBBBBB", date="2021-01-01")
    items = by_id(cfg)
    file_one(cfg, conn, items["AAAAAAAAAAA"], Classification("show", "A", show="S"))
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("show", "B", show="S"))

    assert undo_filing(cfg, conn, "AAAAAAAAAAA") == []
    assert (cfg.shows_dir / "S (2020)" / "tvshow.nfo").exists()  # the other episode still needs it
    assert undo_filing(cfg, conn, "BBBBBBBBBBB") == []
    assert tree(cfg.shows_dir) == set()
    assert video.read_bytes() == b"video-AAAAAAAAAAA" and video.stat().st_nlink == 1
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 0


def test_undo_keeps_preexisting_show_folder_and_changed_files(cfg, conn):
    (cfg.shows_dir / "Mine").mkdir()
    (cfg.shows_dir / "Mine" / "tvshow.nfo").write_text("mine")
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    item = by_id(cfg)["AAAAAAAAAAA"]
    plan = file_one(cfg, conn, item, Classification("show", "A", show="Mine"))
    nfo = plan.entries[0].dest.with_suffix(".nfo")
    nfo.write_text("edited by hand")
    kept = undo_filing(cfg, conn, "AAAAAAAAAAA")
    assert any("changed since filing" in path for path in kept) and nfo.exists()
    assert (cfg.shows_dir / "Mine" / "tvshow.nfo").read_text() == "mine"


def test_undo_refuses_when_library_copy_is_the_last_one(cfg, conn):
    video = add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2020-01-01")
    plan = file_one(cfg, conn, by_id(cfg)["AAAAAAAAAAA"], Classification("movie", "A"))
    video.unlink()
    with pytest.raises(FilingError, match="last copy"):
        undo_filing(cfg, conn, "AAAAAAAAAAA")
    assert plan.entries[0].dest.exists()
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1


def test_ping_respects_age_and_rate_limit(cfg, conn):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    config = replace(cfg, discord_webhook_url="https://example.invalid/hook", queue_ping_days=7)
    sent: list[str] = []
    post = lambda _url, content: sent.append(content)  # noqa: E731
    start = datetime.now(timezone.utc)
    assert check_queue(config, conn, start, post) is False  # just seen
    assert check_queue(config, conn, start + timedelta(days=8), post) is True
    assert check_queue(config, conn, start + timedelta(days=8, hours=1), post) is False
    assert check_queue(config, conn, start + timedelta(days=9, hours=1), post) is True
    assert len(sent) == 2 and "1 of 1" in sent[0]
    assert (cfg.movies_dir.exists() and tree(cfg.movies_dir) == set())


def client(cfg, **changes):
    return create_app(replace(cfg, **changes), start_background=False).test_client()


def test_auth_required_except_health(cfg):
    web = client(cfg, auth_disabled=False, auth_user="u", auth_password="p")
    assert web.get("/").status_code == 401
    assert web.get("/healthz").status_code == 200
    token = base64.b64encode(b"u:p").decode()
    assert web.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
    bad = base64.b64encode(b"u:x").decode()
    assert web.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_app_will_not_start_without_auth(cfg):
    with pytest.raises(RuntimeError):
        create_app(replace(cfg, auth_disabled=False), start_background=False)


def test_cross_site_post_blocked(cfg):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    web = client(cfg)
    data = {"type": "video", "name": "A", "title": "A"}
    assert web.post("/item/AAAAAAAAAAA/file", data=data, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert web.post("/item/AAAAAAAAAAA/file", data=data, headers={"Origin": "http://evil.example"}).status_code == 403
    assert tree(cfg.videos_dir) == set()


def test_dry_run_writes_nothing(cfg):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    web = client(cfg, dry_run=True)
    response = web.post("/item/AAAAAAAAAAA/file", data={"type": "video", "name": "A", "title": "A"})
    assert b"Dry run" in response.data and b"A.mp4" in response.data
    assert tree(cfg.videos_dir) == set()
    assert b"1 unclassified" in web.get("/").data


def test_web_flow_file_undo_reclassify(cfg):
    video = add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2020-01-01")
    web = client(cfg)
    page = web.get("/").data
    assert b"1 unclassified" in page and b"card-AAAAAAAAAAA" in page
    assert web.get("/thumb/AAAAAAAAAAA").data == b"jpg"
    assert b"Episode" in web.get("/item/AAAAAAAAAAA/fields?type=show").data
    preview = web.post("/item/AAAAAAAAAAA/preview", data={"type": "movie", "title": "A", "year": "2020"})
    assert b"hardlink" in preview.data and tree(cfg.movies_dir) == set()

    done = web.post("/item/AAAAAAAAAAA/file", data={"type": "movie", "title": "A", "year": "2020"})
    assert done.headers["HX-Retarget"] == "#card-AAAAAAAAAAA"
    assert (cfg.movies_dir / "A (2020)" / "A (2020).mp4").exists()
    assert b"0 unclassified" in web.get("/").data
    assert web.get("/thumb/AAAAAAAAAAA").status_code == 404
    assert b"A (2020).mp4" in web.get("/filed").data

    back = web.post("/filed/AAAAAAAAAAA/undo", data={"then": "reclassify"})
    assert back.status_code == 303 and "#card-AAAAAAAAAAA" in back.headers["Location"]
    assert tree(cfg.movies_dir) == set() and video.exists()
    assert b"1 unclassified" in web.get("/").data


def test_group_bulk_action_orders_by_date(cfg):
    add_video(cfg, "Chan", "Second", "BBBBBBBBBBB", date="2011-01-01")
    add_video(cfg, "Chan", "First", "AAAAAAAAAAA", date="2010-01-01")
    add_video(cfg, "Chan", "Skip", "CCCCCCCCCCC", date="2012-01-01")
    web = client(cfg)
    from youtarr_routarr import group_key
    key = group_key("Chan")
    page = web.get(f"/group/{key}").data.decode()
    assert page.index("First") < page.index("Second")
    data = {"show": "Chan", "season": "1", "include-AAAAAAAAAAA": "on", "include-BBBBBBBBBBB": "on",
            "episode-AAAAAAAAAAA": "1", "episode-BBBBBBBBBBB": "2",
            "title-AAAAAAAAAAA": "First", "title-BBBBBBBBBBB": "Second"}
    assert web.post(f"/group/{key}/file", data=data).status_code == 200
    season = cfg.shows_dir / "Chan (2010)" / "Season 01"
    assert (season / "Chan - S01E01 - First.mp4").exists() and (season / "Chan - S01E02 - Second.mp4").exists()
    assert b"1 unclassified" in web.get("/").data


def test_video_form_defaults_folder_to_existing_channel_folder(cfg):
    (cfg.videos_dir / "YouTube" / "VSauce").mkdir(parents=True)
    add_video(cfg, "Vsauce", "A", "AAAAAAAAAAA")
    page = client(cfg).get("/").data.decode()
    assert 'name="folder" value="VSauce"' in page and "<option>VSauce</option>" in page
    assert "Videos/YouTube/" in page and 'data-combo="folders-AAAAAAAAAAA"' in page


def test_show_form_prefers_existing_show_and_refreshes_episode(cfg, conn):
    shows = cfg.shows_dir / "Chan (2010)" / "Season 01"
    shows.mkdir(parents=True)
    (shows / "Chan - S01E01 - X.mp4").write_bytes(b"v")
    add_video(cfg, "Chan", "A", "AAAAAAAAAAA")
    web = client(cfg)
    fields = web.get("/item/AAAAAAAAAAA/fields?type=show").data.decode()
    assert 'value="Chan (2010)"' in fields and 'name="episode" type="number" min="1" value="2"' in fields
    other = web.get("/item/AAAAAAAAAAA/episode?show=Brand+New&season=1").data.decode()
    assert 'id="episode-AAAAAAAAAAA"' in other and 'value="1"' in other
    again = web.get("/item/AAAAAAAAAAA/episode?show=chan&season=").data.decode()
    assert 'value="2"' in again


def test_group_rows_renumber_and_keep_edits(cfg):
    add_video(cfg, "Chan", "First", "AAAAAAAAAAA", date="2010-01-01")
    add_video(cfg, "Chan", "Second", "BBBBBBBBBBB", date="2011-01-01")
    add_video(cfg, "Chan", "Third", "CCCCCCCCCCC", date="2012-01-01")
    (cfg.shows_dir / "Other" / "Season 02").mkdir(parents=True)
    (cfg.shows_dir / "Other" / "Season 02" / "Other - S02E04 - X.mp4").write_bytes(b"v")
    from youtarr_routarr import group_key
    web = client(cfg)
    rows = web.get(f"/group/{group_key('Chan')}/rows", query_string={
        "show": "Other", "season": "2", "include-AAAAAAAAAAA": "on", "include-CCCCCCCCCCC": "on",
        "title-AAAAAAAAAAA": "Edited"}).data.decode()
    assert rows.startswith('<tbody id="group-rows">')
    assert 'name="episode-AAAAAAAAAAA" value="5"' in rows and 'name="episode-CCCCCCCCCCC" value="6"' in rows
    assert 'name="episode-BBBBBBBBBBB" value=""' in rows and 'value="Edited"' in rows


def test_new_show_year_override(cfg, conn):
    add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2015-06-01")
    add_video(cfg, "C", "B", "BBBBBBBBBBB", date="2016-06-01")
    items = by_id(cfg)
    file_one(cfg, conn, items["AAAAAAAAAAA"], Classification("show", "A", show="Salad Fingers", show_year=2004))
    nfo = ET.parse(cfg.shows_dir / "Salad Fingers (2004)" / "tvshow.nfo").getroot()
    assert nfo.findtext("year") == "2004" and nfo.findtext("premiered") is None
    # Existing show: a different year is ignored rather than making a second folder.
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("show", "B", show="Salad Fingers", show_year=1999))
    assert sorted(p.name for p in cfg.shows_dir.iterdir()) == ["Salad Fingers (2004)"]


def test_new_show_defaults_to_upload_year_and_rejects_bad_year(cfg, conn):
    add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2015-06-01")
    item = by_id(cfg)["AAAAAAAAAAA"]
    plan = build_plan(cfg, conn, [(item, Classification("show", "A", show="New"))])
    assert plan.ok and plan.entries[0].dest.parts[-3] == "New (2015)"
    assert plan.shows["new"].premiered == "2015-06-01"
    bad = build_plan(cfg, conn, [(item, Classification("show", "A", show="New", show_year=15))])
    assert not bad.ok and "1800" in bad.errors[0]


def test_show_year_field_only_for_new_shows(cfg):
    (cfg.shows_dir / "Old (2001)").mkdir()
    add_video(cfg, "C", "A", "AAAAAAAAAAA", date="2015-06-01")
    web = client(cfg)
    fields = web.get("/item/AAAAAAAAAAA/fields?type=show").data.decode()
    assert 'name="show_year"' in fields and 'value="2015"' in fields
    assert 'name="show_year"' not in web.get("/item/AAAAAAAAAAA/episode?show=old").data.decode()
    kept = web.get("/item/AAAAAAAAAAA/episode?show=Fresh&show_year=1990").data.decode()
    assert 'name="show_year"' in kept and 'value="1990"' in kept
    done = web.post("/item/AAAAAAAAAAA/file", data={"type": "show", "title": "A", "show": "Fresh",
                                                   "season": "1", "episode": "1", "show_year": "1990"})
    assert done.status_code == 200 and (cfg.shows_dir / "Fresh (1990)" / "Season 01").is_dir()


def test_group_page_year_follows_show_choice(cfg):
    (cfg.shows_dir / "Old (2001)").mkdir()
    add_video(cfg, "Chan", "First", "AAAAAAAAAAA", date="2010-01-01")
    add_video(cfg, "Chan", "Second", "BBBBBBBBBBB", date="2011-01-01")
    from youtarr_routarr import group_key
    key = group_key("Chan")
    web = client(cfg)
    page = web.get(f"/group/{key}").data.decode()
    assert 'id="group-year"' in page and 'name="show_year"' in page and 'value="2010"' in page
    old = web.get(f"/group/{key}/rows", query_string={"show": "Old", "include-BBBBBBBBBBB": "on"}).data.decode()
    assert 'hx-swap-oob="true"' in old and 'name="show_year"' not in old
    new = web.get(f"/group/{key}/rows", query_string={"show": "New", "include-BBBBBBBBBBB": "on"}).data.decode()
    assert 'name="show_year"' in new and 'value="2011"' in new
    data = {"show": "New", "season": "1", "show_year": "2003", "include-AAAAAAAAAAA": "on",
            "episode-AAAAAAAAAAA": "1", "title-AAAAAAAAAAA": "First"}
    assert web.post(f"/group/{key}/file", data=data).status_code == 200
    assert (cfg.shows_dir / "New (2003)" / "Season 01" / "New - S01E01 - First.mp4").exists()


@pytest.mark.parametrize("raw, expected", [
    ('Horror Short Film "Odd Girl" | ALTER', "Odd Girl"),
    ("Horror Short Film “The Armoire” | ALTER", "The Armoire"),
    ("THE COOK ｜ Award-Winning Short Film", "THE COOK"),
    ("Lights Out - Who's There Film Challenge (2013)", "Lights Out"),
    ("LISTERS: A Glimpse Into Extreme Birdwatching", "LISTERS: A Glimpse Into Extreme Birdwatching"),
    ("X-Men Origins", "X-Men Origins"),
    ("Hey, Vsauce! Your home security is great...", "Hey, Vsauce! Your home security is great..."),
    ("(2020) | x", "(2020) | x"),
])
def test_suggest_movie_title(raw, expected):
    assert filing.suggest_movie_title(raw) == expected


def test_movie_form_suggests_clean_title_and_offers_full_one(cfg):
    add_video(cfg, "ALTER", 'Horror Short Film "Odd <Girl>" | ALTER', "AAAAAAAAAAA", date="2021-06-14")
    item = by_id(cfg)["AAAAAAAAAAA"]
    web = client(cfg)
    page = web.get("/item/AAAAAAAAAAA/fields?type=movie").data.decode()
    assert 'name="title" value="Odd &lt;Girl&gt;"' in page and "Use the full title" in page
    assert "<Girl>" not in page and 'value = &#34;' in page  # JS string is attribute-escaped
    plain = add_video(cfg, "C", "Plain", "BBBBBBBBBBB")
    assert "Use the full title" not in web.get("/item/BBBBBBBBBBB/fields?type=movie").data.decode()
    done = web.post("/item/AAAAAAAAAAA/file", data={"type": "movie", "title": "Odd Girl", "year": "2021"})
    assert done.status_code == 200 and (cfg.movies_dir / "Odd Girl (2021)" / "Odd Girl (2021).mp4").exists()
    assert plain.exists() and item.path.exists()


def test_stats_needs_no_auth_and_reveals_only_counts(cfg, conn):
    add_video(cfg, "C", "Secret Title", "AAAAAAAAAAA", date="2020-01-01")
    add_video(cfg, "C", "B", "BBBBBBBBBBB", date="2020-01-01")
    file_one(cfg, conn, by_id(cfg)["BBBBBBBBBBB"], Classification("movie", "B"))
    web = client(cfg, auth_disabled=False, auth_user="u", auth_password="p")
    response = web.get("/stats")
    assert response.status_code == 200
    assert response.get_json() == {"queue": 1, "waiting": 0, "filed": 1, "ping_days": 7}
    assert b"Secret" not in response.data


def add_info(cfg, vid, title, **extra):
    import json
    data = {"id": vid, "title": title, "upload_date": "20250101", "channel": "C", "description": "",
            "duration": 737, "categories": ["Film & Animation"], "tags": ["horror", "Horror", "short"]}
    data.update(extra)
    (cfg.info_dir / f"{vid}.info.json").write_text(json.dumps(data))


def test_nfo_gets_youtube_fields_lockdata_and_fanart(cfg, conn):
    add_video(cfg, "C", "SISTER - a short film by Daniel Hentschel", "AAAAAAAAAAA", date="2025-01-01")
    add_info(cfg, "AAAAAAAAAAA", "SISTER - a short film by Daniel Hentschel")
    file_one(cfg, conn, by_id(cfg)["AAAAAAAAAAA"], Classification("movie", "SISTER"))
    folder = cfg.movies_dir / "SISTER (2025)"
    root = ET.parse(folder / "SISTER (2025).nfo").getroot()
    assert root.findtext("originaltitle") == "SISTER - a short film by Daniel Hentschel"
    assert root.findtext("runtime") == "12" and root.findtext("director") == "Daniel Hentschel"
    assert [t.text for t in root.findall("tag")] == ["Film & Animation", "horror", "short"]
    assert root.findtext("lockdata") == "true" and root[-1].tag == "lockdata"
    assert (folder / "fanart.jpg").read_bytes() == b"fanart"

    undo_filing(cfg, conn, "AAAAAAAAAAA")
    assert not folder.exists()


@pytest.mark.parametrize("title, plot, expected", [
    ("SISTER - a short film by Daniel Hentschel", "", "Daniel Hentschel"),
    ("MILK & SERIAL (FOUND FOOTAGE HORROR FILM DIRECTED BY CURRY BARKER)", "", "Curry Barker"),
    ("Odd Girl", "Director of Photography: Farhad Ghaderi\nDirected by: Jane Roe", "Jane Roe"),
    ("X", "Director / Writer / Editor – Christian Jaramillo", "Christian Jaramillo"),
    ("X", "Director and writer - Nassos Vakalis", "Nassos Vakalis"),
    ("X", "Director of Photography: Someone", None),
    ("X", "Director: https://example.com", None),
])
def test_find_director(title, plot, expected):
    from youtarr_routarr.media import QueueItem
    item = QueueItem("AAAAAAAAAAA", True, Path("x.mp4"), "C", title, plot=plot)
    assert filing.find_director(item) == expected


def test_existing_shows_lists_each_show_once_with_its_year(cfg, conn):
    add_video(cfg, "David Firth", "Salad Fingers 1", "AAAAAAAAAAA", date="2004-07-01")
    file_one(cfg, conn, by_id(cfg)["AAAAAAAAAAA"], Classification("show", "Spoons", show="Salad Fingers", season=1))
    assert filing.existing_shows(cfg, conn) == ["Salad Fingers (2004)"]


def test_group_specials_go_to_season_zero_and_number_separately(cfg):
    # The special is the oldest video, yet must not decide the show's year or poster source.
    add_video(cfg, "Chan", "Bonus Special", "SSSSSSSSSSS", date="2009-01-01")
    add_video(cfg, "Chan", "First", "AAAAAAAAAAA", date="2010-01-01")
    add_video(cfg, "Chan", "Second", "BBBBBBBBBBB", date="2011-01-01")
    from youtarr_routarr import group_key
    web = client(cfg)
    key = group_key("Chan")
    page = web.get(f"/group/{key}").data.decode()  # titles with "special" are pre-ticked
    assert 'name="special-SSSSSSSSSSS" checked' in page and 'name="special-AAAAAAAAAAA" checked' not in page
    rows = web.get(f"/group/{key}/rows", query_string={
        "show": "Chan", "season": "1", "include-SSSSSSSSSSS": "on", "include-AAAAAAAAAAA": "on",
        "include-BBBBBBBBBBB": "on", "special-SSSSSSSSSSS": "on"}).data.decode()
    assert 'name="episode-SSSSSSSSSSS" value="1"' in rows
    assert 'name="episode-AAAAAAAAAAA" value="1"' in rows and 'name="episode-BBBBBBBBBBB" value="2"' in rows
    data = {"show": "Chan", "season": "1", "include-SSSSSSSSSSS": "on", "include-AAAAAAAAAAA": "on",
            "include-BBBBBBBBBBB": "on", "special-SSSSSSSSSSS": "on",
            "episode-SSSSSSSSSSS": "1", "episode-AAAAAAAAAAA": "1", "episode-BBBBBBBBBBB": "2",
            "title-SSSSSSSSSSS": "Bonus Special", "title-AAAAAAAAAAA": "First", "title-BBBBBBBBBBB": "Second"}
    assert web.post(f"/group/{key}/file", data=data).status_code == 200
    show = cfg.shows_dir / "Chan (2010)"
    assert (show / "Season 00" / "Chan - S00E01 - Bonus Special.mp4").exists()
    assert (show / "Season 01" / "Chan - S01E01 - First.mp4").exists()
    assert (show / "Season 01" / "Chan - S01E02 - Second.mp4").exists()
    assert ET.parse(show / "Season 00" / "Chan - S00E01 - Bonus Special.nfo").getroot().findtext("season") == "0"
    assert ET.parse(show / "tvshow.nfo").getroot().findtext("premiered") == "2010-01-01"


def test_video_is_linked_after_its_nfo_and_images(cfg, conn, monkeypatch):
    add_video(cfg, "David Firth", "Salad Fingers 1", "AAAAAAAAAAA", date="2004-07-01", subs=True)
    order = []
    real = filing._run_op
    monkeypatch.setattr(filing, "_run_op", lambda op, *a: (order.append(op.role), real(op, *a))[1])
    file_one(cfg, conn, by_id(cfg)["AAAAAAAAAAA"], Classification("show", "Spoons", show="Salad Fingers", season=1))
    assert order[-1] == "video" and order.count("video") == 1


# --- Audio filing -----------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg is not installed")


def make_audio_video(path: Path, codec: str = "aac") -> None:
    """A real one-second video with a tone, so ffprobe and ffmpeg have something to read."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "mpeg4", "-c:a", codec,
                    "-shortest", str(path)], check=True)


def tags(path: Path) -> dict[str, str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    import json
    return {k.lower(): v for k, v in json.loads(out).get("format", {}).get("tags", {}).items()}


def music_item(cfg, title="Song", channel="Chan", vid="mmmmmmmmmmm", **info):
    video = add_video(cfg, channel, title, vid)
    make_audio_video(video)
    if info:
        import json
        (cfg.info_dir / f"{vid}.info.json").write_text(json.dumps(info))
    return by_id(cfg)[vid]


def test_suggest_music_prefers_youtube_music_fields(cfg):
    from youtarr_routarr.media import QueueItem
    from youtarr_routarr.filing import suggest_music
    base = dict(video_id="x", has_real_id=True, path=Path("x.mp4"), channel="Artist - Topic")
    rich = QueueItem(title="whatever", music_artist="Real Artist", music_track="Real Track",
                     music_album="Real Album", release_year=2019, year=2021, **base)
    assert suggest_music(rich) == {"artist": "Real Artist", "title": "Real Track", "album": "Real Album", "year": 2019}
    topic = QueueItem(title="Plain Song", year=2020, **base)
    assert suggest_music(topic) == {"artist": "Artist", "title": "Plain Song", "album": "Singles", "year": 2020}
    dashed = QueueItem(title="Some Band - Big Hit (Official Video)", **{**base, "channel": "VEVO"})
    assert suggest_music(dashed)["artist"] == "Some Band"
    assert suggest_music(dashed)["title"] == "Big Hit"
    keep = QueueItem(title="Jay-Z - Run This (feat. Kanye)", **{**base, "channel": "VEVO"})
    assert suggest_music(keep)["title"] == "Run This (feat. Kanye)"
    named = QueueItem(title="Band - Song", music_artist="Band", **{**base, "channel": "x"})
    assert suggest_music(named)["title"] == "Song"
    plain = QueueItem(title="A Title - With Dash", music_artist="Other", **{**base, "channel": "x"})
    assert suggest_music(plain)["title"] == "A Title - With Dash"


@needs_ffmpeg
def test_file_music_extracts_tagged_audio_and_leaves_source(cfg, conn):
    item = music_item(cfg)
    source_before = item.path.read_bytes()
    cls = Classification(type="music", title="Big Hit", artist="Some Band", album="First Album", track=3, year=2019)
    plan = build_plan(cfg, conn, [(item, cls)])
    assert plan.ok, plan.errors
    dest = cfg.music_dir / "Some Band" / "First Album" / "03 - Big Hit.m4a"
    assert plan.entries[0].dest == dest and not dest.exists()  # planning writes nothing
    apply_plan(cfg, conn, plan)
    assert dest.is_file() and dest.stat().st_size > 0
    assert tree(cfg.music_dir) == {"Some Band", "Some Band/First Album", "Some Band/First Album/03 - Big Hit.m4a"}
    found = tags(dest)
    assert found["title"] == "Big Hit" and found["artist"] == "Some Band"
    assert found["album"] == "First Album" and found["album_artist"] == "Some Band"
    assert found["track"] == "3" and found["date"] == "2019"
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            str(dest)], capture_output=True, text=True).stdout.split()
    assert probe == ["audio"]
    assert item.path.read_bytes() == source_before
    row = conn.execute("SELECT * FROM filings WHERE video_id = ?", (item.video_id,)).fetchone()
    assert row["type"] == "music" and row["dest_path"] == str(dest)
    assert item.video_id not in by_id_pending(cfg, conn)


def by_id_pending(cfg, conn):
    filed = {row["video_id"] for row in conn.execute("SELECT video_id FROM filings")}
    return {i.video_id for i in scan_queue(cfg.queue_dir, cfg.info_dir) if i.video_id not in filed}


@needs_ffmpeg
def test_music_undo_removes_audio_and_empty_folders(cfg, conn):
    item = music_item(cfg)
    file_one(cfg, conn, item, Classification(type="music", title="Song", artist="Band", album="Album"))
    assert undo_filing(cfg, conn, item.video_id) == []
    assert tree(cfg.music_dir) == set()
    assert item.path.exists()


@needs_ffmpeg
def test_music_reuses_folders_case_insensitively_and_never_overwrites(cfg, conn):
    first = music_item(cfg, title="One", vid="aaaaaaaaaa1")
    second = music_item(cfg, title="Two", vid="aaaaaaaaaa2")
    file_one(cfg, conn, first, Classification(type="music", title="Same", artist="Band", album="Album"))
    plan = build_plan(cfg, conn, [(second, Classification(type="music", title="Other", artist="BAND", album="album"))])
    assert plan.ok
    assert plan.entries[0].dest == cfg.music_dir / "Band" / "Album" / "Other.m4a"
    clash = build_plan(cfg, conn, [(second, Classification(type="music", title="Same", artist="band", album="ALBUM"))])
    assert any("Already exists" in e for e in clash.errors)


@needs_ffmpeg
def test_music_requires_artist_and_album_and_valid_track(cfg, conn):
    item = music_item(cfg)
    for cls in (Classification(type="music", title="S", album="A"),
                Classification(type="music", title="S", artist="B"),
                Classification(type="music", title="S", artist="B", album="A", track=0)):
        assert not build_plan(cfg, conn, [(item, cls)]).ok


@needs_ffmpeg
def test_music_failed_extraction_rolls_back(cfg, conn, monkeypatch):
    item = music_item(cfg)
    plan = build_plan(cfg, conn, [(item, Classification(type="music", title="S", artist="B", album="A"))])
    assert plan.ok
    item.path.write_bytes(b"not a video")  # ffprobe passed at plan time; ffmpeg now fails
    with pytest.raises(FilingError, match="ffmpeg could not extract"):
        apply_plan(cfg, conn, plan)
    assert tree(cfg.music_dir) == set()
    assert conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 0


def test_music_rejects_unreadable_audio(cfg, conn):
    video = add_video(cfg, "Chan", "Broken", "bbbbbbbbbbb")
    plan = build_plan(cfg, conn, [(by_id(cfg)["bbbbbbbbbbb"],
                                   Classification(type="music", title="S", artist="B", album="A"))])
    assert not plan.ok and video.exists()


def test_filings_table_migrates_to_allow_music(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE filings (video_id TEXT PRIMARY KEY,
        type TEXT NOT NULL CHECK (type IN ('show', 'movie', 'video')), source_path TEXT NOT NULL,
        dest_path TEXT NOT NULL, show_id INTEGER, season INTEGER, episode INTEGER,
        metadata TEXT NOT NULL, manifest TEXT NOT NULL, filed_at TEXT NOT NULL);
        INSERT INTO filings VALUES ('v1', 'video', 's', 'd', NULL, NULL, NULL, '{}', '{}', 't');""")
    old.commit()
    old.close()
    migrated = connect(path)
    assert migrated.execute("SELECT video_id FROM filings").fetchone()[0] == "v1"
    migrated.execute("INSERT INTO filings VALUES ('v2', 'music', 's', 'd', NULL, NULL, NULL, '{}', '{}', 't')")
    migrated.close()
    connect(path).close()  # already migrated: a second connect changes nothing


@needs_ffmpeg
def test_music_form_prefills_and_files_over_http(cfg):
    item = music_item(cfg, title="Band - Big Hit (Official Video)", channel="VEVO", vid="hhhhhhhhhhh",
                      album="The Album", release_year=2018)
    app = create_app(cfg, start_background=False)
    web = app.test_client()
    page = web.get(f"/item/{item.video_id}/fields?type=music").get_data(as_text=True)
    assert 'value="Band"' in page and 'value="Big Hit"' in page
    assert 'value="The Album"' in page and 'value="2018"' in page
    posted = web.post(f"/item/{item.video_id}/file", data={
        "type": "music", "artist": "Band", "album": "The Album", "title": "Big Hit", "year": "2018", "track": ""})
    assert posted.status_code == 200 and "Filed 1 video" in posted.get_data(as_text=True)
    assert (cfg.music_dir / "Band" / "The Album" / "Big Hit.m4a").is_file()


# --- Discord "added" alerts ---------------------------------------------------------------------

def alert_cfg(cfg, **changes):
    return replace(cfg, discord_added_webhook_url="https://example.invalid/added", **changes)


def test_added_alert_for_a_movie_looks_like_a_seerr_embed(cfg, conn):
    add_video(cfg, "Chan", "Blazing Saddles", "mmmmmmmmmm1", date="1974-02-07")
    item = by_id(cfg)["mmmmmmmmmm1"]
    plan = file_one(cfg, conn, item, Classification(type="movie", title="Blazing Saddles", year=1974))
    sent = []
    assert notify_added(alert_cfg(cfg), plan, lambda url, embeds, images: sent.append((url, embeds, images)))
    (url, embeds, images), = sent
    assert url == "https://example.invalid/added"
    embed, = embeds
    assert embed["title"] == "Movie Added: Blazing Saddles (1974)"
    assert embed["description"] == "A & B plot"
    assert {f["name"]: f["value"] for f in embed["fields"]} == {"Channel": "Chan", "Library": "Movies"}
    assert embed["url"] == "https://www.youtube.com/watch?v=mmmmmmmmmm1" and embed["timestamp"]
    assert images == [("image0.jpg", b"jpg")] and embed["image"] == {"url": "attachment://image0.jpg"}


def test_added_alert_titles_for_each_type(cfg, conn):
    for vid in ("aaaaaaaaaa1", "aaaaaaaaaa2"):
        add_video(cfg, "Chan", vid, vid, thumb=False)
    items = by_id(cfg)
    plan = build_plan(cfg, conn, [
        (items["aaaaaaaaaa1"], Classification(type="show", title="Pilot", show="My Show", season=1, episode=2,
                                              show_year=2022)),
        (items["aaaaaaaaaa2"], Classification(type="video", title="Clip", name="A Clip"))])
    titles = [embed["title"] for embed, _thumb in added_embeds(plan)]
    assert titles == ["Episode Added: My Show S01E02 - Pilot", "Video Added: A Clip"]


def test_bulk_episodes_collapse_into_one_alert_per_show(cfg, conn):
    for n in range(12):
        add_video(cfg, "Chan", f"Ep {n}", f"bbbbbbbbb{n:02d}", date=f"2022-05-{n + 1:02d}")
    items = sorted(by_id(cfg).values(), key=lambda i: i.upload_date)
    plan = build_plan(cfg, conn, [(i, Classification(type="show", title=i.title, show="Bulk", season=1))
                                  for i in items])
    assert plan.ok
    (embed, _thumb), = added_embeds(plan)
    assert embed["title"] == "Episodes Added: Bulk (12)"
    lines = embed["description"].splitlines()
    assert lines[0] == "S01E01 · Ep 0" and lines[-1] == "…and 4 more"


def test_added_alert_needs_its_own_webhook_and_never_raises(cfg, conn):
    add_video(cfg, "Chan", "Film", "cccccccccc1")
    plan = file_one(cfg, conn, by_id(cfg)["cccccccccc1"], Classification(type="movie", title="Film", year=2022))

    def boom(*args):
        raise OSError("discord is down")

    assert notify_added(cfg, plan, boom) is False  # no added-alert webhook configured
    queue_only = replace(cfg, discord_webhook_url="https://example.invalid/queue")
    assert notify_added(queue_only, plan, boom) is False  # the queue webhook is not used for added alerts
    assert notify_added(alert_cfg(cfg), plan, boom) is False  # swallowed, logged


def test_added_alert_skips_missing_or_oversized_image(cfg, conn, monkeypatch):
    add_video(cfg, "Chan", "Film", "dddddddddd1")
    plan = file_one(cfg, conn, by_id(cfg)["dddddddddd1"], Classification(type="movie", title="Film", year=2022))
    plan.entries[0].item.thumb.unlink()
    sent = []
    notify_added(alert_cfg(cfg), plan, lambda url, embeds, images: sent.append((embeds, images)))
    embeds, images = sent[0]
    assert images == [] and "image" not in embeds[0]


@needs_ffmpeg
def test_filing_over_http_sends_the_alert(cfg, monkeypatch):
    import threading
    from youtarr_routarr import notify
    done = threading.Event()
    sent = []
    monkeypatch.setattr(notify, "_post_message",
                        lambda url, embeds, images: (sent.append(embeds[0]["title"]), done.set()))
    monkeypatch.setattr(notify.notify_added, "__defaults__", (notify._post_message, None))
    item = music_item(cfg, title="Band - Big Hit", channel="VEVO", vid="eeeeeeeeeee")
    web = create_app(alert_cfg(cfg), start_background=False).test_client()
    web.post(f"/item/{item.video_id}/file", data={
        "type": "music", "artist": "Band", "album": "Singles", "title": "Big Hit"})
    assert done.wait(5) and sent == ["Audio Added: Band - Big Hit"]


# --- Optional libraries -----------------------------------------------------------------------


def test_config_blank_or_unset_library_is_off():
    config = Config.from_env({"SHOWS_DIR": "  ", "MOVIES_DIR": "/m", "VIDEOS_DIR": ""})
    assert config.shows_dir is None and config.videos_dir is None and config.music_dir is None
    assert config.movies_dir == Path("/m") and config.queue_dir == Path("/media/youtarr-queue")
    assert config.enabled_types() == ("movie",) and config.default_type() == "movie"


def test_app_needs_at_least_one_library(cfg):
    off = replace(cfg, shows_dir=None, movies_dir=None, videos_dir=None, music_dir=None)
    with pytest.raises(RuntimeError, match="at least one"):
        create_app(off, start_background=False)


def test_disabled_library_is_hidden_rejected_and_not_health_checked(cfg):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    web = client(cfg, shows_dir=None, videos_dir=None)
    page = web.get("/").data.decode()
    assert 'value="movie" checked' in page
    assert 'value="show"' not in page and 'value="video"' not in page and "File as a show" not in page
    assert web.get("/healthz").status_code == 200
    response = web.post("/item/AAAAAAAAAAA/file", data={"type": "video", "title": "A", "name": "A"})
    assert b"The Videos library is turned off." in response.data
    from youtarr_routarr import group_key
    assert web.get(f"/group/{group_key('C')}").status_code == 404
    assert web.get("/item/AAAAAAAAAAA/episode").status_code == 404
    assert tree(cfg.videos_dir) == set()


def test_undo_works_after_the_library_is_turned_off(cfg, conn):
    add_video(cfg, "C", "A", "AAAAAAAAAAA")
    file_one(cfg, conn, by_id(cfg)["AAAAAAAAAAA"], Classification("show", "A", show="S"))
    assert undo_filing(replace(cfg, shows_dir=None), conn, "AAAAAAAAAAA") == []
    assert tree(cfg.shows_dir) == set()


# --- Audio-only downloads ---------------------------------------------------------------------


def add_audio(cfg, channel: str, title: str, vid: str) -> Path:
    folder = cfg.queue_dir / channel / f"{channel} - {title} - {vid}"
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / f"{channel} - {title} [{vid}].mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", str(audio)], check=True)
    return audio


@needs_ffmpeg
def test_scan_lists_an_audio_only_download(cfg):
    add_audio(cfg, "Chan", "Song", "AAAAAAAAAAA")
    item = by_id(cfg)["AAAAAAAAAAA"]
    assert item.audio_only and item.ext == ".mp3"


def test_scan_lists_a_video_and_mp3_pair_once(cfg):
    video = add_video(cfg, "C", "A", "AAAAAAAAAAA")
    video.with_suffix(".mp3").write_bytes(b"mp3")
    items = scan_queue(cfg.queue_dir, cfg.info_dir)
    assert [i.path for i in items] == [video] and not items[0].audio_only


def test_scan_hides_an_mp3_whose_video_is_in_another_folder(cfg):
    video = add_video(cfg, "C", "A", "AAAAAAAAAAA")
    other = cfg.queue_dir / "Other"
    other.mkdir()
    (other / "Other - A [AAAAAAAAAAA].mp3").write_bytes(b"mp3")
    assert [i.path for i in scan_queue(cfg.queue_dir, cfg.info_dir)] == [video]


@pytest.mark.parametrize("source_ext", [".m4a", ".opus"])
def test_scan_skips_an_mp3_still_being_converted(cfg, source_ext):
    folder = cfg.queue_dir / "C"
    folder.mkdir()
    (folder / "C - A [AAAAAAAAAAA].mp3").write_bytes(b"partial")
    (folder / f"C - A [AAAAAAAAAAA]{source_ext}").write_bytes(b"source")
    assert scan_queue(cfg.queue_dir, cfg.info_dir) == []


@needs_ffmpeg
def test_audio_only_files_as_audio_and_nothing_else(cfg, conn):
    source = add_audio(cfg, "Chan", "Song", "AAAAAAAAAAA")
    before = source.read_bytes()
    item = by_id(cfg)["AAAAAAAAAAA"]
    for kind in ("show", "movie", "video"):
        plan = build_plan(cfg, conn, [(item, Classification(kind, "Song", year=2020))])
        assert any("audio only" in e for e in plan.errors)
    plan = file_one(cfg, conn, item, Classification(type="music", title="Song", artist="Band", album="Album"))
    dest = cfg.music_dir / "Band" / "Album" / "Song.mp3"
    assert plan.entries[0].dest == dest and tags(dest)["artist"] == "Band"
    assert source.read_bytes() == before


@needs_ffmpeg
def test_audio_only_item_offers_only_audio_and_skips_the_group(cfg):
    add_audio(cfg, "Chan", "Song", "AAAAAAAAAAA")
    web = client(cfg)
    page = web.get("/").data.decode()
    assert 'value="music" checked' in page and 'name="artist"' in page and "Audio only" in page
    assert 'value="video"' not in page and "File as a show" not in page
    from youtarr_routarr import group_key
    assert web.get(f"/group/{group_key('Chan')}").status_code == 404
    assert web.get("/stats").get_json()["queue"] == 1


@needs_ffmpeg
def test_audio_only_item_with_audio_off_is_listed_with_a_note(cfg):
    add_audio(cfg, "Chan", "Song", "AAAAAAAAAAA")
    page = client(cfg, music_dir=None).get("/").data.decode()
    assert "Chan - Song" in page or "Song" in page
    assert "Turn on the Audio library (MUSIC_FOLDER) to file this." in page
    assert 'type="radio"' not in page and "/item/AAAAAAAAAAA/file" not in page


@needs_ffmpeg
def test_music_undo_refuses_when_the_source_is_gone(cfg, conn):
    item = music_item(cfg)
    plan = file_one(cfg, conn, item, Classification(type="music", title="Song", artist="Band", album="Album"))
    dest = plan.entries[0].dest
    item.path.unlink()
    with pytest.raises(FilingError, match="last copy"):
        undo_filing(cfg, conn, item.video_id)
    assert dest.exists() and conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0] == 1
    dest.unlink()  # nothing left to protect, so the record can be cleared
    assert undo_filing(cfg, conn, item.video_id) == []


# --- License ----------------------------------------------------------------------------------


def test_footer_links_to_the_source(cfg):
    page = client(cfg).get("/").data.decode()
    assert 'href="https://github.com/jesp1999/youtarr-routarr"' in page and ">Source<" in page


def test_image_ships_the_license():
    root = Path(__file__).resolve().parents[1]
    text = (root / "LICENSE").read_text()
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text and "Version 3, 19 November 2007" in text
    assert (root / "build" / "LICENSE").read_text() == text


@pytest.mark.parametrize("place", ["same", "parent", "child", "symlink"])
def test_app_refuses_a_library_that_overlaps_the_queue(cfg, tmp_path, place):
    if place == "symlink":
        (tmp_path / "alias").symlink_to(cfg.queue_dir, target_is_directory=True)
    overlap = {"same": cfg.queue_dir, "parent": cfg.queue_dir.parent, "child": cfg.queue_dir / "shows",
               "symlink": tmp_path / "alias"}[place]
    with pytest.raises(RuntimeError, match="SHOWS_DIR overlaps the queue"):
        create_app(replace(cfg, shows_dir=overlap), start_background=False)


def test_release_labels_the_image_with_the_same_license_as_the_dockerfile():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "build" / "Dockerfile").read_text()
    workflow = (root / ".github" / "workflows" / "release.yml").read_text()
    label = "org.opencontainers.image.licenses"
    assert f'{label}="AGPL-3.0-or-later"' in dockerfile
    # Without this, the metadata action labels the image from the repository's detected license.
    assert f"{label}=AGPL-3.0-or-later" in workflow


def _webm_and_mp3(cfg, with_video: bool) -> Path:
    folder = cfg.queue_dir / "C"
    folder.mkdir()
    webm = folder / "C - A [AAAAAAAAAAA].webm"
    video = ["-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-c:v", "libvpx"] if with_video else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *video, "-f", "lavfi", "-i", "sine=duration=1",
                    "-c:a", "libopus", "-shortest", str(webm)], check=True)
    webm.with_suffix(".mp3").write_bytes(b"partial")
    return webm


@needs_ffmpeg
def test_scan_hides_an_audio_only_webm_while_it_becomes_an_mp3(cfg):
    _webm_and_mp3(cfg, with_video=False)
    assert scan_queue(cfg.queue_dir, cfg.info_dir) == []


@needs_ffmpeg
def test_scan_still_lists_a_real_webm_video_that_has_an_mp3(cfg):
    webm = _webm_and_mp3(cfg, with_video=True)
    assert [i.path for i in scan_queue(cfg.queue_dir, cfg.info_dir)] == [webm]


def test_added_alert_names_the_library_after_its_folder(cfg, conn):
    folder = cfg.movies_dir.parent / "My_Film_Shelf"
    folder.mkdir()
    add_video(cfg, "Chan", "A", "AAAAAAAAAAA")
    plan = file_one(replace(cfg, movies_dir=folder), conn, by_id(cfg)["AAAAAAAAAAA"], Classification("movie", "A"))
    (embed, _thumb), = added_embeds(plan)
    assert {f["name"]: f["value"] for f in embed["fields"]}["Library"] == "My Film Shelf"
