"""Read-only scan of youtarr's download directory."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
AUDIO_EXTS = {".mp3"}  # what Youtarr writes for audio-only downloads
# A same-named file with one of these means the .mp3 is a video's companion, or is still being converted.
MP3_SOURCE_EXTS = VIDEO_EXTS | {".m4a", ".opus"}
THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SUB_EXTS = {".srt", ".vtt", ".ass"}
ID_IN_BRACKETS = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
ID_AT_FOLDER_END = re.compile(r" - ([A-Za-z0-9_-]{11})$")
YEAR_IN_NAME = re.compile(r"\((\d{4})\)")
INCOMPLETE = re.compile(r"\.(part|ytdl|temp)(\.|$)|\.f\d+\.[a-z0-9]+$", re.I)


@dataclass
class QueueItem:
    video_id: str
    has_real_id: bool
    path: Path
    channel: str
    title: str
    upload_date: str | None = None  # YYYY-MM-DD
    year: int | None = None
    plot: str = ""
    thumb: Path | None = None
    fanart: Path | None = None
    subtitles: list[Path] = field(default_factory=list)
    playlist: str | None = None
    playlist_index: int | None = None
    size: int = 0
    duration: int | None = None  # seconds
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # YouTube Music style metadata, present on official audio uploads and "Topic" channels.
    music_artist: str = ""
    music_track: str = ""
    music_album: str = ""
    release_year: int | None = None

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def audio_only(self) -> bool:
        return self.ext in AUDIO_EXTS


def _date(raw: object) -> str | None:
    text = str(raw or "").strip()
    match = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", text)
    return f"{match[1]}-{match[2]}-{match[3]}" if match else None


# Info files reach many megabytes, mostly lists of formats and captions, so only these fields are
# kept, cached by modification time.
INFO_KEYS = frozenset((
    "title", "description", "upload_date", "channel", "uploader", "playlist", "playlist_title",
    "playlist_index", "duration", "categories", "tags", "artist", "track", "album", "release_year", "creator"))
_info_cache: dict[str, tuple[int, dict]] = {}
# One file is parsed at a time: several page loads parsing at once can exhaust the container's memory.
_info_lock = threading.Lock()


def _wanted(pairs: list[tuple[str, object]]) -> dict:
    """Drop unused fields while parsing, so nested lists never build up in memory."""
    return {key: value for key, value in pairs if key in INFO_KEYS}


def _read_info(info_dir: Path | None, video_id: str) -> dict:
    if not info_dir:
        return {}
    path = info_dir / f"{video_id}.info.json"
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    with _info_lock:
        cached = _info_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            info = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_wanted)
        except (OSError, ValueError):
            return {}
        if not isinstance(info, dict):
            return {}
        _info_cache[str(path)] = (mtime, info)
    return info


def _read_nfo(path: Path) -> dict:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return {}
    return {
        "title": root.findtext("title"),
        "description": root.findtext("plot"),
        "upload_date": root.findtext("premiered") or root.findtext("aired"),
        "channel": root.findtext("studio"),
    }


def _clean_stem(stem: str) -> str:
    text = ID_IN_BRACKETS.sub("", stem)
    text = YEAR_IN_NAME.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -") or stem


def _extract_id(path: Path, rel: str) -> tuple[str, bool]:
    matches = ID_IN_BRACKETS.findall(path.stem)
    if matches:
        return matches[-1], True
    folder = ID_AT_FOLDER_END.search(path.parent.name)
    if folder:
        return folder[1], True
    return "file-" + hashlib.sha1(rel.encode("utf-8")).hexdigest()[:16], False


def _sidecars(video: Path, names: list[str], video_id: str) -> tuple[Path | None, Path | None, list[Path]]:
    stem = video.stem
    thumb = fanart = None
    for ext in THUMB_EXTS:
        if stem + ext in names:
            thumb = video.with_name(stem + ext)
            break
    for ext in THUMB_EXTS:
        if f"{stem}-fanart{ext}" in names:
            fanart = video.with_name(f"{stem}-fanart{ext}")
            break
    # Only from this video's own folder: in the flat layout, poster.jpg is the channel's artwork.
    own_folder = ID_AT_FOLDER_END.search(video.parent.name)
    if thumb is None and "poster.jpg" in names and own_folder and own_folder[1] == video_id:
        thumb = video.with_name("poster.jpg")
    subs = [video.with_name(name) for name in sorted(names)
            if name.startswith(stem + ".") and Path(name).suffix.lower() in SUB_EXTS]
    return thumb, fanart, subs


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _year(value: object) -> int | None:
    return value if isinstance(value, int) and 1800 <= value <= 2999 else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]


def _has_source(audio: Path, names: list[str]) -> bool:
    return any(Path(name).stem == audio.stem and Path(name).suffix.lower() in MP3_SOURCE_EXTS for name in names)


def _is_audio_source(media: Path, names: list[str]) -> bool:
    """True for a .webm holding only audio that is being converted to the .mp3 beside it.

    Anything ffprobe cannot answer counts as a video, so a real download is never hidden.
    """
    if media.suffix.lower() != ".webm" or media.stem + ".mp3" not in names:
        return False
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(media)], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and not done.stdout.strip()


def _downloading(names: list[str]) -> tuple[set[str], bool]:
    """IDs still being written in a folder, and whether some partial file cannot be matched to one."""
    ids: set[str] = set()
    unknown = False
    for name in names:
        if name.endswith((".part", ".ytdl")):
            found = ID_IN_BRACKETS.findall(name)
            if found:
                ids.add(found[-1])
            else:
                unknown = True
    return ids, unknown


def scan_queue(queue_dir: Path, info_dir: Path | None = None) -> list[QueueItem]:
    """Every finished video, plus each audio-only download that has no video."""
    videos: list[QueueItem] = []
    audio: list[QueueItem] = []
    for dirpath, dirnames, filenames in os.walk(queue_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        directory = Path(dirpath)
        busy, unknown = _downloading(filenames)
        if unknown:
            continue  # something is still being written here, and it could be any of these files
        for name in sorted(filenames):
            media = directory / name
            ext = media.suffix.lower()
            if (ext not in VIDEO_EXTS | AUDIO_EXTS or name.startswith(".")
                    or INCOMPLETE.search(name) or media.is_symlink()):
                continue
            if ext in AUDIO_EXTS and _has_source(media, filenames):
                continue
            if _is_audio_source(media, filenames):
                continue
            item = _build_item(queue_dir, media, filenames, info_dir)
            # The flat layout shares one folder between downloads, so skip only the unfinished ones.
            if busy and (item.video_id in busy or not item.has_real_id):
                continue
            found = audio if ext in AUDIO_EXTS else videos
            found.append(item)
    # The same download may have its video in one folder and its audio in another.
    video_ids = {item.video_id for item in videos}
    return videos + [item for item in audio if item.video_id not in video_ids]


def _build_item(queue_dir: Path, video: Path, names: list[str], info_dir: Path | None) -> QueueItem:
    rel = video.relative_to(queue_dir).as_posix()
    video_id, real_id = _extract_id(video, rel)
    info = _read_info(info_dir, video_id) if real_id else {}
    nfo_path = video.with_suffix(".nfo")
    nfo = _read_nfo(nfo_path) if nfo_path.name in names else {}
    parts = Path(rel).parts

    def pick(key: str):
        return info.get(key) or nfo.get(key)

    date = _date(pick("upload_date"))
    name_year = YEAR_IN_NAME.search(video.stem)
    year = int(date[:4]) if date else (int(name_year[1]) if name_year else None)
    index = info.get("playlist_index")
    thumb, fanart, subs = _sidecars(video, names, video_id)
    duration = info.get("duration")
    return QueueItem(
        video_id=video_id,
        has_real_id=real_id,
        path=video,
        channel=(pick("channel") or info.get("uploader")
                 or (parts[0] if len(parts) > 1 else "Unsorted")),
        title=(pick("title") or _clean_stem(video.stem)).strip(),
        upload_date=date,
        year=year,
        plot=(pick("description") or "").strip(),
        thumb=thumb,
        fanart=fanart,
        subtitles=subs,
        playlist=info.get("playlist_title") or info.get("playlist"),
        playlist_index=index if isinstance(index, int) else None,
        size=video.stat().st_size,
        duration=round(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        categories=_strings(info.get("categories")),
        tags=_strings(info.get("tags")),
        music_artist=_text(info.get("artist") or info.get("creator")),
        music_track=_text(info.get("track")),
        music_album=_text(info.get("album")),
        release_year=_year(info.get("release_year")),
    )


def sort_key(item: QueueItem) -> tuple:
    """Playlist order when youtarr recorded one, then upload date, then title."""
    return (item.playlist_index is None, item.playlist_index or 0,
            item.upload_date or "9999", item.title.casefold())
