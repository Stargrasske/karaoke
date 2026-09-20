# karaoke

A local karaoke + lyric-search platform.

- **Drive** playback/cache/history from local **SQLite**: known tracks, source URLs/URIs, approved lyrics, radio stats and backfill gaps.
- **Fetch** synced (timestamped) lyrics from **LRCLIB** (free, no key), cached in SQLite.
- **Index** SQLite + local library data into optional **OpenSearch** vector indexes when semantic search/training features are wanted.
- **Karaoke** CLI: identify the current song (file / text / live via `songrec`), then render time-synced highlighted lyrics.
- **lyricsearch**: semantic "find the song that goes '...'" via lyric embeddings (OpenSearch-derived index).
- **Whisper fallback**: for local files with no LRCLIB match, transcribe to approximate synced lyrics with faster-whisper.
- **Backfill**: log missing radio/player lyrics into SQLite and process them later with `karaoke-backfill`.

## Source model (hybrid)

- `~/Music` = optional audio source for scanning and Whisper transcription.
- Spotify (your account) = optional metadata/source seed (liked songs / playlists). Spotify's API does **not** allow audio download, so no Whisper for Spotify-only tracks unless a matching audio source is found elsewhere.
- SQLite (`~/.local/share/karaoke/karaoke.db`) is the operational database: tracks, sources, lyrics, stats and backfill gaps.
- OpenSearch is optional and derived from SQLite/local files for semantic search, sentiment and training experiments; playback must not require the cluster.

## Layout

```
src/karaoke/        # the package
tests/              # pytest
deploy/             # kind + opensearch manifests
docs/               # MkDocs project documentation
```

## Safety

The scanner/deploy scripts **only** talk to the `kind-karaoke` kube-context. They abort if the current
context is not `kind-*`, so nothing can touch the AKS work clusters.

See the implementation plan: `~/.hermes/plans/2026-08-29_173500-local-karaoke-platform.md`.

## Dev quickstart

```bash
# Windows PowerShell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
cd web; npm install; cd ..
python scripts/dev.py

# Linux/macOS
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e ".[dev]"
(cd web && npm install)
python scripts/dev.py
```

The development command starts the read-only library API on port 8000, the
desktop control API on port 8765, and the Angular dashboard on port 4200. It
waits for each service to become healthy and stops the complete process tree on
Ctrl+C. Open `http://localhost:4200` for the dashboard. Use `--no-control` or
`--no-web` when working on only part of the stack.

Python 3.11 or newer and Node.js/npm are required. OpenSearch, RabbitMQ, Docker,
and pre-seeded data are optional for the first view. On Windows, recording uses
an ffmpeg DirectShow microphone; automatic `songrec` identification remains
Linux-only.

## Usage

```bash
# Index a music library (tags -> LRCLIB lyrics -> embed -> OpenSearch)
music-index --dir ~/Music

# Semantic search: find a song by what its lyrics mean
lyricsearch "religion faith losing my belief"
lyricsearch --keyword "teen spirit"          # keyword mode

# Karaoke: synced, highlighted lyrics in the terminal
karaoke "R.E.M. - Losing My Religion"        # by name (uses cache/LRCLIB)
karaoke --file song.mp3                       # from a local file's tags
karaoke --file song.mp3 --transcribe          # Whisper fallback if no LRCLIB lyrics
karaoke --file song.mp3 --force-transcribe    # always transcribe (skip cache + LRCLIB)
karaoke --listen                              # identify room audio via mic (songrec), sync once
karaoke --radio                               # CONTINUOUS: follow live radio, re-lock as songs change
karaoke --output                              # identify audio playing on this machine
karaoke --youtube URL                         # karaoke a YouTube video (yt-dlp title -> lyrics)
karaoke --youtube URL --download              # + fetch audio so Whisper/beats can run
karaoke-yt URL                                # shorthand: URL is positional (no flag)
karaoke-yt URL -d --transcribe                # download + Whisper if no LRCLIB match
karaoke-yt --cache-status                     # show downloaded YouTube audio size
karaoke-yt --prune-cache 100                  # prune oldest downloads to <= 100 MiB
karaoke-stage youtube URL                     # stage YouTube captions for review
karaoke-stage list                            # review unapproved staged lyrics
karaoke --print "Queen - Bohemian Rhapsody"  # just print the LRC, no live player
karaoke --spotify                             # lock lyrics to the LIVE Spotify position
karaoke --player                              # get current song from any desktop player (MPRIS)
```

