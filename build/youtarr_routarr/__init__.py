from __future__ import annotations

import hashlib
import hmac
import re
import threading
from pathlib import Path

from flask import Flask, Response, abort, g, redirect, render_template, request, send_file, url_for

from . import health, ui
from .config import Config
from .db import connect
from .filing import (Classification, FilingError, apply_plan, build_plan, existing_artists, existing_shows,
                     next_episode, show_exists, suggest_movie_title, suggest_music, undo_filing, video_folders,
                     videos_base)
from .media import QueueItem, scan_queue, sort_key
from .notify import aged_ids, notify_added, record_seen, start_worker

# Linked in the page footer: the AGPL asks that people using a hosted copy can get its source.
# Change it if you run a modified version.
SOURCE_URL = "https://github.com/jesp1999/youtarr-routarr"
# Names reported by /healthz.
HEALTH_NAMES = {"show": "shows", "movie": "movies", "video": "videos", "music": "music"}
_write_lock = threading.Lock()


def _int(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise FilingError(f"Not a whole number: {value!r}") from exc


def _season_arg(value: str | None) -> int:
    """Season for suggestions only: blank or junk falls back to 1."""
    try:
        season = _int(value)
    except FilingError:
        return 1
    return 1 if season is None or season < 0 else season


# Titles that usually mean a Jellyfin "Specials" episode (Season 0). Only a pre-tick the user reviews.
SPECIAL_TITLE = re.compile(r"\b(?:special|bonus)\b", re.I)


def _match_show(name: str, shows: list[str]) -> str:
    """Prefer an existing show such as 'Vsauce (2015)' over a bare channel name."""
    pattern = re.compile(re.escape(name) + r"( \(\d{4}\))?", re.I)
    return next((show for show in shows if pattern.fullmatch(show)), name)


def _year_arg(value: str | None) -> int | None:
    try:
        return _int(value)
    except FilingError:
        return None


def _earliest_year(items: list[QueueItem]) -> int | None:
    dates = [item.upload_date for item in items if item.upload_date]
    return int(min(dates)[:4]) if dates else None


def group_key(channel: str) -> str:
    return hashlib.sha1(channel.encode("utf-8")).hexdigest()[:12]


def _check_libraries(cfg: Config) -> None:
    """A library at, above or inside the queue would let filing write into Youtarr's folder."""
    queue = cfg.queue_dir.resolve()
    for kind in cfg.enabled_types():
        library = cfg.library_dir(kind).resolve()
        if library.is_relative_to(queue) or queue.is_relative_to(library):
            name = f"{HEALTH_NAMES[kind].upper()}_DIR"
            raise RuntimeError(f"{name} overlaps the queue folder ({cfg.queue_dir}). "
                               "Give each library its own folder.")


def create_app(cfg: Config | None = None, start_background: bool = True) -> Flask:
    cfg = cfg or Config.from_env()
    if not cfg.auth_disabled and not (cfg.auth_user and cfg.auth_password):
        raise RuntimeError("Set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD, or AUTH_DISABLED=1.")
    if not cfg.enabled_types():
        raise RuntimeError("Set at least one of SHOWS_DIR, MOVIES_DIR, VIDEOS_DIR or MUSIC_DIR.")
    _check_libraries(cfg)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    connect(cfg.db_path).close()

    app = Flask(__name__)
    app.config["YOUTARR_ROUTARR"] = cfg
    app.jinja_env.globals["source_url"] = SOURCE_URL
    if start_background and cfg.discord_webhook_url:
        start_worker(cfg)

    def db():
        if "db" not in g:
            g.db = connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.before_request
    def guard():
        # Monitoring endpoints: health and bare counts, no titles or paths.
        if request.endpoint in ("healthz", "stats", "static"):
            return None
        if not cfg.auth_disabled:
            auth = request.authorization
            valid = (auth is not None and auth.type == "basic"
                     and hmac.compare_digest(auth.username or "", cfg.auth_user)
                     and hmac.compare_digest(auth.password or "", cfg.auth_password))
            if not valid:
                return Response("Authentication required.", 401,
                                {"WWW-Authenticate": 'Basic realm="YoutarrRoutarr"'})
        # Browsers resend basic auth on cross-site requests, so block those from changing anything.
        if request.method == "POST":
            site = request.headers.get("Sec-Fetch-Site")
            origin = request.headers.get("Origin")
            if site is not None:
                if site not in ("same-origin", "none"):
                    abort(403)
            elif origin and origin.split("://", 1)[-1] != request.host:
                abort(403)
        return None

    def pending() -> list[QueueItem]:
        filed = {row["video_id"] for row in db().execute("SELECT video_id FROM filings")}
        return [item for item in scan_queue(cfg.queue_dir, cfg.info_dir) if item.video_id not in filed]

    def pending_item(video_id: str) -> QueueItem:
        for item in pending():
            if item.video_id == video_id:
                return item
        abort(404)

    def field_context(item: QueueItem, kind: str, data=None) -> dict:
        """A form for one download. `values` holds the suggestions, overridden by anything posted."""
        if item.audio_only:
            kind = "music"
        elif kind not in cfg.enabled_types():
            kind = cfg.default_type()
        ctx = {"item": item, "kind": kind}
        if kind == "show":
            shows = existing_shows(cfg, db())
            show = _match_show(item.playlist or item.channel, shows)
            values = {"title": item.title, "show": show, "season": "1", "episode": "", "show_year": item.year or "",
                      "show_plot": "", "special": ""}
        elif kind == "movie":
            values = {"title": suggest_movie_title(item.title), "year": item.year or ""}
        elif kind == "music":
            suggested = suggest_music(item)
            values = {"artist": suggested["artist"], "album": suggested["album"], "title": suggested["title"],
                      "track": "", "year": suggested["year"] or ""}
            ctx.update(artists=existing_artists(cfg))
        else:
            folders = video_folders(cfg)
            folder = next((f for f in folders if f.casefold() == item.channel.casefold()), item.channel)
            values = {"folder": folder, "name": item.title, "title": item.title}
            # Short prefix like "videos/YouTube/"; the preview shows the full path.
            prefix = videos_base(cfg).relative_to(cfg.videos_dir.parent).as_posix()
            ctx.update(folders=folders, videos_prefix=prefix)
        if data is not None:
            values.update({key: data.get(key, "") for key in values if key in data or key == "special"})
        if kind == "show":
            name = str(values["show"]).strip() or item.channel
            season = 0 if values["special"] else _season_arg(str(values["season"]))
            suggested = next_episode(cfg, db(), name, season)
            if data is None:
                values["episode"] = suggested
            ctx.update(shows=shows, next_episode=values["episode"] or suggested,
                       new_show=not show_exists(cfg, db(), name), show_year=values["show_year"])
        ctx["values"] = values
        return ctx

    def from_data(item: QueueItem, data) -> Classification:
        season = _int(str(data.get("season") or "")) if data.get("season") else 1
        return Classification(
            type=data.get("type", ""),
            title=(str(data.get("title") or "") or item.title).strip(),
            year=_int(str(data.get("year") or "")),
            show=str(data.get("show") or "").strip() or None,
            season=0 if data.get("special") else season,
            episode=_int(str(data.get("episode") or "")),
            show_plot=str(data.get("show_plot") or "").strip() or None,
            show_year=_int(str(data.get("show_year") or "")),
            name=str(data.get("name") or "").strip() or None,
            folder=str(data.get("folder") or "").strip() or None,
            artist=str(data.get("artist") or "").strip() or None,
            album=str(data.get("album") or "").strip() or None,
            track=_int(str(data.get("track") or "")),
        )

    def planned(pairs_factory, bulk: bool) -> dict:
        """Plan without writing, for the preview under a form."""
        try:
            with _write_lock:
                plan = build_plan(cfg, db(), pairs_factory())
        except FilingError as exc:
            return {"errors": [str(exc)], "preview": None, "bulk": bulk}
        audio = any(entry.cls.type == "music" for entry in plan.entries)
        footnote = ui.FOOTNOTES["bulk" if bulk else "audio" if audio else "link"]
        return {"errors": plan.errors, "preview": ui.preview(plan, bulk), "bulk": bulk, "footnote": footnote}

    def file_pairs(pairs_factory):
        """File what was posted. Returns (plan, errors); nothing is written in dry-run mode."""
        try:
            pairs = pairs_factory()
            with _write_lock:
                plan = build_plan(cfg, db(), pairs)
                if plan.ok and not cfg.dry_run:
                    apply_plan(cfg, db(), plan)
                    if cfg.discord_added_webhook_url:
                        # Off the request thread: Discord being slow must not hold up the page.
                        threading.Thread(target=notify_added, args=(cfg, plan), daemon=True).start()
        except FilingError as exc:
            return None, [str(exc)]
        return plan, plan.errors

    def after_filing(order: list[str], video_ids: list[str]):
        target = {"item": ui.next_after(order, video_ids)}
        return redirect(url_for("queue", **{k: v for k, v in target.items() if v},
                                filed=",".join(video_ids)), code=303)

    def page(template: str, active: str, items: list[QueueItem] | None = None, **context):
        """A full page, with the navigation counts."""
        items = pending() if items is None else items
        nav = {"active": active, "queue": len(items),
               "history": db().execute("SELECT COUNT(*) FROM filings").fetchone()[0]}
        return render_template(template, nav=nav, dry_run=cfg.dry_run, **context)

    def bulk_context(chosen: list[QueueItem], data=None) -> dict:
        """The episode table for several downloads from one channel."""
        shows = existing_shows(cfg, db())
        show = _match_show(chosen[0].channel, shows)
        season = 1
        if data is not None:
            show = (data.get("show") or "").strip() or show
            season = _season_arg(data.get("season"))
        counters = {season: next_episode(cfg, db(), show, season), 0: next_episode(cfg, db(), show, 0)}
        start = counters[season]
        rows = []
        for item in chosen:
            if data is None:
                special = bool(SPECIAL_TITLE.search(item.title))  # a guess; the user reviews the ticks
                row_season = 0 if special else season
                rows.append((item, counters[row_season], item.title, special))
                counters[row_season] += 1
            else:
                rows.append((item, data.get(f"episode-{item.video_id}", ""),
                             data.get(f"title-{item.video_id}") or item.title,
                             data.get(f"special-{item.video_id}") is not None))
        return {
            "key": group_key(chosen[0].channel), "channel": chosen[0].channel, "rows": rows, "shows": shows,
            "show": show, "season": season, "start": (data or {}).get("start") or start,
            "show_plot": (data or {}).get("show_plot", ""),
            "show_year": (data or {}).get("show_year") or _earliest_year(chosen),
            "new_show": not show_exists(cfg, db(), show),
        }

    def bulk_pairs(chosen: list[QueueItem], bulk: dict) -> list[tuple[QueueItem, Classification]]:
        return [(item, Classification(
            type="show", show=bulk["show"] or None, season=0 if special else bulk["season"], title=title.strip(),
            episode=_int(str(episode)), show_plot=bulk["show_plot"] or None,
            show_year=_int(str(bulk["show_year"] or ""))))
            for item, episode, title, special in bulk["rows"]]

    def render_queue(selected: list[str], kind: str = "", data=None, outcome=None, toast=None):
        """The inbox: the grouped queue, plus a panel for whatever is selected."""
        items = pending()
        ids = [item.video_id for item in items]
        record_seen(db(), ids)
        now = _utcnow()
        aged = aged_ids(db(), ids, cfg.queue_ping_days, now)
        by_id = {item.video_id: item for item in items}
        chosen = [by_id[video_id] for video_id in selected if video_id in by_id]
        if len(chosen) > 1:
            # Bulk filing is for episodes of one channel; audio-only downloads cannot be episodes.
            channel = chosen[0].channel
            episodes = [i for i in chosen if i.channel == channel and not i.audio_only]
            chosen = episodes if cfg.shows_dir is not None and episodes else chosen[:1]
            chosen = sorted(chosen, key=sort_key)
        order = ui.display_order(items)
        panel = {"mode": "empty"}
        if len(chosen) == 1:
            item = chosen[0]
            if item.audio_only and cfg.music_dir is None:
                panel = {"mode": "blocked", "item": item}
            else:
                ctx = field_context(item, kind, data)
                posted = {**ctx["values"], "type": ctx["kind"]}
                panel = {"mode": "single", "ctx": ctx, "item": item,
                         "outcome": outcome or planned(lambda: [(item, from_data(item, posted))], False)}
        elif chosen:
            bulk = bulk_context(chosen, data)
            panel = {"mode": "bulk", "bulk": bulk,
                     "outcome": outcome or planned(lambda: bulk_pairs(chosen, bulk), True)}
        picked = [item.video_id for item in chosen]
        return page(
            "queue.html", "queue", items=items, groups=ui.groups(items), panel=panel, selected=picked,
            skip=ui.next_after(order, picked), toast=toast, ages=ui.ages(db(), now), aged=set(aged),
            types=[(k, ui.TYPE_LABELS[k]) for k in cfg.enabled_types()],
            default_label=ui.TYPE_LABELS[cfg.default_type()], shows_on=cfg.shows_dir is not None,
            music_on=cfg.music_dir is not None, ping_days=cfg.queue_ping_days)

    @app.get("/healthz")
    def healthz():
        folders = [("queue", cfg.queue_dir)] + [(HEALTH_NAMES[kind], cfg.library_dir(kind))
                                                for kind in cfg.enabled_types()]
        problems = [name for name, path in folders if not path.is_dir()]
        db().execute("SELECT 1").fetchone()
        if problems:
            return {"status": "degraded", "missing": problems}, 503
        return {"status": "ok"}

    @app.get("/stats")
    def stats():
        """Counts for the Homepage widget."""
        items = pending()
        ids = [item.video_id for item in items]
        filed_count = db().execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        return {"queue": len(items), "waiting": len(aged_ids(db(), ids, cfg.queue_ping_days, _utcnow())),
                "filed": filed_count, "ping_days": cfg.queue_ping_days}

    @app.get("/")
    def queue():
        """Selection lives in the address: ?item=<id> or ?ids=a,b,c (ids may also repeat)."""
        selected = [video_id for value in request.args.getlist("ids") for video_id in value.split(",") if video_id]
        if request.args.get("item"):
            selected = [request.args["item"]]
        filed_ids = [v for v in request.args.get("filed", "").split(",") if v]
        return render_queue(list(dict.fromkeys(selected)), kind=request.args.get("type", ""),
                            toast=ui.filed_toast(db(), filed_ids))

    @app.get("/item/<video_id>/fields")
    def fields(video_id: str):
        return render_template("_fields_inner.html",
                               ctx=field_context(pending_item(video_id), request.args.get("type", "show")))

    @app.get("/item/<video_id>/episode")
    def episode_field(video_id: str):
        """Re-suggest the episode number, and ask for a year if the show is new."""
        require_shows()
        item = pending_item(video_id)
        show = (request.args.get("show") or "").strip() or item.playlist or item.channel
        season = 0 if request.args.get("special") else _season_arg(request.args.get("season"))
        return render_template("_episode.html", item=item,
                               next_episode=next_episode(cfg, db(), show, season),
                               new_show=not show_exists(cfg, db(), show),
                               show_year=_year_arg(request.args.get("show_year")) or item.year)

    @app.get("/thumb/<video_id>")
    def thumb(video_id: str):
        item = pending_item(video_id)
        if not item.thumb:
            abort(404)
        return send_file(item.thumb, max_age=3600)

    @app.post("/item/<video_id>/preview")
    def preview(video_id: str):
        item = pending_item(video_id)
        outcome = planned(lambda: [(item, from_data(item, request.form))], False)
        return render_template("_preview.html", outcome=outcome)

    @app.post("/item/<video_id>/file")
    def file_item(video_id: str):
        item = pending_item(video_id)
        order = ui.display_order(pending())
        plan, errors = file_pairs(lambda: [(item, from_data(item, request.form))])
        if not errors and not cfg.dry_run:
            return after_filing(order, [video_id])
        toast = None
        if not errors:
            toast = {"text": f"Dry run — would file “{plan.entries[0].display}”. Nothing written.", "undo": None}
        outcome = planned(lambda: [(item, from_data(item, request.form))], False)
        outcome["errors"] = errors or outcome["errors"]
        outcome["refused"] = bool(errors)
        return render_queue([video_id], kind=request.form.get("type", ""), data=request.form,
                            outcome=outcome, toast=toast)

    def require_shows() -> None:
        if cfg.shows_dir is None:
            abort(404)

    def group_items(key: str) -> tuple[str, list[QueueItem]]:
        require_shows()
        items = sorted((i for i in pending() if group_key(i.channel) == key and not i.audio_only), key=sort_key)
        if not items:
            abort(404)
        return items[0].channel, items

    @app.get("/group/<key>")
    def group(key: str):
        """Old links to a channel's bulk page now select the whole channel in the inbox."""
        _channel, items = group_items(key)
        return redirect(url_for("queue", ids=",".join(item.video_id for item in items)))

    @app.get("/group/<key>/rows")
    def group_rows(key: str):
        """Renumber the table after a field changes, keeping edited titles and ticks."""
        channel, items = group_items(key)
        args = request.args
        show = (args.get("show") or "").strip() or channel
        season = _season_arg(args.get("season"))
        numbers = {season: next_episode(cfg, db(), show, season), 0: next_episode(cfg, db(), show, 0)}
        # A new show or season starts from its next free episode; otherwise "From ep." wins.
        reset = request.headers.get("HX-Trigger-Name") in ("show", "season")
        start = None if reset else _year_arg(args.get("start"))
        if start is not None:
            numbers[season] = start
        chosen = [item for item in items if args.get(f"include-{item.video_id}") is not None]
        year = _year_arg(args.get("show_year")) or _earliest_year(chosen or items)
        first = numbers[season]
        rows = []
        for item in chosen:
            special = args.get(f"special-{item.video_id}") is not None
            row_season = 0 if special else season
            rows.append((item, numbers[row_season], args.get(f"title-{item.video_id}") or item.title, special))
            numbers[row_season] += 1
        bulk = {"key": key, "rows": rows, "season": season, "start": first}
        # The other fields sit outside the table, so they are swapped out-of-band. Keep the tbody
        # first: htmx parses inside a <template>, and a leading span makes the parser drop the tbody.
        html = (render_template("_group_rows.html", bulk=bulk)
                + render_template("_show_year.html", oob=True, show_year=year,
                                  new_show=not show_exists(cfg, db(), show)))
        if reset:
            html += render_template("_bulk_start.html", oob=True, bulk=bulk)
        return html

    def posted_bulk(key: str) -> tuple[list[QueueItem], dict]:
        _channel, items = group_items(key)
        chosen = [item for item in items if request.form.get(f"include-{item.video_id}") is not None]
        if not chosen:
            raise FilingError("Select at least one video.")
        bulk = bulk_context(chosen, request.form)
        bulk["season"] = _int(request.form.get("season")) if request.form.get("season") else 1
        bulk["show"] = (request.form.get("show") or "").strip() or None
        bulk["show_year"] = request.form.get("show_year", "")
        return chosen, bulk

    def group_pairs(key: str) -> list[tuple[QueueItem, Classification]]:
        return bulk_pairs(*posted_bulk(key))

    @app.post("/group/<key>/preview")
    def group_preview(key: str):
        return render_template("_preview.html", outcome=planned(lambda: group_pairs(key), True))

    @app.post("/group/<key>/file")
    def group_file(key: str):
        order = ui.display_order(pending())
        plan, errors = file_pairs(lambda: group_pairs(key))
        if not errors and not cfg.dry_run:
            return after_filing(order, [i for i in order if i in {e.item.video_id for e in plan.entries}])
        toast = None
        if not errors:
            count = len(plan.entries)
            toast = {"text": f"Dry run — would file {count} item{'s' if count != 1 else ''}. Nothing written.",
                     "undo": None}
        selected = [k.removeprefix("include-") for k in request.form if k.startswith("include-")]
        if not selected:
            return render_queue([], toast={"text": "Select at least one video.", "undo": None})
        if len(selected) == 1:
            return render_queue(selected, kind="show", toast=toast or {"text": " ".join(errors), "undo": None})
        outcome = planned(lambda: group_pairs(key), True)
        outcome["errors"] = errors or outcome["errors"]
        outcome["refused"] = bool(errors)
        return render_queue(selected, data=request.form, outcome=outcome, toast=toast)

    def history_page(error=None, kept=None):
        return page("filed.html", "history", rows=ui.history(cfg, db()), error=error, kept=kept)

    @app.get("/filed")
    def filed():
        return history_page()

    @app.post("/filed/<video_id>/undo")
    def undo(video_id: str):
        error, kept = None, None
        if cfg.dry_run:
            error = "Dry-run mode is on, so nothing was undone."
        else:
            try:
                with _write_lock:
                    kept = undo_filing(cfg, db(), video_id)
            except FilingError as exc:
                error = str(exc)
        if not error and request.form.get("then") == "reclassify":
            return redirect(url_for("queue", item=video_id), code=303)
        return history_page(error, kept)

    @app.get("/settings")
    def settings():
        items = pending()
        ids = [item.video_id for item in items]
        waiting = len(aged_ids(db(), ids, cfg.queue_ping_days, _utcnow()))
        return page("settings.html", "settings", items=items, cards=health.library_cards(cfg, db()),
                    checks=health.checks(cfg, db(), waiting), environment=health.environment(cfg))

    return app


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
