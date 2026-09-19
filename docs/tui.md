# The TUI (`karaoke-tui`)

A player-aware Textual shell. It watches the desktop over MPRIS, follows
whatever is playing, and renders time-synced lyrics.

## Layout

```
┌─ header ────────────────────────────────────────────────────────────┐
│  ___     _    _             ___                    ← figlet title   │
│ / __|___| |__| |___ _ _    | _ )_ _ _____ __ ___ _                  │
│ \___\___/_\__,_\___|_||_|  |___/_| \___/\_/\_/|_||_|                │
│   Gotye · scan · chromium · F major 129bpm · lrclib · offset +0.0s  │
├──────────────┬────────────────────────────────────┬─────────────────┤
│              │   Now and then, I think of when    │  mood square    │
│  cover art   │ ♪ Like when you said you felt so   │  key / bpm      │
│  (colour)    │   Told myself that you were right  │  sentiment arc  │
│              │   But felt so lonely in your comp  │  mood bars      │
│              │   ...                              │  rhythm         │
│              │                                    │                 │
│ source lrclib│                                    │                 │
│ lines  42    │                                    │                 │
│ length 4:05  │                                    │                 │
│ offset -0.3s │                                    │                 │
│ postproc done│                                    │                 │
│ workers  12  │                                    │                 │
│ cpu  [###__] │                                    │                 │
│ queue    0   │                                    │                 │
└──────────────┴────────────────────────────────────┴─────────────────┘
  Mode: scan (auto) · chromium          worker-load: [##___] 38% · queue 0
```

Both side columns are the same width, so the lyrics sit centred.

## Keys

Press `?` for the live list — it is generated from the bindings, so it cannot
go stale. The ones worth knowing:

| key | does |
|---|---|
| `D` | AI DJ Booth modal (Camelot suggestions, `dj-list` playlist, Ollama chat) |
| `v` | Lyric Vibe on current playing song (mood arc, sentiment bars, vocal delivery tips) |
| `H` | library overlay; picking a song closes it |
| `-` `/` `+` | adjust mood energy slider floor (real-time filtering) |
| `a` | enqueue highlighted track to end of active queue |
| `U` | shuffle upcoming queue items |
| `C` | clear active queue |
| `F` | focus mode — hides everything but the lyrics |
| `R` | mic/radio mode (songrec) |
| `A` | queue the playing track for post-processing |
| `T` | stats — library, pipeline, listening, keys, tempo |
| `?` | key reference |
| `,` `.` | nudge lyric sync ∓0.1s, `S` saves it for that track |

## AI DJ Booth & `dj-list` Playlist Management (`D`)

Pressing `D` opens the interactive **AI DJ Booth**:
- **Harmonic Flow Suggestions**: Suggests Camelot wheel adjacent keys (+/- 1 step, relative major/minor) and matching BPMs from currently playing or crowd top tracks.
- **Numbered 1-Click Queuing**: Suggestions are formatted as `[1]`, `[2]`, `[3]`, `[4]` with track details. Simply type `1`, `2`, `all`, or click `[+ Add #1]` / `[+ Add All]` to queue without needing raw track IDs.
- **Persistent `dj-list` Playlist**: Tracks added via the DJ are stored in PostgreSQL under the canonical `dj-list` playlist.
- **Live Player Sync & Follow**: Pressing `[🎧 Follow DJ]` or running `/follow-dj` loads `dj-list` directly into the live player queue and tracks playback changes.
- **Commands**:
  - `/suggest [strategy]`: Recommend next tracks (`harmonic`, `energy_up`, `cool_down`, `acoustic`).
  - `1`..`4` or `/queue <#|query|track_id>`: Add to `dj-list` and player queue.
  - `/queue all` (or `all`): Add all active suggestions.
  - `/dj-list` (or `/playlist`): View current tracks in `dj-list`.
  - `/follow-dj`: Follow & load `dj-list` into live player.
  - `/clear-dj`: Empty `dj-list`.
  - `/now`, `/vibe`, `/stats`, `/search <query>`.

## Library, Mood Slider & Playlist Controls

