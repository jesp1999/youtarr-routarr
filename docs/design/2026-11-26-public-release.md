# Public release

Status: approved 2026-11-26.

## Intent

Make YoutarrRoutarr usable by other Youtarr users, publish it at
`github.com/jesp1999/youtarr-routarr`, and link it from the Youtarr project as a sister project.

## Goals

- A new user can set it up by editing one env file, without editing Compose or code.
- It works with Jellyfin and Plex.
- Audio-only downloads can be filed.
- The code is licensed under the AGPL-3.0.
- A ready-made container image is available.

## Non-goals

- Running without Docker as a supported setup.
- Emby and Kodi support. The README lists them as untested.
- Writing NFO files for music, or fetching online metadata.
- Configurable naming layouts.
- Supporting upgrades from versions that predate this release, beyond what already works.

## Anti-goals

- Changing where existing filings live or how they are named.
- Ever moving, changing or deleting a file in Youtarr's queue.
- A setup that starts writing files before the user turns dry-run off.

## Constraints

- Queue and libraries must be on one filesystem behind one mount, because they are hardlinked.
- The container user needs to own the queue files or have read and write access to them.
  Running it with Youtarr's user and group IDs is the documented way.
- Existing deployments that set every directory variable keep behaving the same.
- No personal domains, addresses, paths or IDs in tracked files.

## Approaches

| Approach | Trade-off |
|---|---|
| **One layout for both servers (chosen)** | Nothing to configure. May need extra files if the servers disagree. |
| A `MEDIA_SERVER` setting | Exact files per server, but two code paths, and switching strands older filings. |
| Documentation only for Plex | No work, but unproven when this was chosen. |

Testing then showed that Plex reads the files the app already writes, so the chosen approach needs
no changes to the filing layout.

### Test results

Sample downloads were filed with the app's own code into fresh Plex 1.43.5 and Jellyfin 12.1.0
containers.

| Check | Plex with NFO agents | Jellyfin |
|---|---|---|
| Titles, episode numbers, Season 00, descriptions, dates | Read from NFO, ahead of tags inside the video | Read from NFO |
| `-thumb.jpg`, `poster.jpg`, `fanart.jpg` | Read | Read |
| Videos in nested folders | Work in a movie library | Work in a home videos library |
| Audio | Correct with the Personal Media agent | Correct |

- Plex showed the NFO agents only after the server was signed in to an account.
- The regular Plex Music agent matched the sample artist "Band" to "The Band".
- YouTube tags did not appear in Plex as genres or labels. Collections were not checked.
- Not tested: playback, Plex client apps, and adding to a show folder that has no `tvshow.nfo`.

## Design

### Setup

One sample file, `.env.example`, replaces `youtarr-routarr.env.example`. The user copies it to
`.env`. Compose reads it and passes the app only the variables it needs.

| Variable | Sample value | Meaning |
|---|---|---|
| `BASIC_AUTH_USER` | `admin` | Login name |
| `BASIC_AUTH_PASSWORD` | blank | Required. The app refuses to start without it |
| `AUTH_DISABLED` | `0` | `1` turns the login off |
| `MEDIA_ROOT` | `/path/to/media` | Host folder holding the queue and libraries, seen as `/media` |
| `YOUTARR_INFO_DIR` | `/path/to/youtarr/jobs/info` | Youtarr's metadata folder, mounted read-only |
| `QUEUE_FOLDER` | `youtarr-queue` | Youtarr's download folder, under `MEDIA_ROOT` |
| `SHOWS_FOLDER` | `youtube-shows` | Blank turns shows off |
| `MOVIES_FOLDER` | `youtube-movies` | Blank turns movies off |
| `VIDEOS_FOLDER` | `videos` | Blank turns videos off |
| `VIDEOS_SUBDIR` | `YouTube` | Folder inside the videos library where filings start |
| `MUSIC_FOLDER` | `youtube-music` | Blank turns audio off |
| `PUID`, `PGID` | `1000` | User and group the container runs as |
| `TZ` | `Etc/UTC` | Time zone |
| `PORT` | `8000` | Host port |
| `DRY_RUN` | `1` | `0` allows writes |
| `DISCORD_WEBHOOK_URL` | blank | Queue-age ping |
| `DISCORD_ADDED_WEBHOOK_URL` | blank | Alert when media is filed |
| `QUEUE_PING_DAYS` | `7` | Age that triggers the ping |

