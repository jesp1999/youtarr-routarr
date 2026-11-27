"""Plan, apply and undo filings.

Rules enforced here:
- Video and sidecar files are hardlinked. Nothing in the queue is moved, changed or deleted.
- Existing destination files are never overwritten.
- Source and target must be on one filesystem and reachable through one mount.
- A batch applies fully or not at all.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .config import LIBRARY_LABELS, Config
from .db import log_event, now_iso
from .media import VIDEO_EXTS, QueueItem

UNSAFE = str.maketrans({":": " -", "/": "-", "\\": "-", "*": "", "?": "", '"': "'",
                        "<": "", ">": "", "|": "-"})
EPISODE_RE = re.compile(r"S(\d{1,4})E(\d{1,4})", re.I)
YEAR_SUFFIX = re.compile(r"\s*\(\d{4}\).*$")
MAX_NAME_BYTES = 150
XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class FilingError(Exception):
    pass


def safe_name(raw: str) -> str:
    """Turn a title into one safe path component, or raise."""
    text = unicodedata.normalize("NFKC", raw or "").translate(UNSAFE)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\s+", " ", text).strip(" .")
    encoded = text.encode("utf-8")[:MAX_NAME_BYTES]
    text = encoded.decode("utf-8", "ignore").strip(" .")
    if not text:
        raise FilingError(f"Name is empty after cleaning: {raw!r}")
    return text


QUOTED = re.compile(r'[“"]([^”"]+)[”"]')
# Where a YouTube title's marketing tail starts: " - ", "|", "(" or "[". A bare "-" stays,
# so "X-Men" and "Award-Winning" survive.
TITLE_TAIL = re.compile(r"\s[-–—]\s|[|(\[]")


def suggest_movie_title(raw: str) -> str:
    """'Horror Short Film "Odd Girl" | ALTER' -> 'Odd Girl'; 'THE COOK | Award-Winning...' -> 'THE COOK'.

    Falls back to the full title when cleaning
    would leave nothing useful.
    """
    text = unicodedata.normalize("NFKC", raw or "")  # full-width ｜ and ： become | and :
    quoted = QUOTED.search(text)
    if quoted:
        base = quoted.group(1)
    else:
        cut = TITLE_TAIL.search(text)
        base = text[:cut.start()] if cut else text
    base = re.sub(r"\s+", " ", base).strip(" -:,")
    return base if len(base) >= 2 else (raw or "").strip()


def _xml(root_tag: str, fields: list[tuple[str, object]], video_id: str | None) -> bytes:
    root = ET.Element(root_tag)
    for tag, value in fields:
        if value not in (None, ""):
            ET.SubElement(root, tag).text = XML_ILLEGAL.sub("", str(value))
    if video_id:
        unique = ET.SubElement(root, "uniqueid", type="youtube", default="true")
        unique.text = video_id
    # Stop Jellyfin filling gaps from TMDB/TVDB: short films share titles with real releases.
    ET.SubElement(root, "lockdata").text = "true"
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


@dataclass
class Classification:
    type: str  # show | movie | video | music
    title: str
    year: int | None = None
    show: str | None = None
    season: int = 1
    episode: int | None = None
    show_plot: str | None = None
    show_year: int | None = None  # new shows only; defaults to the earliest upload year
    name: str | None = None  # video only
    folder: str | None = None  # video only: subfolder path under the Videos subdir, "/"-separated
    artist: str | None = None  # music only
    album: str | None = None  # music only
    track: int | None = None  # music only: optional track number


@dataclass
class Op:
    kind: str  # link | write | extract
    dst: Path
    role: str
    src: Path | None = None
    data: bytes | None = None
    tags: dict[str, object] | None = None  # extract only: audio tags to write


@dataclass
class ShowContext:
    name: str
    folder: Path
    premiered: str | None
    exists: bool
    show_id: int | None
    year: int | None = None
    used: dict[int, set[int]] = field(default_factory=dict)
    ops: list[Op] = field(default_factory=list)
    manifest: dict = field(default_factory=lambda: {"files": [], "dirs": []})


@dataclass
class Entry:
    item: QueueItem
    cls: Classification
    root: Path
    dest: Path | None = None
    ops: list[Op] = field(default_factory=list)
    show: ShowContext | None = None
    season: int | None = None
    episode: int | None = None
    manifest: dict = field(default_factory=lambda: {"files": [], "dirs": []})

    @property
    def display(self) -> str:
        return self.cls.name or self.cls.title


@dataclass
class Plan:
    entries: list[Entry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    shows: dict[str, ShowContext] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def all_ops(self) -> list[Op]:
        ops: list[Op] = [op for show in self.shows.values() for op in show.ops]
        return ops + [op for entry in self.entries for op in entry.ops]


def _season_dir(folder: Path, season: int) -> Path:
    """Reuse an existing 'Season 1' folder rather than adding a 'Season 01' twin."""
    for candidate in (f"Season {season:02d}", f"Season {season}"):
        if (folder / candidate).is_dir():
            return folder / candidate
    return folder / f"Season {season:02d}"


def _episodes_on_disk(folder: Path, season: int) -> set[int]:
    directory = _season_dir(folder, season)
    if not directory.is_dir():
        return set()
    found = set()
    for child in directory.iterdir():
        match = EPISODE_RE.search(child.name)
        if match and child.suffix.lower() in VIDEO_EXTS and int(match[1]) == season:
            found.add(int(match[2]))
    return found


def _used_episodes(conn: sqlite3.Connection, ctx: ShowContext, season: int) -> set[int]:
    if season not in ctx.used:
        used = _episodes_on_disk(ctx.folder, season)
        if ctx.show_id is not None:
            rows = conn.execute("SELECT episode FROM filings WHERE show_id = ? AND season = ?",
                                (ctx.show_id, season))
            used |= {row["episode"] for row in rows}
        ctx.used[season] = used
    return ctx.used[season]


def next_episode(cfg: Config, conn: sqlite3.Connection, show: str, season: int = 1) -> int:
    """Default episode number for the per-video form."""
    try:
        ctx = _resolve_show(cfg, conn, show, None)
    except FilingError:
        return 1
    return max(_used_episodes(conn, ctx, season), default=0) + 1


def existing_shows(cfg: Config, conn: sqlite3.Connection) -> list[str]:
    # Folder names (with the year), not bare show names: listing both made every show appear twice.
    if cfg.shows_dir is None:
        return []
    names = {row["folder"] for row in conn.execute("SELECT folder FROM shows")}
    try:
        names |= {child.name for child in cfg.shows_dir.iterdir() if child.is_dir()}
    except OSError:
        pass
    return sorted(names, key=str.casefold)


def videos_base(cfg: Config) -> Path:
    """Where Video filings start, e.g. videos/YouTube."""
    return cfg.videos_dir.joinpath(*_subpath_parts(cfg.videos_subdir))


def _subpath_parts(raw: str | None) -> list[str]:
    """Split 'Comedy/Sketches' into safe components. Empty input means no subfolder."""
    return [safe_name(part) for part in (raw or "").replace("\\", "/").split("/") if part.strip()]


def _has_video(directory: Path) -> bool:
    try:
        return any(child.suffix.lower() in VIDEO_EXTS and child.is_file() for child in directory.iterdir())
    except OSError:
        return False


def video_folders(cfg: Config, max_depth: int = 3) -> list[str]:
    """Category or author folders under the Videos base, skipping per-video folders."""
    base = videos_base(cfg)
    found: list[str] = []

    def walk(directory: Path, prefix: str, depth: int) -> None:
        try:
            children = sorted((c for c in directory.iterdir() if c.is_dir()), key=lambda c: c.name.casefold())
        except OSError:
            return
        for child in children:
            if _has_video(child):
                continue
            path = f"{prefix}{child.name}"
            found.append(path)
            if depth < max_depth:
                walk(child, path + "/", depth + 1)

    walk(base, "", 1)
    return found


def _reuse_dir(parent: Path, name: str) -> Path:
    """An existing child folder matching case-insensitively, else the path a new one would have."""
    try:
        match = next((c for c in parent.iterdir() if c.is_dir() and c.name.casefold() == name.casefold()), None)
    except OSError:
        match = None
    return match or parent / name


def resolve_video_folder(cfg: Config, raw: str | None) -> Path:
    """Map a typed subfolder onto existing folders case-insensitively, so 'vsauce' reuses 'Vsauce'."""
    current = videos_base(cfg)
    for part in _subpath_parts(raw):
        current = _reuse_dir(current, part)
    return current


def existing_artists(cfg: Config) -> list[str]:
    if cfg.music_dir is None:
        return []
    try:
        names = [child.name for child in cfg.music_dir.iterdir() if child.is_dir()]
    except OSError:
        return []
    return sorted(names, key=str.casefold)


# "Artist - Title", with any dash. A bare hyphen stays, so "Jay-Z" is not split.
ARTIST_SPLIT = re.compile(r"\s[-–—]\s")
TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$", re.I)
# Bracketed video-only labels such as "(Official Video)" or "[Lyrics]". Other brackets stay: "(feat. X)".
TRACK_NOISE = re.compile(
    r"\s*[(\[]\s*(?:official|lyrics?|lyric video|audio|music video|video|hd|hq|4k|visuali[sz]er)\b[^)\]]*[)\]]",
    re.I)


def suggest_music(item: QueueItem) -> dict[str, object]:
    """Artist, track, album and year to prefill the form, from YouTube's music fields when it has them.

    Falls back to "Artist - Title" in the video title, then to the channel (minus " - Topic").
    Every value is only a suggestion the user reviews.
    """
    title = unicodedata.normalize("NFKC", item.title or "")
    artist, track = item.music_artist, item.music_track
    left = right = None
    parts = ARTIST_SPLIT.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts[0].strip(), parts[1].strip()
    if not artist:
        artist = left if left else TOPIC_SUFFIX.sub("", item.channel).strip()
    if not track:
        # Drop an "Artist - " prefix only when it names the artist; otherwise the dash is part of the title.
        track = right if left and left.casefold() == artist.casefold() else title
    track = TRACK_NOISE.sub("", track).strip() or track
    return {
        "artist": artist,
        "title": track,
        "album": item.music_album or item.playlist or "Singles",
        "year": item.release_year or item.year,
    }


AUDIO_EXT = {"aac": ".m4a", "alac": ".m4a", "opus": ".opus", "vorbis": ".ogg", "mp3": ".mp3", "flac": ".flac"}
FFMPEG_TIMEOUT = 600


def audio_ext(path: Path) -> str:
    """File extension for the video's first audio stream, which is copied rather than re-encoded."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FilingError(f"Cannot run ffprobe to check the audio: {exc}") from exc
    codec = done.stdout.strip().split(",")[0].lower()
    if not codec:
        raise FilingError(f"No audio stream found in {path.name}")
    if codec not in AUDIO_EXT:
        raise FilingError(f"Cannot save {codec} audio without re-encoding it: {path.name}")
    return AUDIO_EXT[codec]