## Stats

Every play/identification is recorded in the local cache. Report it with:

```bash
karaoke-stats            # plays, discoveries, top tracks/artists, per-mode, cache hit-rate
karaoke-stats --days 7   # only the last week
karaoke-stats --json     # machine-readable
make stats               # same summary via make
```

In `--radio` mode the stats also track song *discovery*: each new song songrec
identifies is a `discover` event, drift-corrections are `relock`, and lyrics
served from the local cache are `cache_hit` — so you can see how often radio
re-locked known songs offline.

## Offline behaviour and song identification

- **Known song, no cluster**: after a song has been played once, its lyrics live
  in the local SQLite cache. Radio/live modes replay it with no cluster and no
  LRCLIB request.
- **Unknown song**: identification still uses `songrec`, which queries Shazam
  **online**. There is no offline audio-fingerprint match (that would need
  AcoustID/Chromaprint); the local cache stores recognition *results*, not
  fingerprints.

## Sound check

Before a live session, verify the audio + identify + lyrics stack:

```bash
make test-audio          # pactl devices, songrec, LRCLIB reachability
make mic-test            # live mic VU meter (SECS=4 to change duration)
```

### Live audio sync (mic / room)

`karaoke --listen` (mic) or `--output` (this machine's audio) identify the song
via songrec AND read its Shazam `offset` (position in the track at match time).
The player anchors that offset to a monotonic clock, so lyrics scroll in sync
with whatever is playing in the room — no keypress, no Spotify needed. Offsets
from several matches are clustered (median of the tightest group) to reject the
occasional chorus-repeat outlier. Use `--offset <secs>` to nudge for latency if
lines run early/late.

**Live nudge controls** (in `--radio`, `--listen`, `--output` players): the
song-recognition step listens for ~10s before returning a position, which can
leave the highlighted line running a couple of lines behind the audio. Correct
it on the fly without restarting:

| Key | Action |
|-----|--------|
| `b` | lyrics are **behind** → jump the highlight **forward** one line |
| `v` | lyrics are **ahead** → step the highlight **back** one line |
| `0` | reset the nudge to the `--offset` baseline |
| `q` | quit |

Each press snaps the highlight exactly one line and the accumulated nudge is
shown in the footer (e.g. `nudge +6.2s`), so it sticks for the rest of the song.
`--offset <secs>` still sets the starting bias if you already know the lag.

**Default forward lead**: because that ~10s recognition window biases the
reported position low, mic/radio locks now start with a **+12.6s forward
pre-bias** baked in (`DEFAULT_LEAD_S`), so the highlight lands close to the audio
without tapping `b` at all. Tune or disable it with `--lead <secs>` (e.g.
`--lead 0` for the raw offset, `--lead 8` for a smaller push). `--lead` stacks on
top of `--offset`; the `0` key resets the live nudge back to this baseline.

### Word-level highlight

The active line highlights the **current word in purple** (magenta) on top of the
blue line background. LRCLIB only timestamps whole lines, so the word playhead is
interpolated: the line's on-screen duration (until the next line's timestamp, or
a 4s tail for the last line) is spread evenly across its words. It's an estimate,
not per-word karaoke timing, but it gives a natural left-to-right sweep to sing
along to. Falls back cleanly on blank lines.

### Spotify position sync

`karaoke --spotify` reads the track currently playing on Spotify and scrolls the
lyrics locked to the **real playback position** (polls progress + interpolates),
so lines advance in time with the audio — no keypress needed. It reuses the OAuth
credentials Hermes already stored in `~/.hermes/auth.json` (no separate login).
Spotify's API forbids audio download, so these tracks use LRCLIB lyrics only
(no Whisper); metadata can be indexed for search via the Spotify importer.

