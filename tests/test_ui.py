"""The inbox interface: selection by URL, preview, filing redirects, history and settings."""
import re
from dataclasses import replace
from pathlib import Path

import pytest

from test_youtarr_routarr import (add_audio, add_video, alert_cfg, by_id, cfg, client, conn, file_one,  # noqa: F401
                                  music_item, needs_ffmpeg, tree)
from youtarr_routarr import group_key
from youtarr_routarr.filing import Classification

A, B, C = "AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"


def html(response) -> str:
    return response.get_data(as_text=True)


def two_channels(cfg):
    add_video(cfg, "Chan", "First", A, date="2010-01-01")
    add_video(cfg, "Chan", "Bonus Special", B, date="2011-01-01")
    add_video(cfg, "Other", "Elsewhere", C, date="2012-01-01")


def test_queue_groups_rows_and_starts_with_an_empty_panel(cfg):
    two_channels(cfg)
    page = html(client(cfg).get("/"))
    assert "3 waiting" in page and "older than" not in page
    assert "Pick a download" in page and 'id="preview"' not in page
    assert f'href="/?item={A}"' in page and f'name="ids" value="{A}"' in page
    assert "Select all 2" in page and f'href="/?ids={A},{B}"' in page
    assert "2010-01-01 · today" in page


def test_old_downloads_are_marked_waiting(cfg, conn):
    add_video(cfg, "Chan", "Old", A)
    with conn:
        conn.execute("INSERT INTO queue_seen VALUES (?, '2000-01-01T00:00:00+00:00')", (A,))
    page = html(client(cfg).get("/"))
    assert "1 older than 7 days" in page and re.search(r"waiting \d+d", page)


def test_empty_queue_says_so(cfg):
    assert "Queue is empty. New Youtarr downloads land here." in html(client(cfg).get("/"))


def test_selecting_an_item_shows_its_form_and_a_real_preview(cfg):
    add_video(cfg, "Chan", "A Clip", A, subs=True)
    page = html(client(cfg).get(f"/?item={A}"))
    assert 'id="panel-form"' in page and f'action="/item/{A}/file"' in page
    assert 'value="video" checked' in page and 'name="folder" value="Chan"' in page
    assert "Will write · 5 files" in page  # video, nfo, poster, fanart, subtitle
    assert "YouTube/Chan/A Clip/A Clip.mp4" in page and "hardlink" in page
    assert tree(cfg.videos_dir) == set()


def test_item_page_takes_a_type_and_previews_it(cfg):
    add_video(cfg, "Chan", "Film", A, date="2021-03-04")
    page = html(client(cfg).get(f"/?item={A}&type=show"))
    assert 'value="show" checked' in page and 'name="special"' in page
    assert "Chan - S01E01 - Film.mp4" in page and "tvshow.nfo" in page and "if new" in page


def test_unknown_or_already_filed_item_leaves_the_panel_empty(cfg):
    add_video(cfg, "Chan", "A", A)
    response = client(cfg).get("/?item=ZZZZZZZZZZZ")
    assert response.status_code == 200 and "Pick a download" in html(response)


def test_several_ids_show_the_bulk_panel_for_one_channel(cfg):
    two_channels(cfg)
    page = html(client(cfg).get(f"/?ids={A},{B},{C}"))
    assert "Chan · 2 downloads" in page and "File as show episodes" in page
    assert f'name="include-{A}"' in page and f'name="include-{B}"' in page and f"include-{C}" not in page
    assert f'name="special-{B}" checked' in page and 'name="start" ' in page
    assert "S00E" in page and "Chan - S01E01 - First.mp4" in page and "+ nfo, thumb" in page
    assert "Specials never set the show" in page


def test_checkbox_form_style_ids_work_too(cfg):
    two_channels(cfg)
    assert "Chan · 2 downloads" in html(client(cfg).get(f"/?ids={A}&ids={B}"))


def test_one_id_falls_back_to_the_single_panel(cfg):
    two_channels(cfg)
    assert f'action="/item/{A}/file"' in html(client(cfg).get(f"/?ids={A}"))


def test_old_group_link_redirects_to_the_selection(cfg):
    two_channels(cfg)
    response = client(cfg).get(f"/group/{group_key('Chan')}")
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/?ids={A},{B}")