def _clean_tag(value: object) -> str:
    return "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C").strip()


def _extract_audio(op: Op) -> str:
    """Copy the audio stream into a hidden temp file, then link it into place. Returns its sha256.

    The link fails if the destination exists, so nothing is ever overwritten, and the file only
    appears under its real name once it is complete.
    """
    tmp = op.dst.with_name(f".{op.dst.stem}.partial{op.dst.suffix}")
    command = ["ffmpeg", "-nostdin", "-n", "-loglevel", "error", "-i", str(op.src),
               "-map", "0:a:0", "-vn", "-c:a", "copy", "-map_metadata", "-1"]
    for key, value in (op.tags or {}).items():
        if value not in (None, ""):
            command += ["-metadata", f"{key}={_clean_tag(value)}"]
    command.append(str(tmp))
    try:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise FilingError(f"ffmpeg took longer than {FFMPEG_TIMEOUT} s on {op.src.name}") from exc
        if done.returncode != 0:
            detail = (done.stderr.strip().splitlines() or ["no output"])[-1]
            raise FilingError(f"ffmpeg could not extract the audio from {op.src.name}: {detail}")
        digest = hashlib.sha256()
        with open(tmp, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        os.link(tmp, op.dst, follow_symlinks=False)
        return digest.hexdigest()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def show_exists(cfg: Config, conn: sqlite3.Connection, raw: str) -> bool:
    """True when filing under this name would reuse a show, so its year is already fixed."""
    try:
        return _resolve_show(cfg, conn, raw, None).exists
    except FilingError:
        return True  # ambiguous name: the user has to pick an existing folder anyway


def _resolve_show(cfg: Config, conn: sqlite3.Connection, raw: str, first_date: str | None,
                  year: int | None = None) -> ShowContext:
    name = safe_name(raw)
    row = conn.execute("SELECT * FROM shows WHERE name = ? COLLATE NOCASE OR folder = ?",
                       (name, name)).fetchone()
    if row:
        return ShowContext(row["name"], cfg.shows_dir / row["folder"], row["premiered"], True, row["id"])
    if (cfg.shows_dir / name).is_dir():
        return ShowContext(YEAR_SUFFIX.sub("", name) or name, cfg.shows_dir / name, None, True, None)
    pattern = re.compile(re.escape(name) + r" \(\d{4}\)$", re.I)
    try:
        matches = [c for c in cfg.shows_dir.iterdir() if c.is_dir() and pattern.match(c.name)]
    except OSError:
        matches = []
    if len(matches) > 1:
        options = ", ".join(sorted(m.name for m in matches))
        raise FilingError(f"'{name}' matches several show folders; enter the exact one: {options}")
    if matches:
        return ShowContext(name, matches[0], None, True, None)
    if year is not None and not 1800 <= year <= 2999:
        raise FilingError(f"Show year must be between 1800 and 2999, not {year}")
    if year is None and first_date:
        year = int(first_date[:4])
    # Keep the full premiere date only when it agrees with the chosen year.
    premiered = first_date if first_date and year and first_date.startswith(str(year)) else None
    folder = f"{name} ({year})" if year else name
    return ShowContext(name, cfg.shows_dir / folder, premiered, False, None, year=year)


# Credits only when the uploader states them; a channel is often a distributor, not the director.
DIRECTOR_IN_TITLE = re.compile(r"\b(?:directed by|a (?:short )?film by)\s+([^|()\[\]\-–—,]+)", re.I)
# "Directed by X", "Director: X", "Director and writer - X", "Director / Writer / Editor – X".
# A role list after "Director" must end in a separator, so "Director of Photography: X" never matches.
_ROLES = r"(?:writer|editor|producer|screenwriter|creator|animator|cinematographer)"
DIRECTOR_LINE = re.compile(
    r"^\s*(?:directed\s+by\s*[:\-–—]?\s*|director(?:\s*(?:/|&|,|and)\s*" + _ROLES + r")*\s*[:\-–—]\s*)(.+?)\s*$",
    re.I | re.M)
NAME_END = re.compile(r"\s+[|•·/]\s+|\s{2,}|\s+[-–—]\s+")
MAX_TAGS = 20


def find_director(item: QueueItem) -> str | None:
    for pattern, text in ((DIRECTOR_IN_TITLE, item.title), (DIRECTOR_LINE, item.plot)):
        match = pattern.search(text or "")
        if match:
            name = NAME_END.split(match.group(1))[0]
            name = re.sub(r"\s+", " ", name).strip(" .:")
            if name.isupper():
                name = name.title()  # "CURRY BARKER" from an all-caps title
            if 2 <= len(name) <= 60 and not name.startswith("http"):
                return name
    return None


def youtube_fields(item: QueueItem, title: str | None) -> list[tuple[str, object]]:
    """NFO fields taken from Youtarr's info.json: the parts YouTube can vouch for."""
    fields: list[tuple[str, object]] = []
    if title and title != item.title:
        fields.append(("originaltitle", item.title))
    if item.duration:
        fields.append(("runtime", max(1, round(item.duration / 60))))
    director = find_director(item)
    if director:
        fields.append(("director", director))
    seen: set[str] = set()
    for tag in [*item.categories, *item.tags[:MAX_TAGS]]:
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            fields.append(("tag", tag))
    return fields


def _fanart_op(item: QueueItem, folder: Path) -> list[Op]:
    if not item.fanart:
        return []
    return [Op("link", folder / f"fanart{item.fanart.suffix.lower()}", "fanart", src=item.fanart)]


def _thumb_op(item: QueueItem, dst: Path) -> list[Op]:
    if not item.thumb:
        return []
    return [Op("link", dst.with_suffix(item.thumb.suffix.lower()), "thumbnail", src=item.thumb)]


def _subtitle_ops(item: QueueItem, dest: Path) -> list[Op]:
    ops = []
    for sub in item.subtitles:
        tail = sub.name[len(item.path.stem):]  # e.g. ".en.srt"
        ops.append(Op("link", dest.with_name(dest.stem + tail), "subtitle", src=sub))
    return ops


def _plan_show(cfg: Config, conn: sqlite3.Connection, plan: Plan, entry: Entry, first_date: str | None) -> None:
    item, cls = entry.item, entry.cls
    raw = cls.show or item.playlist or item.channel
    key = safe_name(raw).casefold()
    ctx = plan.shows.get(key)
    new_ctx = ctx is None
    if new_ctx:
        ctx = _resolve_show(cfg, conn, raw, first_date, cls.show_year)
        plan.shows[key] = ctx
    if cls.season < 0:
        raise FilingError("Season cannot be negative")
    used = _used_episodes(conn, ctx, cls.season)
    episode = cls.episode if cls.episode is not None else max(used, default=0) + 1
    if episode < 1:
        raise FilingError("Episode must be 1 or higher")
    if episode in used:
        raise FilingError(f"{ctx.name} S{cls.season:02d}E{episode:02d} is already taken")
    used.add(episode)

    title = safe_name(cls.title)
    stem = f"{safe_name(ctx.name)} - S{cls.season:02d}E{episode:02d} - {title}"
    dest = _season_dir(ctx.folder, cls.season) / f"{stem}{item.ext}"
    entry.show, entry.season, entry.episode, entry.dest = ctx, cls.season, episode, dest
    entry.ops = [
        Op("link", dest, "video", src=item.path),
        Op("write", dest.with_suffix(".nfo"), "nfo", data=_xml("episodedetails", [
            ("title", cls.title), ("showtitle", ctx.name), ("season", cls.season),
            ("episode", episode), ("plot", item.plot), ("aired", item.upload_date),
            ("premiered", item.upload_date), *youtube_fields(item, cls.title),
        ], item.video_id if item.has_real_id else None)),
        *_thumb_op(item, dest.with_name(f"{dest.stem}-thumb.jpg")),
        *_subtitle_ops(item, dest),
    ]
    if new_ctx and not ctx.exists:
        ctx.ops = [
            Op("write", ctx.folder / "tvshow.nfo", "show-nfo", data=_xml("tvshow", [
                ("title", ctx.name), ("plot", cls.show_plot or item.plot),
                ("premiered", ctx.premiered), ("year", ctx.year),
                ("studio", item.channel),
            ], None)),
            *_thumb_op(item, ctx.folder / "poster.jpg"),
            *_fanart_op(item, ctx.folder),
        ]


def _plan_single_folder(entry: Entry, base: str, parent: Path | None = None) -> None:
    """Movies and Videos: <parent>/<base>/<base>.ext with an NFO and poster beside it."""
    item, cls = entry.item, entry.cls
    folder = (parent or entry.root) / base
    if folder.exists():
        raise FilingError(f"Folder already exists: {folder}")
    dest = folder / f"{base}{item.ext}"
    entry.dest = dest
    entry.ops = [
        Op("link", dest, "video", src=item.path),
        Op("write", dest.with_suffix(".nfo"), "nfo", data=_xml("movie", [
            ("title", cls.name or cls.title), ("plot", item.plot),
            ("premiered", item.upload_date),
            ("year", cls.year or (item.upload_date or "")[:4]), ("studio", item.channel),
            *youtube_fields(item, cls.name or cls.title),
        ], item.video_id if item.has_real_id else None)),
        *_thumb_op(item, folder / "poster.jpg"),
        *_fanart_op(item, folder),
        *_subtitle_ops(item, dest),
    ]


def _plan_music(entry: Entry) -> None:
    """Music: <Music>/<Artist>/<Album>/[NN - ]<Title>.ext, audio only, tagged for Jellyfin."""
    item, cls = entry.item, entry.cls
    artist, album = (cls.artist or "").strip(), (cls.album or "").strip()
    if not artist:
        raise FilingError("Artist is required.")
    if not album:
        raise FilingError("Album is required.")
    if cls.track is not None and cls.track < 1:
        raise FilingError("Track number must be 1 or higher")
    ext = audio_ext(item.path)
    folder = _reuse_dir(_reuse_dir(entry.root, safe_name(artist)), safe_name(album))
    stem = safe_name(cls.title)
    if cls.track:
        stem = f"{cls.track:02d} - {stem}"
    entry.dest = folder / f"{stem}{ext}"
    entry.ops = [Op("extract", entry.dest, "audio", src=item.path, tags={
        "title": cls.title, "artist": artist, "album_artist": artist, "album": album,
        "track": cls.track, "date": cls.year or item.release_year or item.year,
    })]


def _check_paths(plan: Plan) -> None:
    seen: set[Path] = set()
    for entry in plan.entries:
        for op in entry.ops:
            if op.src is None or op.kind != "link":
                continue  # only hardlinks need a shared filesystem
            try:
                same = os.stat(op.src).st_dev == os.stat(entry.root).st_dev
            except OSError as exc:
                plan.errors.append(f"Cannot check {op.src}: {exc.strerror}")
                continue
            if not same:
                plan.errors.append(
                    f"Refusing to file: {op.src} and {entry.root} are on different filesystems.")
    for op in plan.all_ops():
        if op.dst in seen:
            plan.errors.append(f"Two items would write the same file: {op.dst}")
        seen.add(op.dst)
        if os.path.lexists(op.dst):
            plan.errors.append(f"Already exists, will not overwrite: {op.dst}")


def build_plan(cfg: Config, conn: sqlite3.Connection, pairs: list[tuple[QueueItem, Classification]]) -> Plan:
    plan = Plan()
    # Specials (Season 0) never set the show's premiere year, poster or plot: plan them after the
    # regular episodes, and take the earliest date from regular episodes when there are any.
    pairs = sorted(pairs, key=lambda pair: pair[1].type == "show" and pair[1].season == 0)
    regular = {safe_name(c.show or i.playlist or i.channel).casefold() for i, c in pairs
               if c.type == "show" and c.season != 0 and (c.show or i.playlist or i.channel)}
    earliest: dict[str, str] = {}
    for item, cls in pairs:
        if cls.type == "show":
            try:
                key = safe_name(cls.show or item.playlist or item.channel).casefold()
            except FilingError:
                continue
            if cls.season == 0 and key in regular:
                continue
            if item.upload_date and (key not in earliest or item.upload_date < earliest[key]):
                earliest[key] = item.upload_date

    for item, cls in pairs:
        if cls.type not in LIBRARY_LABELS:
            plan.errors.append(f"Unknown type: {cls.type!r}")
            continue
        root = cfg.library_dir(cls.type)
        if root is None:
            plan.errors.append(f"The {LIBRARY_LABELS[cls.type]} library is turned off.")
            continue
        if item.audio_only and cls.type != "music":
            plan.errors.append(f"'{item.title}' is audio only; file it as Audio.")
            continue
        if not root.is_dir():
            plan.errors.append(f"Library folder does not exist: {root}")
            continue
        entry = Entry(item, cls, root)
        try:
            if cls.type == "show":
                key = safe_name(cls.show or item.playlist or item.channel).casefold()
                _plan_show(cfg, conn, plan, entry, earliest.get(key))
            elif cls.type == "movie":
                year = cls.year or item.year
                if not year:
                    raise FilingError(f"'{cls.title}' has no upload date; enter a year.")
                cls.year = year
                _plan_single_folder(entry, f"{safe_name(cls.title)} ({year})")
            elif cls.type == "music":
                _plan_music(entry)
            else:
                _plan_single_folder(entry, safe_name(cls.name or cls.title),
                                    resolve_video_folder(cfg, cls.folder))
        except FilingError as exc:
            plan.errors.append(str(exc))
            continue
        plan.entries.append(entry)
    _check_paths(plan)
    return plan


def _mkdirs(directory: Path, created: list[str]) -> None:
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir()
        created.append(str(path))


def _run_op(op: Op, manifest: dict, created: list[Path], created_dirs: list[str]) -> None:
    before = len(created_dirs)
    _mkdirs(op.dst.parent, created_dirs)
    manifest["dirs"].extend(created_dirs[before:])
    if op.kind == "extract":
        digest = _extract_audio(op)
        created.append(op.dst)
        manifest["files"].append({"path": str(op.dst), "kind": "write", "role": op.role, "sha256": digest})
    elif op.kind == "link":
        os.link(op.src, op.dst, follow_symlinks=False)
        created.append(op.dst)
        manifest["files"].append({"path": str(op.dst), "kind": "link", "role": op.role,
                                  "ino": os.stat(op.dst).st_ino})
    else:
        with open(op.dst, "xb") as handle:
            created.append(op.dst)
            handle.write(op.data)
        manifest["files"].append({"path": str(op.dst), "kind": "write", "role": op.role,
                                  "sha256": hashlib.sha256(op.data).hexdigest()})


def _rollback(files: list[Path], dirs: list[str]) -> None:
    for path in reversed(files):
        try:
            path.unlink()
        except OSError:
            pass
    for directory in reversed(dirs):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def _friendly(exc: OSError, op: Op | None) -> str:
    where = f" ({op.dst})" if op else ""
    if exc.errno == errno.EXDEV:
        return ("Refusing to file: the source and target are on different filesystems or "
                f"separate mounts{where}. Mount their common parent once in the container.")
    if exc.errno == errno.EPERM and op and op.kind == "link":
        return (f"The kernel refused the hardlink{where}. With fs.protected_hardlinks=1 the app's "
                "user must own the source or have read and write access to it.")
    return f"Filing failed{where}: {exc.strerror or exc}"


def apply_plan(cfg: Config, conn: sqlite3.Connection, plan: Plan) -> None:
    """Create all files, then record them. Any failure removes everything this call created."""
    if not plan.ok:
        raise FilingError("; ".join(plan.errors))
    created: list[Path] = []
    created_dirs: list[str] = []
    current: Op | None = None
    try:
        old_umask = os.umask(0o002)
        try:
            for ctx in plan.shows.values():
                for current in ctx.ops:
                    _run_op(current, ctx.manifest, created, created_dirs)
            for entry in plan.entries:
                # Video last: Jellyfin's folder monitor can index it the moment it appears, and an NFO
                # written afterwards is not re-read, leaving the episode without season/episode numbers.
                for current in sorted(entry.ops, key=lambda op: op.role == "video"):
                    _run_op(current, entry.manifest, created, created_dirs)
        finally:
            os.umask(old_umask)
        with conn:
            for ctx in plan.shows.values():
                if ctx.show_id is None:
                    ctx.show_id = conn.execute(
                        "INSERT INTO shows (name, folder, premiered, created_by_app, manifest, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (ctx.name, ctx.folder.name, ctx.premiered, 0 if ctx.exists else 1,
                         json.dumps(ctx.manifest, ensure_ascii=False), now_iso())).lastrowid
            for entry in plan.entries:
                metadata = {
                    "title": entry.cls.title, "name": entry.cls.name, "year": entry.cls.year,
                    "plot": entry.item.plot, "upload_date": entry.item.upload_date,
                    "channel": entry.item.channel,
                }
                if entry.cls.type == "music":
                    metadata.update(artist=entry.cls.artist, album=entry.cls.album, track=entry.cls.track)
                conn.execute(
                    "INSERT INTO filings (video_id, type, source_path, dest_path, show_id, season,"
                    " episode, metadata, manifest, filed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry.item.video_id, entry.cls.type, str(entry.item.path), str(entry.dest),
                     entry.show.show_id if entry.show else None, entry.season, entry.episode,
                     json.dumps(metadata, ensure_ascii=False),
                     json.dumps(entry.manifest, ensure_ascii=False), now_iso()))
                log_event(conn, entry.item.video_id, "file",
                          {"type": entry.cls.type, "dest": str(entry.dest)})
    except OSError as exc:
        _rollback(created, created_dirs)
        raise FilingError(_friendly(exc, current)) from exc
    except Exception:
        _rollback(created, created_dirs)
        raise


