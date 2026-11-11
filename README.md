# YoutarrRoutarr

LAN-only triage for Youtarr downloads, routing each one to a library. Each download is classified as a Show
episode, a Movie, a named Video or Audio, then hardlinked into the matching Jellyfin library with an
NFO and thumbnail. Audio is the exception: it is extracted into the Music library. The app never moves, changes or deletes Youtarr's copy.

## Layouts

All three libraries are on `/media`, the same filesystem as the queue.

| Type | Path |
|---|---|
| Show | `youtube-shows/Show (Year)/Season 01/Show - S01E01 - Title.ext`, plus `tvshow.nfo` and `poster.jpg` |
| Special | `youtube-shows/Show (Year)/Season 00/Show - S00E01 - Title.ext`; Jellyfin lists Season 00 as Specials. Tick **Special** per row on the bulk page (titles containing "special" or "bonus" are pre-ticked). Specials never set the show's year, poster or plot. |
| Movie | `youtube-movies/Title (Year)/Title (Year).ext`, plus an NFO and `poster.jpg` |
| Video | `videos/YouTube/<Folder>/Name/Name.ext`, plus an NFO and `poster.jpg`. The folder is a category or channel, picked from existing ones or typed new (nested with `/`, created like `mkdir -p`). |
| Audio | `youtube-music/Artist/Album/[NN - ]Title.ext`. Only the audio stream is copied out of the video (no re-encoding, so `.m4a`, `.opus`, `.ogg`, `.mp3` or `.flac` follows the source codec) and tagged with title, artist, album artist, album, track and year. Artist, album, title and year are prefilled from YouTube's music fields (`artist`, `track`, `album`, `release_year`) when the video has them, otherwise from an `Artist - Title` video title and the channel name (minus " - Topic"). Album falls back to the playlist, then `Singles`. Track is optional. Existing artist and album folders are reused ignoring case. |

An existing show folder is reused, and its `tvshow.nfo` is left alone. Subtitles are linked too.

## Settings

Create `youtarr-routarr.env` beside the Compose file with mode 640. Do not quote values.

| Variable | Default | Purpose |
|---|---|---|
| `BASIC_AUTH_USER`, `BASIC_AUTH_PASSWORD` | None | Required unless `AUTH_DISABLED=1` |
| `MUSIC_DIR` | `/media/youtube-music` | Where Audio filings go (set in Compose, not the env file) |
| `DRY_RUN` | `1` | Set to `0` to allow writes |
| `DISCORD_WEBHOOK_URL` | Empty | Queue-age ping only. Empty turns it off |
| `DISCORD_ADDED_WEBHOOK_URL` | Empty | Alert sent when media is filed. Empty turns it off. Use a different webhook from the queue ping, as it never falls back to that one |
| `QUEUE_PING_DAYS` | `7` | Item age that triggers a ping, sent at most once every 24 hours |

## Discord alerts

When `DISCORD_ADDED_WEBHOOK_URL` is set, each successful filing posts an embed laid out like Seerr's:
a linked title (`Movie Added: Title (Year)`, `Episode Added: Show S01E02 - Title`, `Video Added: Name`
or `Audio Added: Artist - Title`), the YouTube description, Channel and Library fields, the
thumbnail as the image, and a timestamp. A batch that files several episodes of one show sends a
single `Episodes Added: Show (N)` embed listing them. Alerts go out after the files are written, off
the request thread, so a Discord outage never fails or delays a filing; failures are only logged.

## Tests

```bash
uv venv /tmp/youtarr-routarr-venv
uv pip install --python /tmp/youtarr-routarr-venv/bin/python -r build/requirements.txt pytest
/tmp/youtarr-routarr-venv/bin/python -m pytest tests
```

## Deployment

1. Create the queue, library and `data/` folders, writable by the user the container runs as.
   The queue and libraries must be on one filesystem.
2. Create the env file with `DRY_RUN=1`, and set the paths in `docker-compose.yml`.
3. Point Youtarr's output directory at the queue folder. Keep that folder out of your Jellyfin libraries.
4. Add the library folders to Jellyfin.
5. Run `docker compose up -d --build`.
6. Check the previews, set `DRY_RUN=0`, and recreate the container.

## Design notes

- **One media mount:** `link()` returns `EXDEV` across separate bind mounts even on one
  filesystem, so all of `/media` is mounted read-write. Safety in that tree therefore
  depends on the app's code, not on the mount.
- **Queue age:** this is tracked in the database because Youtarr sets file mtimes to the upload date.
- **Playlists:** none of the 55 `info.json` files inspected had playlist data. The bulk action
  groups by channel and sorts by upload date, and uses `playlist_index` if one ever appears.
- **Undo:** it removes only files whose inode or hash still matches. It refuses to run if the
  source is gone, because the library copy would then be the last one.
