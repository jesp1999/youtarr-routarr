"""Environment-driven settings. Dry-run is on unless explicitly disabled."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    queue_dir: Path
    shows_dir: Path
    movies_dir: Path
    videos_dir: Path
    info_dir: Path | None
    db_path: Path
    dry_run: bool = True
    auth_user: str = ""
    auth_password: str = ""
    auth_disabled: bool = False
    discord_webhook_url: str = ""
    queue_ping_days: int = 7
    videos_subdir: str = "YouTube"
    music_dir: Path = Path("/media/music")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ) if env is None else env
        info = env.get("INFO_DIR", "/youtarr-info")
        return cls(
            queue_dir=Path(env.get("QUEUE_DIR", "/media/youtarr-queue")),
            shows_dir=Path(env.get("SHOWS_DIR", "/media/youtube-shows")),
            movies_dir=Path(env.get("MOVIES_DIR", "/media/youtube-movies")),
            videos_dir=Path(env.get("VIDEOS_DIR", "/media/videos")),
            info_dir=Path(info) if info else None,
            db_path=Path(env.get("DB_PATH", "/data/youtarr-routarr.sqlite3")),
            dry_run=_flag(env.get("DRY_RUN"), True),
            auth_user=env.get("BASIC_AUTH_USER", ""),
            auth_password=env.get("BASIC_AUTH_PASSWORD", ""),
            auth_disabled=_flag(env.get("AUTH_DISABLED"), False),
            discord_webhook_url=env.get("DISCORD_WEBHOOK_URL", ""),
            queue_ping_days=int(env.get("QUEUE_PING_DAYS", "7")),
            videos_subdir=env.get("VIDEOS_SUBDIR", "YouTube"),
            music_dir=Path(env.get("MUSIC_DIR", "/media/music")),
        )