def _remove_files(manifest: dict, kept: list[str]) -> None:
    for record in manifest.get("files", []):
        path = Path(record["path"])
        try:
            if record["kind"] == "link":
                intact = os.lstat(path).st_ino == record["ino"]
            else:
                intact = hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        except FileNotFoundError:
            continue
        except OSError as exc:
            kept.append(f"{path} ({exc.strerror})")
            continue
        if intact:
            path.unlink()
        else:
            kept.append(f"{path} (changed since filing)")


def _remove_dirs(directories: list[str]) -> None:
    for directory in sorted(directories, key=len, reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass  # not empty or already gone


def undo_blocker(row: sqlite3.Row) -> str | None:
    """Why a filing cannot be undone right now, or None. Undo must never delete a last copy."""
    # Extracted audio is a separate file, so a link count cannot tell whether the source survives.
    if (row["type"] == "music" and not os.path.exists(row["source_path"])
            and os.path.lexists(row["dest_path"])):
        return ("The source download is gone, so this may be the last copy of the audio. Undo would delete it, "
                "so nothing was changed.")
    for record in json.loads(row["manifest"])["files"]:
        if record["role"] != "video":
            continue
        try:
            stat = os.lstat(record["path"])
        except FileNotFoundError:
            continue
        if stat.st_ino == record["ino"] and stat.st_nlink < 2:
            return ("The source copy is gone, so this is the last copy of the video. Undo would delete it, "
                    "so nothing was changed.")
    return None


def undo_filing(cfg: Config, conn: sqlite3.Connection, video_id: str) -> list[str]:
    """Remove what a filing created. Returns any paths left in place. Never touches the source."""
    row = conn.execute("SELECT * FROM filings WHERE video_id = ?", (video_id,)).fetchone()
    if row is None:
        raise FilingError("That video is not filed.")
    blocked = undo_blocker(row)
    if blocked:
        raise FilingError(blocked)
    manifest = json.loads(row["manifest"])

    kept: list[str] = []
    _remove_files(manifest, kept)
    _remove_dirs(manifest.get("dirs", []))
    with conn:
        conn.execute("DELETE FROM filings WHERE video_id = ?", (video_id,))
        show_id = row["show_id"]
        if show_id is not None:
            remaining = conn.execute("SELECT COUNT(*) FROM filings WHERE show_id = ?",
                                     (show_id,)).fetchone()[0]
            if remaining == 0:
                show = conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
                if show["created_by_app"]:
                    show_manifest = json.loads(show["manifest"])
                    _remove_files(show_manifest, kept)
                    # Show/Season NN/file: taken from the filing, so a library since turned off still works.
                    folder = Path(row["dest_path"]).parent.parent
                    if folder.is_dir():
                        for child in folder.iterdir():
                            if child.is_dir() and child.name.startswith("Season "):
                                _remove_dirs([str(child)])
                    _remove_dirs([str(folder)])
                conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
        log_event(conn, video_id, "undo", {"dest": row["dest_path"], "kept": kept})
    return kept