### YouTube mode

`karaoke --youtube URL` resolves a YouTube video to its lyrics: [yt-dlp] reads the
video's title/uploader/duration, a smart parser strips promo decorations
(`(Official Music Video)`, `[Lyrics]`, `| Label`, VEVO/`- Topic` uploader noise)
and splits `Artist - Title`, and the result flows through the normal
LRCLIB → cache → synced-render pipeline. Auto-generated `Art - Topic` channels use
the uploader as the artist; `music.youtube.com` track/artist tags are used
verbatim when present. Timing is keypress-started (like `--file`); to sync to
YouTube actually playing through your speakers, use `--output` or `--radio`.

`karaoke-yt URL` is a shorthand console entry for the same flow (the URL is the
positional argument, no `--youtube` flag needed): `karaoke-yt URL -d --transcribe`,
`karaoke-yt URL --print`, etc.

Requires the optional extra: `pip install -e ".[youtube]"` (or `pip install yt-dlp`).
Unlike Spotify, YouTube audio *can* be fetched — `--youtube URL --download` saves
the audio so the Whisper fallback and librosa beat detection work on videos with
no LRCLIB match. Downloads go to `~/.local/share/karaoke/youtube/` by default and
are auto-pruned after each download to `KARAOKE_YT_CACHE_MAX_MB` (default 500 MiB;
set to `0` to disable). You can inspect or clean the cache manually:

```bash
karaoke-yt --cache-status          # count + MiB + directory
karaoke-yt --prune-cache 100       # delete oldest audio until <= 100 MiB
karaoke-yt --clear-cache           # delete all downloaded YouTube audio
karaoke-yt URL -d --cache-max-mb 50 # per-run cap override
```

Downloading is a YouTube ToS gray area; metadata-only is the default.

#### Staging YouTube captions / crawled lyrics

When LRCLIB has no lyrics but YouTube displays captions/lyrics, keep them out of
the trusted cache until reviewed:

```bash
karaoke-stage youtube URL              # fetch captions and stage as pending
karaoke-stage list                     # list pending candidates
karaoke-stage show 12                  # inspect raw staged lyrics
karaoke-stage approve 12               # copy approved candidate to local cache
karaoke-stage reject 12                # leave it out of the approved cache
```

This is the review queue for lower-trust sources: YouTube manual/auto captions
now, and later web crawls or Whisper training/transcription output. Approved rows
are copied into the normal local lyrics cache; pending/rejected rows remain
separate and never affect `karaoke` lookup.

#### YouTube Premium / library access (cookies)

If you have YouTube Music Premium, you can authenticate the fetch with your
logged-in session to get higher-bitrate audio (better Whisper/beat results) and
access library-only / private / age-restricted tracks:

```bash
karaoke-yt URL --download --cookies-from-browser firefox   # read cookies live from a browser
karaoke-yt URL --download --cookies ~/yt-cookies.txt        # or an exported cookies.txt
```

The browser spec accepts yt-dlp's full form, e.g. `firefox:PROFILE`,
`chrome+gnomekeyring`, `firefox:prof::Container`. Notes:

- This does **not** use Premium's in-app "Download" — those offline files are
  DRM-locked and unreadable. It authenticates the normal yt-dlp fetch *as you*.
- Automating downloads with your account cookies is against YouTube's ToS and can
  get an account flagged with heavy use. Fine for occasional karaoke; your call.
  Anonymous, metadata-only stays the default.

[yt-dlp]: https://github.com/yt-dlp/yt-dlp

### Desktop player (MPRIS)

`karaoke --player` (or `-p`) gets the current song from any MPRIS-compatible
desktop media player (Spotify, VLC, web browsers playing media, etc.) via the
`playerctl` command-line tool.

This is a one-shot lookup; if the song changes, re-run the command. It's a
convenient way to avoid typing the artist and title for a song already playing
on your desktop. You may need to `emerge media-sound/playerctl` if it's not
already installed.


