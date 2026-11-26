from __future__ import annotations

import hashlib
import hmac
import re
import threading
from pathlib import Path

from flask import Flask, Response, abort, g, make_response, redirect, render_template, request, send_file, url_for

from .config import Config
from .db import connect
from .filing import (Classification, FilingError, apply_plan, build_plan, existing_artists, existing_shows,
                     next_episode, show_exists, suggest_movie_title, suggest_music, undo_filing, video_folders,
                     videos_base)
from .media import QueueItem, scan_queue, sort_key
from .notify import aged_ids, notify_added, record_seen, start_worker

# Radio button labels on the queue page.
TYPE_LABELS = {"show": "Show episode", "movie": "Movie", "video": "Video", "music": "Audio"}
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


def create_app(cfg: Config | None = None, start_background: bool = True) -> Flask:
    cfg = cfg or Config.from_env()
    if not cfg.auth_disabled and not (cfg.auth_user and cfg.auth_password):
        raise RuntimeError("Set BASIC_AUTH_USER and BASIC_AUTH_PASSWORD, or AUTH_DISABLED=1.")
    if not cfg.enabled_types():
        raise RuntimeError("Set at least one of SHOWS_DIR, MOVIES_DIR, VIDEOS_DIR or MUSIC_DIR.")
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    connect(cfg.db_path).close()

    app = Flask(__name__)
    app.config["YOUTARR_ROUTARR"] = cfg
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

    def field_context(item: QueueItem, kind: str) -> dict:
        kind = kind if kind in cfg.enabled_types() else cfg.default_type()
        show = item.playlist or item.channel
        ctx = {"item": item, "kind": kind, "show": show}
        if kind == "show":
            shows = existing_shows(cfg, db())
            show = _match_show(show, shows)
            ctx.update(show=show, shows=shows, next_episode=next_episode(cfg, db(), show, 1),
                       new_show=not show_exists(cfg, db(), show), show_year=item.year)
        elif kind == "movie":
            ctx.update(movie_title=suggest_movie_title(item.title))
        elif kind == "music":
            ctx.update(artists=existing_artists(cfg), **{f"music_{k}": v for k, v in suggest_music(item).items()})
        elif kind == "video":
            folders = video_folders(cfg)
            folder = next((f for f in folders if f.casefold() == item.channel.casefold()), item.channel)
            # Short prefix like "videos/YouTube/"; the preview shows the full path.
            prefix = videos_base(cfg).relative_to(cfg.videos_dir.parent).as_posix()
            ctx.update(folders=folders, folder=folder, videos_prefix=prefix)
        return ctx

    def from_form(item: QueueItem) -> Classification:
        form = request.form
        return Classification(
            type=form.get("type", ""),
            title=(form.get("title") or item.title).strip(),
            year=_int(form.get("year")),
            show=(form.get("show") or "").strip() or None,
            season=_int(form.get("season")) if form.get("season") else 1,
            episode=_int(form.get("episode")),
            show_plot=(form.get("show_plot") or "").strip() or None,
            show_year=_int(form.get("show_year")),
            name=(form.get("name") or "").strip() or None,
            folder=(form.get("folder") or "").strip() or None,
            artist=(form.get("artist") or "").strip() or None,
            album=(form.get("album") or "").strip() or None,
            track=_int(form.get("track")),
        )

    def run_plan(pairs_factory, apply: bool, done_target: str | None):
        """Shared preview and file handler; htmx swaps the returned fragment."""
        try:
            pairs = pairs_factory()
            with _write_lock:
                plan = build_plan(cfg, db(), pairs)
                if apply and plan.ok and not cfg.dry_run:
                    apply_plan(cfg, db(), plan)
                    if cfg.discord_added_webhook_url:
                        # Off the request thread: Discord being slow must not hold up the page.
                        threading.Thread(target=notify_added, args=(cfg, plan), daemon=True).start()
                    html = render_template("_filed_notice.html", plan=plan)
                    response = make_response(html)
                    if done_target:
                        response.headers["HX-Retarget"] = done_target
                        response.headers["HX-Reswap"] = "outerHTML"
                    return response
        except FilingError as exc:
            return render_template("_plan.html", plan=None, errors=[str(exc)], applied=False,
                                   requested=apply, dry_run=cfg.dry_run)
        return render_template("_plan.html", plan=plan, errors=plan.errors, applied=False,
                               requested=apply, dry_run=cfg.dry_run)

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
        items = sorted(pending(), key=sort_key)
        record_seen(db(), [item.video_id for item in items])
        aged = aged_ids(db(), [item.video_id for item in items], cfg.queue_ping_days,
                        _utcnow())
        groups: dict[str, list[QueueItem]] = {}
        for item in items:
            groups.setdefault(item.channel, []).append(item)
        return render_template(
            "queue.html", groups=sorted(groups.items(), key=lambda kv: kv[0].casefold()),
            types=[(kind, TYPE_LABELS[kind]) for kind in cfg.enabled_types()], default_type=cfg.default_type(),
            shows_on=cfg.shows_dir is not None,
            total=len(items), aged=len(aged), group_key=group_key, field_context=field_context,
            dry_run=cfg.dry_run, ping_days=cfg.queue_ping_days)

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
        season = _season_arg(request.args.get("season"))
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
        return run_plan(lambda: [(item, from_form(item))], False, None)

    @app.post("/item/<video_id>/file")
    def file_item(video_id: str):
        item = pending_item(video_id)
        return run_plan(lambda: [(item, from_form(item))], True, f"#card-{video_id}")

    def require_shows() -> None:
        if cfg.shows_dir is None:
            abort(404)

    def group_items(key: str) -> tuple[str, list[QueueItem]]:
        require_shows()
        items = sorted((i for i in pending() if group_key(i.channel) == key), key=sort_key)
        if not items:
            abort(404)
        return items[0].channel, items

    @app.get("/group/<key>")
    def group(key: str):
        channel, items = group_items(key)
        shows = existing_shows(cfg, db())
        show = _match_show(channel, shows)
        counters = {1: next_episode(cfg, db(), show, 1), 0: next_episode(cfg, db(), show, 0)}
        rows = []
        for item in items:
            special = bool(SPECIAL_TITLE.search(item.title))  # a guess; the user reviews the ticks
            season = 0 if special else 1
            rows.append((item, counters[season], item.title, True, special))
            counters[season] += 1
        return render_template("group.html", key=key, channel=channel, show=show, rows=rows,
                               shows=shows, dry_run=cfg.dry_run, oob=False,
                               new_show=not show_exists(cfg, db(), show), show_year=_earliest_year(items))

    @app.get("/group/<key>/rows")
    def group_rows(key: str):
        """Renumber the table after the show or season changes, keeping edited titles and ticks."""
        channel, items = group_items(key)
        args = request.args
        show = (args.get("show") or "").strip() or channel
        season = _season_arg(args.get("season"))
        numbers = {season: next_episode(cfg, db(), show, season), 0: next_episode(cfg, db(), show, 0)}
        rows = []
        chosen = [item for item in items if args.get(f"include-{item.video_id}") is not None]
        year = _year_arg(args.get("show_year")) or _earliest_year(chosen or items)
        for item in items:
            included = item in chosen
            special = args.get(f"special-{item.video_id}") is not None
            row_season = 0 if special else season
            rows.append((item, numbers[row_season] if included else "",
                         args.get(f"title-{item.video_id}") or item.title, included, special))
            if included:
                numbers[row_season] += 1
        # The year field sits above the table, so it is swapped out-of-band. Keep the tbody first:
        # htmx parses inside a <template>, and a leading span makes the parser drop the tbody.
        return (render_template("_group_rows.html", rows=rows, key=key)
                + render_template("_show_year.html", oob=True, show_year=year,
                                  new_show=not show_exists(cfg, db(), show)))

    def group_pairs(key: str) -> list[tuple[QueueItem, Classification]]:
        _channel, items = group_items(key)
        form = request.form
        show = (form.get("show") or "").strip() or None
        season = _int(form.get("season")) if form.get("season") else 1
        show_year = _int(form.get("show_year"))
        pairs = []
        for item in items:
            if form.get(f"include-{item.video_id}") is None:
                continue
            special = form.get(f"special-{item.video_id}") is not None
            pairs.append((item, Classification(
                type="show", show=show, season=0 if special else season,
                title=(form.get(f"title-{item.video_id}") or item.title).strip(),
                episode=_int(form.get(f"episode-{item.video_id}")),
                show_plot=(form.get("show_plot") or "").strip() or None,
                show_year=show_year)))
        if not pairs:
            raise FilingError("Select at least one video.")
        return pairs

    @app.post("/group/<key>/preview")
    def group_preview(key: str):
        return run_plan(lambda: group_pairs(key), False, None)

    @app.post("/group/<key>/file")
    def group_file(key: str):
        return run_plan(lambda: group_pairs(key), True, "#group-form")

    @app.get("/filed")
    def filed():
        rows = db().execute(
            "SELECT f.*, s.name AS show_name FROM filings f LEFT JOIN shows s ON s.id = f.show_id"
            " ORDER BY f.filed_at DESC, f.video_id").fetchall()
        return render_template("filed.html", rows=rows, dry_run=cfg.dry_run, error=None, kept=None)

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
            return redirect(url_for("queue") + f"#card-{video_id}", code=303)
        rows = db().execute(
            "SELECT f.*, s.name AS show_name FROM filings f LEFT JOIN shows s ON s.id = f.show_id"
            " ORDER BY f.filed_at DESC, f.video_id").fetchall()
        return render_template("filed.html", rows=rows, dry_run=cfg.dry_run, error=error, kept=kept)

    return app


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
