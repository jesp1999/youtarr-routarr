# YoutarrRoutarr

A companion to [Youtarr](https://github.com/DialmasterOrg/Youtarr) that sorts its downloads into
Jellyfin or Plex libraries. It is a separate project and is not part of Youtarr.

Youtarr downloads into a queue folder. In YoutarrRoutarr you classify each download as a **Show
episode**, a **Movie**, a named **Video** or **Audio**, preview the result, and file it. Filing
hardlinks the video into the matching library with an NFO and artwork, so it takes no extra space.
Audio is the exception: the audio stream is copied into a new file.

- Nothing in Youtarr's folder is ever moved, changed or deleted.
- Existing library files are never overwritten.
- Filings can be undone.
- It starts in dry-run mode: previews work, but nothing is written until you turn that off.

## Requirements

- Docker with Compose.
- Youtarr, plus Jellyfin or Plex.
- The queue and all libraries on **one filesystem**, under one parent folder. Hardlinks cannot
  cross filesystems or separate container mounts.

## Quick start

1. Download the two setup files:

   ```bash
   mkdir youtarr-routarr && cd youtarr-routarr
   curl -LO https://raw.githubusercontent.com/jesp1999/youtarr-routarr/main/docker-compose.yml
   curl -L -o .env https://raw.githubusercontent.com/jesp1999/youtarr-routarr/main/.env.example
   mkdir data
   chmod 600 .env
   ```

2. Edit `.env`. At minimum set `BASIC_AUTH_PASSWORD`, `MEDIA_ROOT`, `YOUTARR_INFO_DIR`, `PUID`
   and `PGID`.
3. Create the queue and library folders under `MEDIA_ROOT`. Make them, and `data/`, writable by
   `PUID:PGID` (see [Permissions](#permissions)).
4. [Point Youtarr at the queue](#pointing-youtarr-at-the-queue).
5. Start it with `docker compose up -d` and open `http://<your-server>:8000`.
6. Check a few previews. When they look right, set `DRY_RUN=0` and run `docker compose up -d` again.
7. Set up your libraries in [Jellyfin](#jellyfin) or [Plex](#plex).

To update: `docker compose pull && docker compose up -d`.

To build from source instead, clone the repository and run:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Naming files with `-f` stops Compose loading `docker-compose.override.yml` by itself. If you have
one, name it last:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml -f docker-compose.override.yml up -d --build
```

## Pointing Youtarr at the queue

Set Youtarr's `YOUTUBE_OUTPUT_DIR` to `MEDIA_ROOT/QUEUE_FOLDER` and recreate its container. Keep
that folder **out of your media server's libraries**: nothing should appear there until you file it.
If you already have downloads elsewhere, check how Youtarr treats them before moving its output.

`YOUTARR_INFO_DIR` is the `jobs/info` folder beside Youtarr's `docker-compose.yml`. It supplies
titles, dates, descriptions and music fields, and is mounted read-only.

## Settings

All settings live in `.env`. Put single quotes around any value containing `$`.

| Variable | Sample value | Meaning |
|---|---|---|
| `BASIC_AUTH_USER` | `admin` | Login name |
| `BASIC_AUTH_PASSWORD` | blank | Required. The app will not start without it |
| `AUTH_DISABLED` | `0` | `1` turns the login off |
| `MEDIA_ROOT` | `/path/to/media` | Host folder holding the queue and libraries, seen as `/media` |
| `YOUTARR_INFO_DIR` | `/path/to/youtarr/jobs/info` | Youtarr's metadata folder |
| `QUEUE_FOLDER` | `youtarr-queue` | Youtarr's download folder, under `MEDIA_ROOT` |
| `SHOWS_FOLDER` | `youtube-shows` | Blank turns shows off |
| `MOVIES_FOLDER` | `youtube-movies` | Blank turns movies off |
| `VIDEOS_FOLDER` | `videos` | Blank turns videos off |
| `VIDEOS_SUBDIR` | `YouTube` | Folder inside the videos library where filings start |
| `MUSIC_FOLDER` | `youtube-music` | Blank turns audio off |
| `PUID`, `PGID` | `1000` | User and group the container runs as |
| `TZ` | `Etc/UTC` | Time zone |
| `PORT` | `8000` | Port on the host |
| `DRY_RUN` | `1` | `0` allows writes |
| `DISCORD_WEBHOOK_URL` | blank | Ping when downloads wait too long |
| `DISCORD_ADDED_WEBHOOK_URL` | blank | Alert when media is filed |
| `QUEUE_PING_DAYS` | `7` | Age in days that triggers the ping, sent at most once a day |

At least one library folder is needed. No library may be the queue folder, contain it or sit inside
it, and the app will not start if one does. Use libraries that Sonarr, Radarr or Lidarr do not manage.

For anything else, such as a reverse-proxy network, add your own `docker-compose.override.yml`.

## Jellyfin

Tested with Jellyfin 12.1.0. Add one library for each folder you use:

| Folder | Content type |
|---|---|
| Shows | Shows |
| Movies | Movies |
| Videos | Home Videos and Photos |
| Music | Music |

## Plex

Tested with Plex Media Server 1.43.5.

- The server must be **signed in to a Plex account**. On an unclaimed server the NFO agents do not
  appear.
- Choose the agent when creating each library:

| Folder | Library type | Agent |
|---|---|---|
| Shows | TV Shows | Plex NFO Series |
| Movies | Movies | Plex NFO Movie |
| Videos | Movies | Plex NFO Movie |
| Music | Music | Plex Personal Media |

The regular Plex Music agent can replace an artist with a similarly named one it finds online.
In testing, YouTube tags did not appear as genres or labels. Playback and Plex's client apps were
not tested. Adding to an existing show folder that has no `tvshow.nfo` was not tested either.

Emby and Kodi read NFO files too, but have not been tested.

## What gets written

| Type | Path, inside the library folder |
|---|---|
| Show | `Show (Year)/Season 01/Show - S01E01 - Title.ext`, plus `tvshow.nfo` and `poster.jpg` |
| Special | `Show (Year)/Season 00/Show - S00E01 - Title.ext`, listed as Specials. Tick **Special** per row on the bulk page (titles containing "special" or "bonus" are pre-ticked). Specials never set the show's year, poster or plot |
| Movie | `Title (Year)/Title (Year).ext`, plus an NFO and `poster.jpg` |
| Video | `<VIDEOS_SUBDIR>/<Folder>/Name/Name.ext`, plus an NFO and `poster.jpg`. Pick an existing folder or type a new one; `/` nests |
| Audio | `Artist/Album/[NN - ]Title.ext` |

Episode thumbnails are named `<episode>-thumb.jpg`, and subtitles are linked beside the video. An
existing show folder is reused, and its `tvshow.nfo` is left alone.

### Audio

Only the audio stream is copied, without re-encoding, so the extension follows the source:
`.m4a`, `.opus`, `.ogg`, `.mp3` or `.flac`. It is tagged with title, artist, album artist, album,
track and year. Fields are prefilled from YouTube's music data when present, otherwise from an
`Artist - Title` video title and the channel name. Album falls back to the playlist, then `Singles`.

Youtarr's "MP3 Only" downloads appear in the queue and can only be filed as Audio. With "Video +
MP3", the download is listed once, as the video.

## Discord alerts

With `DISCORD_ADDED_WEBHOOK_URL` set, each filing posts an embed with a linked title, the
description, channel, library and thumbnail. Several episodes of one show filed together send one
combined message. A Discord outage never fails or delays a filing. Use a different webhook for the
waiting ping.

## Permissions

Run the container with the same `PUID` and `PGID` as Youtarr. Linux only lets a user hardlink a
file it owns, or can both read and write. The user also needs write access to every library folder
and to `data/`:

```bash
sudo chown -R 1000:1000 data     # use your PUID:PGID
```

## Security

Keep it on a private network. It has a basic login and no HTTPS of its own. `/healthz` and `/stats`
need no login: they report health and counts only, with no titles or paths, for monitors and
dashboards.

## How it behaves

- **Undo** removes only files that are unchanged since filing. It refuses when the source download
  is gone, because the library copy may then be the last one.
- **Queue age** is tracked in the database, because Youtarr sets file times to the upload date.
- **Bulk filing** groups by channel and sorts by playlist position when known, otherwise by upload
  date.
- **One media mount** is deliberate. The whole of `MEDIA_ROOT` is writable by the app, so safety
  inside it depends on the app's code rather than the mount.

## Development

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r build/requirements.txt pytest
.venv/bin/python -m pytest tests
```

Some tests need `ffmpeg` and Docker Compose, and are skipped without them.

## License

[AGPL-3.0-or-later](LICENSE). If you run a modified version that others use over a network, you
must offer them its source; point `SOURCE_URL` in the app at your copy. Bundled third-party code
is listed in [THIRD_PARTY_NOTICES.md](build/THIRD_PARTY_NOTICES.md).