- **Search box**: Type words to build a ranked queue. Use `*` as an explicit "show all" search: it fills the queue with all playable tracks that have lyrics, then the Genre and Mood filters narrow that queue in real time (for example, choose `pop` and search `*` to queue all pop tracks).
- **Genre Filter**: A genre selector sits above the mood preset selector and is populated from the current CLAP `track_genre` classifications (for example `hip hop`, `punk rock`, `soul`, `techno`). It filters the library and the active search/queue results in real time, and combines with the mood energy slider/presets.
- **Interactive Mood Slider (`-` / `+` / `m`)**: Sidebar slider `energy ≥ 30% [███░░░░░░░] -/+` dynamically sets the energy floor and filters both the library list and search results in real time.
- **Sort Selector**: Sort tracks by `Artist / Title (A-Z)`, `🌱 Priority: Least Played`, `⭐ Most Played`, `Energy (high→low)`, `Energy (low→high)`, `BPM (fast→slow)`, `BPM (slow→fast)`, or `Key`. Play counts are stored per track (`tracks.play_count`, incremented on every play/discover event) so "least played" surfaces undiscovered tracks first.
- **Genre & Feeling Column**: Displays CLAP zero-shot genre label + acoustic feeling glyph (`Genre/Feel` e.g. `hip hop 🌙`, `rock 🔥`, `pop ⚡`). Feeling is derived from energy (arousal) and brightness (valence proxy): 🔥/☀/⚡/🌗/♡/🌙.
- **Playlist & Queue Controls**: Enqueue track (`a`), Shuffle upcoming (`U`), Clear queue (`C`), Follow Karaoke Queue (`o`; locked on while a local queue is active).
- **Keep the vibe going** (`G`): appends tracks that fit the *whole* current queue. Seeds on every queued track by audio similarity (CLAP embedding, with a per-seed spectral fallback), pools the neighbours, and adds the ones closest to the set — a track that fits several queued songs outranks one that only resembles a single outlier. Already-queued tracks are skipped and each artist is capped. Needs the OpenSearch audio/CLAP indexes; if no seed has a vector it reports that instead of adding anything.

## Operations & Admin Dashboard (`make admin`)

Launch with `make admin` or `python -m karaoke.admin_tui`:
- **Worker Management**: Real-time controls for the Celery post-processing worker (`+` start, `-` stop, `R` restart) plus Flower dashboard visibility at http://127.0.0.1:5555. Falls back to querying `systemctl --user` directly if the control API is unreachable.
- **Audio Processing & Vector Ingestion Pipeline**:
  - `b` — Run audio backfill (`scripts/fill_analysis_and_vector_gaps.py`): fills key/BPM/energy gaps and CLAP genre labels.
  - `v` — Rebuild OpenSearch vector indices (`tracks`, `tracks-lines`).
  - `a` — Analyse captured recordings and ingest detected-song vectors.
- **Error Log Diagnostics**: Real-time exception & traceback viewer for worker and backend troubleshooting.
- **Folder Scan & Ingestion**: Trigger folder scans or file ingestion directly from the Operations dashboard.

## Making the lyrics bigger

A terminal draws **one font at one size**, so the lyrics cannot be rendered
larger than the rest of the interface. Three approaches were tried and
rejected: figlet block type (unreadable at the sizes that fit), Unicode
fullwidth forms (drawn as tofu by most monospace fonts), and letter spacing
(breaks the reading rhythm).

What works instead: **raise the terminal font size**, and let the interface get
out of the way. A larger font means fewer cells, so the panels adapt:

| terminal | behaviour |
|---|---|
| < 110 columns | side columns hidden |
| < 32 rows | header compacted, title bar hidden |
| any size, `F` | everything hidden but the lyrics (93–96% of the screen) |

Measured lyric share at 80×24: **28% before, 77% after**.

## Stats (`T`)

Seven panels over the library and the pipeline:

```
library                     pipeline
  tracks       487            analysed     255  ######...... 52%
  karaoke-ready 422 ####. 87% backlog      232
  plain only     8            word timings   6
  no lyrics     57            workers    12 up
  sources      306            queue         0

lyric sources               keys              tempo
  lrclib       307 ####...     E minor  29      moderato 100-129  130
  whisper       72 #......     C minor  25      andante  70-99     54
  whisper_align 39 #......     A minor  20      allegro 130-159    44
```