- The app keeps its current variable names (`SHOWS_DIR` and so on). Compose builds each one from
  the folder name and leaves it empty when the folder name is blank.
- `.gitignore` covers `.env` and `*.env`, but not `.env.example`.
- The `data/` folder must exist and belong to `PUID:PGID` before the first start.
- Extras such as a reverse-proxy network belong in the user's own Compose override file.

### Optional libraries

- A blank or unset library directory turns that type off. The built-in library path defaults go.
  The queue keeps its default.
- At least one library is required; otherwise startup fails with a clear message.
- A disabled type is hidden in the interface and rejected if submitted.
- The default type is the first enabled of Video, Movie, Show, Audio.
- "File as a show" appears only when shows are enabled.
- `/healthz` checks the queue and enabled libraries only.
- Undo keeps working for filings in a library that was later turned off.

### Audio-only downloads

Youtarr writes `Channel - Title [id].mp3` for "MP3 Only", and an `.mp4` plus `.mp3` with the same
name for "Video + MP3".

- An `.mp3` with no video beside it for the same download appears in the queue.
- A video and `.mp3` pair appears once, as the video. All types stay available.
- An audio-only item offers only Audio. If audio is off, it is listed with a note saying how to
  turn it on.
- Filing copies the audio without re-encoding and tags it, as audio filing does today.
- Audio-only items are left out of "File as a show", and count toward queue totals and alerts.

### Undo safety (proposed)

Undo refuses to delete a filed video when the source is gone, but does not apply the same check to
audio filings. It will: if the source of an audio filing is missing, undo changes nothing and says
why. This matters more once `.mp3` downloads can be filed.

### Removal

Delete `relocate.py`, `backfill.py` and their tests. They only migrate filings from earlier
private versions. The automatic database upgrade stays.

### License

- Add the full AGPL-3.0 text as `LICENSE`, "version 3 or later" (proposed).
- Add a "Source" link to the page footer, so people using a hosted copy can get the code (proposed).
- Note the bundled htmx library and its license.

### Image and automation (proposed)

- GitHub Actions runs the tests on pushes and pull requests.
- A version tag such as `v1.0.0` publishes `ghcr.io/jesp1999/youtarr-routarr` with that version
  and `latest`, for amd64 and arm64.
- `docker-compose.yml` uses the published image. A second file, `docker-compose.build.yml`,
  builds from source.
- The image defaults to user `1000:1000`; Compose overrides it with `PUID:PGID`.

### Documentation

Rewrite the README for a new user:

1. What it does, and that it is a companion to Youtarr.
2. Requirements, including the one-filesystem rule.
3. Quick start, with dry-run first.
4. Pointing Youtarr at the queue folder, which the media server must not scan.
5. Settings.
6. Jellyfin setup.
7. Plex setup: server signed in; Movies and Videos use the Plex NFO Movie agent; Shows use
   Plex NFO Series; Music uses Plex Personal Media. State the version tested.
8. Filing layouts, Discord alerts, permissions, and security (keep it on a private network;
   basic login only, no HTTPS of its own).
9. Development and license.

### Publishing

The repository owner does these steps:

1. Create the empty GitHub repository, add it as a second remote named `github`, and push.
2. Tag the first release, then make the container package public.
3. Propose a short "Community projects" entry to Youtarr, as a `docs:` pull request against its
   `dev` branch. Draft the text as part of this work. Youtarr's maintainer decides.

## Testing

- Tests first for optional libraries, audio-only scanning and filing, and the undo check.
- Validate both Compose files with every library on, and with some blank.
- Build the image and file one video and one `.mp3` in a container.
- The filing layout does not change, so the server tests above need no rerun unless it does.

## Left to the builder

Code structure, wording in the interface, workflow action versions, and README phrasing.
