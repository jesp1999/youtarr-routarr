from __future__ import annotations

import base64
import errno
import os
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
from youtarr_routarr.notify import check_queue  # noqa: E402

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
        videos_dir=tmp_path / "Videos", info_dir=tmp_path / "info", db_path=tmp_path / "state" / "t.sqlite3",
        dry_run=False, auth_disabled=True)
    for path in (config.queue_dir, config.shows_dir, config.movies_dir, config.videos_dir,
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


def test_backfill_upgrades_old_nfos_and_keeps_undo_working(cfg, conn):
    import hashlib, json
    from youtarr_routarr import backfill
    add_video(cfg, "C", "Old - a short film by Ann Lee", "AAAAAAAAAAA", date="2025-01-01")
    add_video(cfg, "C", "Edited", "BBBBBBBBBBB", date="2025-01-01")
    add_info(cfg, "AAAAAAAAAAA", "Old - a short film by Ann Lee")
    items = by_id(cfg)
    file_one(cfg, conn, items["AAAAAAAAAAA"], Classification("movie", "Old"))
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("movie", "Edited"))
    old = cfg.movies_dir / "Old (2025)"
    # Make A look like a filing from before this change: bare NFO, no fanart.
    bare = b"<?xml version='1.0' encoding='utf-8'?>\n<movie>\n  <title>Old</title>\n</movie>\n"
    (old / "Old (2025).nfo").write_bytes(bare)
    (old / "fanart.jpg").unlink()
    row = conn.execute("SELECT manifest FROM filings WHERE video_id='AAAAAAAAAAA'").fetchone()
    manifest = json.loads(row["manifest"])
    manifest["files"] = [r for r in manifest["files"] if r["role"] != "fanart"]
    for r in manifest["files"]:
        if r["role"] == "nfo":
            r["sha256"] = hashlib.sha256(bare).hexdigest()
    with conn:
        conn.execute("UPDATE filings SET manifest=? WHERE video_id='AAAAAAAAAAA'", (json.dumps(manifest),))
    # B was hand-edited after filing: must be left alone.
    edited = cfg.movies_dir / "Edited (2025)" / "Edited (2025).nfo"
    edited.write_bytes(b"<movie><title>Mine</title></movie>")

    backfill.main([], cfg)
    assert (old / "Old (2025).nfo").read_bytes() == bare and not (old / "fanart.jpg").exists()
    backfill.main(["--apply"], cfg)
    root = ET.parse(old / "Old (2025).nfo").getroot()
    assert root.findtext("director") == "Ann Lee" and root.findtext("lockdata") == "true"
    assert (old / "fanart.jpg").exists() and edited.read_bytes() == b"<movie><title>Mine</title></movie>"
    backfill.main(["--apply"], cfg)  # idempotent
    assert ET.parse(old / "Old (2025).nfo").getroot().findall("lockdata").__len__() == 1

    assert undo_filing(cfg, conn, "AAAAAAAAAAA") == [] and not old.exists()


def test_relocate_moves_app_folders_and_keeps_undo_working(cfg, conn, tmp_path):
    from youtarr_routarr import relocate
    add_video(cfg, "Chan", "E1", "AAAAAAAAAAA", date="2020-01-01")
    add_video(cfg, "Chan", "E2", "BBBBBBBBBBB", date="2020-02-01")
    add_video(cfg, "C", "Film", "CCCCCCCCCCC", date="2021-01-01")
    add_video(cfg, "C", "Theirs", "DDDDDDDDDDD", date="2021-01-01")
    (cfg.shows_dir / "Sonarr Show (1999)").mkdir()  # pre-existing, owned by Sonarr
    items = by_id(cfg)
    file_one(cfg, conn, items["AAAAAAAAAAA"], Classification("show", "E1", show="New Show"))
    file_one(cfg, conn, items["BBBBBBBBBBB"], Classification("show", "E2", show="New Show"))
    file_one(cfg, conn, items["CCCCCCCCCCC"], Classification("movie", "Film"))
    file_one(cfg, conn, items["DDDDDDDDDDD"], Classification("show", "X", show="Sonarr Show (1999)"))
    (cfg.movies_dir / "Clash (2021)").mkdir()

    new = replace(cfg, shows_dir=tmp_path / "YT_Shows", movies_dir=tmp_path / "YT_Movies")
    old_shows, old_movies = cfg.shows_dir, cfg.movies_dir
    args = ["--old-shows", str(old_shows), "--old-movies", str(old_movies)]
    assert relocate.main(args, new) == 1  # new roots missing
    new.shows_dir.mkdir(); new.movies_dir.mkdir()
    with pytest.raises(SystemExit):
        relocate.main(["--old-shows", str(new.shows_dir), "--old-movies", str(old_movies)], new)

    relocate.main(args, new)  # dry run
    assert (old_shows / "New Show (2020)").is_dir() and not any(new.shows_dir.iterdir())
    assert relocate.main([*args, "--apply"], new) == 0
    assert (new.shows_dir / "New Show (2020)" / "Season 01" / "New Show - S01E02 - E2.mp4").exists()
    assert (new.movies_dir / "Film (2021)" / "Film (2021).mp4").exists()
    assert (old_shows / "Sonarr Show (1999)" / "Season 01").is_dir()  # not ours: stays
    assert (old_movies / "Clash (2021)").is_dir()
    assert items["AAAAAAAAAAA"].path.stat().st_nlink == 2
    row = conn.execute("SELECT dest_path, manifest FROM filings WHERE video_id='CCCCCCCCCCC'").fetchone()
    assert row["dest_path"].startswith(str(new.movies_dir)) and str(old_movies) not in row["manifest"]
    relocate.main([*args, "--apply"], new)  # idempotent
    assert (new.movies_dir / "Film (2021)").is_dir()

    # Undo and next-episode work against the new root.
    from youtarr_routarr.filing import next_episode
    assert next_episode(new, conn, "New Show", 1) == 3
    assert undo_filing(new, conn, "CCCCCCCCCCC") == [] and not (new.movies_dir / "Film (2021)").exists()
    undo_filing(new, conn, "AAAAAAAAAAA"); undo_filing(new, conn, "BBBBBBBBBBB")
    assert not (new.shows_dir / "New Show (2020)").exists()
    assert items["CCCCCCCCCCC"].path.exists() and items["AAAAAAAAAAA"].path.stat().st_nlink == 1


def test_relocate_conflict_leaves_folder_in_place(cfg, conn, tmp_path):
    from youtarr_routarr import relocate
    add_video(cfg, "C", "Film", "CCCCCCCCCCC", date="2021-01-01")
    file_one(cfg, conn, by_id(cfg)["CCCCCCCCCCC"], Classification("movie", "Film"))
    new = replace(cfg, shows_dir=tmp_path / "YS", movies_dir=tmp_path / "YM")
    new.shows_dir.mkdir(); (new.movies_dir / "Film (2021)").mkdir(parents=True)
    rc = relocate.main(["--old-shows", str(cfg.shows_dir), "--old-movies", str(cfg.movies_dir), "--apply"], new)
    assert rc == 1 and (cfg.movies_dir / "Film (2021)" / "Film (2021).mp4").exists()
    assert conn.execute("SELECT dest_path FROM filings").fetchone()[0].startswith(str(cfg.movies_dir))