def test_from_episode_renumbers_and_a_new_show_resets_it(cfg):
    two_channels(cfg)
    web = client(cfg)
    url = f"/group/{group_key('Chan')}/rows"
    query = {"show": "Chan", "season": "1", "start": "7", f"include-{A}": "on", f"include-{B}": "on"}
    kept = html(web.get(url, query_string=query, headers={"HX-Trigger-Name": "start"}))
    assert f'name="episode-{A}" value="7"' in kept and f'name="episode-{B}" value="8"' in kept
    assert f"include-{C}" not in kept
    reset = html(web.get(url, query_string=query, headers={"HX-Trigger-Name": "show"}))
    assert f'name="episode-{A}" value="1"' in reset
    assert 'name="start"' in reset and 'hx-swap-oob="true"' in reset


def test_special_checkbox_files_a_single_episode_into_season_zero(cfg):
    add_video(cfg, "Chan", "A", A, date="2020-01-01")
    web = client(cfg)
    assert 'value="1"' in html(web.get(f"/item/{A}/episode?show=Chan&season=3&special=on"))
    web.post(f"/item/{A}/file", data={"type": "show", "title": "A", "show": "Chan", "season": "3",
                                      "special": "on", "show_year": "2020"})
    assert (cfg.shows_dir / "Chan (2020)" / "Season 00" / "Chan - S00E01 - A.mp4").exists()


def test_filing_redirects_to_the_next_item_with_a_toast_and_undo(cfg):
    two_channels(cfg)
    web = client(cfg)
    done = web.post(f"/item/{A}/file", data={"type": "movie", "title": "First", "year": "2010"})
    assert done.status_code == 303 and done.headers["Location"].endswith(f"/?item={B}&filed={A}")
    page = html(web.get(done.headers["Location"]))
    assert "Filed “First”." in page and f'action="/filed/{A}/undo"' in page


def test_filing_the_last_item_lands_on_the_empty_queue(cfg):
    add_video(cfg, "Chan", "A", A)
    done = client(cfg).post(f"/item/{A}/file", data={"type": "movie", "title": "A", "year": "2020"})
    assert done.headers["Location"].endswith(f"/?filed={A}")


def test_bulk_filing_redirects_past_everything_filed(cfg):
    two_channels(cfg)
    web = client(cfg)
    done = web.post(f"/group/{group_key('Chan')}/file", data={
        "show": "Chan", "season": "1", f"include-{A}": "on", f"include-{B}": "on",
        f"episode-{A}": "1", f"episode-{B}": "2", f"title-{A}": "First", f"title-{B}": "Second"})
    assert done.status_code == 303
    assert done.headers["Location"].endswith(f"/?item={C}&filed={A},{B}")
    assert "Filed 2 items." in html(web.get(done.headers["Location"]))


def test_dry_run_keeps_the_form_and_says_nothing_was_written(cfg):
    add_video(cfg, "Chan", "A", A)
    web = client(cfg, dry_run=True)
    page = html(web.get(f"/?item={A}"))
    assert "Preview only — dry run" in page and "DRY_RUN=1" in page and "docker compose up -d" in page
    response = web.post(f"/item/{A}/file", data={"type": "video", "name": "Renamed", "title": "A"})
    page = html(response)
    assert response.status_code == 200 and "Dry run — would file “Renamed”. Nothing written." in page
    assert 'name="name" value="Renamed"' in page and "Renamed.mp4" in page
    assert tree(cfg.videos_dir) == set()


def test_filing_errors_keep_what_was_typed(cfg):
    add_video(cfg, "Chan", "A", A)
    (cfg.videos_dir / "YouTube" / "Taken").mkdir(parents=True)
    response = client(cfg).post(f"/item/{A}/file", data={"type": "video", "name": "Taken", "title": "A"})
    page = html(response)
    assert response.status_code == 200 and "Folder already exists" in page and 'value="Taken"' in page


def test_skip_points_at_the_next_item(cfg):
    two_channels(cfg)
    page = html(client(cfg).get(f"/?item={C}"))  # last in order, so it wraps
    assert re.search(rf'href="/\?item={A}"[^>]*>Skip<', page)


def test_history_lists_filings_and_says_when_undo_is_impossible(cfg, conn):
    kept = add_video(cfg, "Chan", "Keep", A)
    gone = add_video(cfg, "Chan", "Gone", B)
    items = by_id(cfg)
    file_one(cfg, conn, items[A], Classification("movie", "Keep", year=2020))
    file_one(cfg, conn, items[B], Classification("movie", "Gone", year=2020))
    gone.unlink()
    page = html(client(cfg).get("/filed"))
    assert "History" in page and "Keep (2020)/Keep (2020).mp4" in page
    assert f'action="/filed/{A}/undo"' in page and f'action="/filed/{B}/undo"' not in page
    assert "Source gone — can’t undo" in page and kept.exists()