`karaoke-ready` is the number that matters — tracks with *synced* lyrics, the
ones that can actually drive a session. `backlog` is what
`scripts/enqueue_postprocess.py` would pick up.

Degrades gracefully: a broker outage drops the worker rows and keeps the rest.

## AI DJ Chat (`D`)

Pressing **`D`** opens the interactive **Karaoke AI DJ Booth** modal. From here, you can:
- Query currently playing audio, detected musical key, and Camelot codes (`/now`).
- Request harmonically compatible follow-up tracks or energy progression (`/suggest`).
- Inspect lyric sentiment, emotional arcs, and singer vocal delivery tips (`/vibe`).
- Check crowd favorites and library stats (`/stats`).
- Search the 18,000+ local track database (`/search <query>`) and launch playback (`/play <track_id>`).
- Chat in natural language backed by local **Ollama** (`qwen3:1.7b` / `qwen3:latest` on AMD ROCm).

## Modes

- **scan** — a browser or desktop player is playing; position comes from MPRIS.
- **spotify** — Spotify is playing; sync and control it.
- **radio** (`R`) — songrec identifies room audio through the microphone. There
  is no MPRIS position, so the playhead is dead-reckoned from where songrec
  heard us plus elapsed time, plus a **12.6s forward lead**: songrec listens for
  ~10s before answering, so the offset it reports is already stale and the
  highlight would sit a couple of lines behind. `karaoke -r` applies the same
  figure (`DEFAULT_LEAD_S`, overridable there with `--lead`), so both modes stay
  in step. Declines to start if `karaoke --radio` already holds the mic.
- **browse** — nothing playing; drive the library list.

Lyrics are resolved from the cache first, then fetched from LRCLIB in the
background on a miss. Lookup tolerates spelling differences between sources —
songrec stores `James Brown & The Famous Flames` and `[2020 Remaster]` where a
browser reports plainer text — while still refusing to match a different
artist.

## Cover art

The left column shows the current track's artwork, drawn as coloured terminal
cells. It comes from the player's own `mpris:artUrl`, which browsers write to a
local file, so nothing is downloaded. Decoding goes through ffmpeg (already
required for downloads), which copes with PNG, JPEG or WebP alike.

Each pixel is a **space with a background colour** rather than a block
character: a space is unambiguously one cell wide, so the art cannot shift a
column on a terminal that draws block glyphs wide.

### Calibrating the aspect ratio

Terminal cells are roughly twice as tall as they are wide, but the exact figure
depends on the font and on any line spacing the terminal adds — and a TUI
cannot query it. The default is **2.5**, measured by eye on a real terminal
rather than taken from the theoretical 2.0. If the cover still looks stretched,
calibrate:

```bash
python -m karaoke.coverart          # prints a square
```

If that block looks taller than it is wide, raise the value; if squat, lower it:

```bash
export KARAOKE_CELL_ASPECT=2.4
```

## Sourcing

Lyrics and sources arrive by different routes. Radio discovery and backfill
resolve a track against LRCLIB by artist/title, which fills in the *words* and
records no *URL* — so the track looks complete, but `Enter` opens a search page
instead of playing it, and post-processing cannot analyse it because key/BPM
needs the audio.

```bash
karaoke-find-sources --dry-run     # see what it would store
karaoke-find-sources --limit 50    # store them
```

It searches YouTube per track and stores the best match using the same verified
picker the backfill uses, so a wrong video is not saved. Where the track has no
stored duration, the last synced lyric acts as evidence — a song cannot be
shorter than its own words, and one several times longer is an album rip.

## Environment

| variable | default | meaning |
|---|---|---|
| `KARAOKE_CELL_ASPECT` | `2.5` | cell height ÷ width, for cover art |
| `KARAOKE_SYNC_OFFSET` | `0.0` | seconds to pull lyrics back in scan mode |
| `KARAOKE_SYNC_OFFSET_SPOTIFY` | `0.0` | same, for Spotify |
| `KARAOKE_COOKIES_FROM_BROWSER` | — | browser to take YouTube cookies from |
