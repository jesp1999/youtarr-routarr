"""Environment-driven settings. Dry-run is on unless explicitly disabled."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


LIBRARY_LABELS = {"show": "Shows", "movie": "Movies", "video": "Videos", "music": "Audio"}
DEFAULT_ORDER = ("video", "movie", "show", "music")
DEFAULT_QUEUE_DIR = "/media/youtarr-queue"


def _library(value: str | None) -> Path | None:
    """A blank or unset library directory turns that library off."""
    value = (value or "").strip()
    return Path(value) if value else None


@dataclass(frozen=True)
class Config:
    queue_dir: Path
    shows_dir: Path | None
    movies_dir: Path | None
    videos_dir: Path | None
    info_dir: Path | None
    db_path: Path
    dry_run: bool = True
    auth_user: str = ""
    auth_password: str = ""
    auth_disabled: bool = False
    discord_webhook_url: str = ""
    queue_ping_days: int = 7
    discord_added_webhook_url: str = ""
    videos_subdir: str = "YouTube"
    music_dir: Path | None = None

    def library_dir(self, kind: str) -> Path | None:
        return {"show": self.shows_dir, "movie": self.movies_dir, "video": self.videos_dir,
                "music": self.music_dir}.get(kind)

    def enabled_types(self) -> tuple[str, ...]:
        return tuple(kind for kind in LIBRARY_LABELS if self.library_dir(kind) is not None)

    def default_type(self) -> str:
        return next(kind for kind in DEFAULT_ORDER if self.library_dir(kind) is not None)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ) if env is None else env
        info = env.get("INFO_DIR", "/youtarr-info")
        return cls(
            queue_dir=Path(env.get("QUEUE_DIR", "").strip() or DEFAULT_QUEUE_DIR),
            shows_dir=_library(env.get("SHOWS_DIR")),
            movies_dir=_library(env.get("MOVIES_DIR")),
            videos_dir=_library(env.get("VIDEOS_DIR")),
            info_dir=Path(info) if info else None,
            db_path=Path(env.get("DB_PATH", "/data/youtarr-routarr.sqlite3")),
            dry_run=_flag(env.get("DRY_RUN"), True),
            auth_user=env.get("BASIC_AUTH_USER", ""),
            auth_password=env.get("BASIC_AUTH_PASSWORD", ""),
            auth_disabled=_flag(env.get("AUTH_DISABLED"), False),
            discord_webhook_url=env.get("DISCORD_WEBHOOK_URL", ""),
            queue_ping_days=int(env.get("QUEUE_PING_DAYS", "7")),
            discord_added_webhook_url=env.get("DISCORD_ADDED_WEBHOOK_URL", ""),
            videos_subdir=env.get("VIDEOS_SUBDIR", "YouTube"),
            music_dir=_library(env.get("MUSIC_DIR")),
        )