def test_settings_show_checks_and_never_secrets(cfg):
    secret = dict(auth_disabled=False, auth_user="admin", auth_password="hunter2",
                  discord_webhook_url="https://discord.invalid/api/webhooks/1/SECRETTOKEN")
    web = client(cfg, music_dir=None, **secret)
    import base64
    headers = {"Authorization": "Basic " + base64.b64encode(b"admin:hunter2").decode()}
    assert web.get("/settings").status_code == 401
    page = html(web.get("/settings", headers=headers))
    assert "hunter2" not in page and "SECRETTOKEN" not in page
    assert "DISCORD_WEBHOOK_URL" in page and ">set<" in page and "BASIC_AUTH_USER" in page
    assert "BASIC_AUTH_PASSWORD" not in page
    for text in ("Queue and libraries share one mount", "Youtarr info folder", "writable",
                 "No library overlaps the queue folder", "waiting over 7 days", "Filing alerts"):
        assert text in page
    assert str(cfg.shows_dir) in page and "Music" not in re.sub(r"MUSIC_DIR", "", page)


def test_navigation_shows_counts_and_the_live_tag(cfg, conn):
    two_channels(cfg)
    file_one(cfg, conn, by_id(cfg)[C], Classification("movie", "C", year=2020))
    page = html(client(cfg).get("/"))
    assert re.search(r"Queue\s*<span[^>]*>2<", page) and re.search(r"History\s*<span[^>]*>1<", page)
    assert ">Live<" in page and "Sign out" not in page


def test_assets_are_local_and_old_stylesheet_is_gone(cfg):
    web = client(cfg)
    page = html(web.get("/"))
    assert "css/nocturne.css" in page and "css/app.css" in page and "style.css" not in page
    static = Path(__file__).resolve().parents[1] / "build" / "youtarr_routarr" / "static"
    styles = (static / "css" / "nocturne.css").read_text() + (static / "css" / "app.css").read_text()
    assert "googleapis" not in styles and "fonts/InterVariable.woff2" in styles
    assert web.get("/static/fonts/InterVariable.woff2").status_code == 200
    app_css = (static / "css" / "app.css").read_text()
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b(?![\w-])", app_css), "app.css must use design tokens, not hex"


@pytest.mark.parametrize("query", [f"item={A}", f"ids={A},{B}"])
def test_pressing_enter_in_a_field_cannot_file(cfg, query):
    two_channels(cfg)
    page = html(client(cfg).get(f"/?{query}"))
    form = page[page.index('id="panel-form"'):]
    # A disabled first submit button switches off the browser's submit-on-Enter, with or without JavaScript.
    assert '<button type="submit" disabled hidden></button>' in form
    assert form.index('<button type="submit" disabled hidden>') < form.index("<input")


def test_dry_run_bulk_keeps_the_table_and_writes_nothing(cfg):
    two_channels(cfg)
    response = client(cfg, dry_run=True).post(f"/group/{group_key('Chan')}/file", data={
        "show": "Chan", "season": "1", f"include-{A}": "on", f"include-{B}": "on",
        f"episode-{A}": "4", f"episode-{B}": "5", f"title-{A}": "Kept title", f"title-{B}": "Other"})
    page = html(response)
    assert response.status_code == 200 and "Dry run — would file 2 items. Nothing written." in page
    assert 'value="Kept title"' in page and f'name="episode-{A}" value="4"' in page and "S01E04" in page
    assert tree(cfg.shows_dir) == set()


def test_bulk_errors_keep_what_was_typed(cfg):
    two_channels(cfg)
    response = client(cfg).post(f"/group/{group_key('Chan')}/file", data={
        "show": "Chan", "season": "x", f"include-{A}": "on", f"include-{B}": "on", f"title-{A}": "Kept title"})
    page = html(response)
    assert response.status_code == 200 and "Not a whole number" in page and 'value="Kept title"' in page
    assert tree(cfg.shows_dir) == set()


@needs_ffmpeg
def test_audio_only_item_disables_the_other_types(cfg):
    add_audio(cfg, "Chan", "Song", A)
    page = html(client(cfg).get(f"/?item={A}"))
    assert 'value="music" checked' in page and 'value="video" disabled' in page
    assert "MP3-only download — can only be filed as audio." in page
    assert "audio copied + tagged" in page and f'name="ids" value="{A}"' not in page
    assert "MP3 only" in page


@needs_ffmpeg
def test_audio_only_item_with_audio_off_explains_why(cfg):
    add_audio(cfg, "Chan", "Song", A)
    page = html(client(cfg, music_dir=None).get(f"/?item={A}"))
    assert "Turn on the Audio library (MUSIC_FOLDER) to file this." in page and 'id="panel-form"' not in page
