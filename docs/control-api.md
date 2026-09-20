# Control API: players, play sessions, streaming sample, folder scan, audio cut

The host-side **control API** (`karaoke.ctrl_api`, default `http://localhost:8765`)
exposes everything that needs the desktop session — MPRIS players (`playerctl`),
`ffmpeg`, and `songrec`. It is the counterpart to the cluster-deployable
read-only [library API](api.md). Every action the TUI performs is available here
over HTTP, so a future web UI drives the same backend with no new server code.

Start it with `make ctrl-api` (or the `karaoke-ctrl-api` systemd unit). It binds
loopback only, since it spawns local processes.

## Playback sessions

```
POST   /api/play                    open a stream/search and return session_id
GET    /api/play/sessions           list recent logical play sessions
GET    /api/play/sessions/{id}      inspect one logical play session
DELETE /api/play/sessions/{id}      pause playback and mark the session stopped
```

`POST /api/play` accepts `url`, `kind`, `artist`, `title`, and `prefer_audio`
(default `true`). When YouTube artist/title are known, direct watch URLs (`music.youtube.com/watch?v=VID`) are preserved for immediate playback, while missing URLs use search queries (`music.youtube.com/search?q=artist+title`).

The response includes a `session_id` (`play_...`), `pid` if a process was
spawned, the resolved inputs, and timestamps. This is the web-UI handle for
later status/stop actions.

## Chrome CDP window control

```
GET  /api/players/window          get active Chrome kiosk window ID, bounds & state
POST /api/players/window         set window state ("fullscreen", "normal", "minimized", "focus") or geometry
```

`POST /api/players/window` controls the dedicated Chrome kiosk window directly over CDP (`:9222`), allowing the web UI or TUI to focus the window or toggle fullscreen playback.

## Post-processing workers & operations

```
GET  /api/workers/status          active systemd units, worker CPU%, RAM, queue depth
POST /api/workers/scale           scale worker pool up/down (body: {"target": 0..6})
GET  /api/logs/errors             recent exception & error log lines
```

`POST /api/workers/scale` manages `karaoke-postprocess@1..6.service` user units dynamically via `systemctl --user`.

## Player controls (MPRIS)

```
GET  /api/players                sessions: all names, which are playing, the active one
GET  /api/players/current        current track + status + position (?player=<mpris_name>)
POST /api/players/play-pause     toggle play/pause      body: {"player": "<name>"?}
POST /api/players/pause          pause (never resumes)  body: {"player": "<name>"?}
POST /api/players/next           next track             body: {"player": "<name>"?}
POST /api/players/previous       previous track         body: {"player": "<name>"?}
POST /api/players/seek           relative seek          body: {"offset_s": 5.0, "player": "<name>"?}
```

`player` is optional everywhere: omit it and playerctl targets the player that is
actually **Playing** (see `playing_player()`), which avoids a paused kiosk tab
shadowing the real source. A control that no player can service returns **409**
rather than a silent success.

`GET /api/players/current` returns the cleaned metadata (browser titles
normalised, YouTube/YT Music recognised), playback `status`, `position_s`, and
`art_url`.

## Folder scan (ingest a music library)

```
POST /api/scan/folder
  body: {
    "dir": "/home/tina/Music",
    "use_fingerprint": true,     # songrec/Shazam when tags are missing or wrong
    "classify_audio": true,      # key/BPM + CLAP embedding + zero-shot genre
    "resolve_streaming": true,   # resolve YouTube + Spotify links
    "dry_run": false,
    "limit": null
  }
```

A **dry run** returns the enrichment preview synchronously (`status: "preview"`
plus per-file `items`). A real ingest can run long over a large library, so it is
dispatched to the background (`status: "accepted"`); poll the library API's
`/api/tracks` for the new rows. The same logic backs the `karaoke-folder-scan`
CLI and `make folder-scan DIR=...`.

Pipeline per file (`karaoke.folder_scan.scan_and_ingest_folder`):

1. **Tags** via mutagen (`tags.extract_tags`).
2. **Fingerprint** via `songrec` when the tags are missing/weak
   (`identify.identify_file_fingerprint` — 10s ffmpeg slice → Shazam).
3. **Classify** key/BPM/energy (`analyze.analyze_audio`), CLAP embedding
   (`clap_vector.embed_audio`), zero-shot genre (`genre.classify`).
4. **Resolve** YouTube (`youtube.search` + `source_select.select_best_source`)
   and Spotify (`SpotifyClient.search_track`).
5. **Ingest** into SQLite (`tracks`, `sources`, `lyrics`, `track_analysis`,
   `track_analysis_history`, `track_genre`). `sources` accumulates local,
   YouTube/YT Music, and Spotify rows; it does not replace existing sources.

## Audio cut

```
POST /api/audio/cut
  body: {"file_path": "/path/in.webm", "start_s": 30.0, "duration_s": 45.0,
         "output_path": "/tmp/out.wav"?}
```

Cuts `[start_s, start_s+duration_s)` out of a file with ffmpeg (stereo, 44.1kHz)
and returns the `output_path`. Omit `output_path` for a temp file. Missing input
is **400**, a missing ffmpeg is **503**.

## Sample stream & record

```
POST   /api/sample                       key/BPM from a short live excerpt (sync)
GET    /api/sample/stream                SSE: start/progress/complete/error events
GET    /api/tracks/{id}/analysis/history analysis versions from samples/scans
POST   /api/record/start                 begin unattended capture (body: source?, keep_audio?, note?)
POST   /api/record/stop                  stop one or all captures
GET    /api/record/status                captures running in this process
POST   /api/recordings/{id}/analyse      decompile a recording into the DB
DELETE /api/recordings/{id}/audio        drop audio, keep markers
```

`GET /api/sample/stream?artist=A&title=B&seconds=45` streams Server-Sent Events:

```
event: start
data: {"status":"started", ...}

event: progress
data: {"status":"capturing", "elapsed_s": 12.0, "percent": 26.7}

event: complete
data: {"status":"analysed", "key":"G Major", "bpm":123.0, ...}
```

`ApiClient.sample_stream_url(...)` returns the URL for a TUI/web EventSource.

Sample mode stores analysis when the request has a resolvable `artist` and
`title`: it upserts the current `track_analysis` row and appends an immutable
`track_analysis_history` entry. Re-running a scan or sample therefore updates the
canonical key/BPM/energy/brightness but preserves previous versions for comparing
sample vs full-file, local vs YouTube, or later source-specific runs.

See [Record mode](modes/record.md) for the capture/decompile detail.

## Client library (`karaoke.api_client.ApiClient`)

`ApiClient` is the single Python entry point both the TUI and a future web UI
use. Every method maps to one endpoint above, and falls back to the in-process
function when no server is reachable — so `karaoke-tui` works standalone, yet the
*same code path* drives the HTTP API the moment `karaoke-api` / `karaoke-ctrl-api`
are running.

An end-to-end integration test suite (`tests/test_e2e_recordings_api.py`) and a
live CLI runner (`scripts/e2e_test_recordings_api.py`) verify the entire lifecycle
(listing, detail inspection, metadata PATCHing, live capture start/stop,
decompiling analysis, audio serving, and discarding) over HTTP.

```python
from karaoke.api_client import ApiClient
api = ApiClient()                      # honours KARAOKE_API_* / KARAOKE_CTRL_* env
api.list_players()                     # -> {"players": [...], "playing": [...], "active": ...}
api.player_play_pause("spotify")       # -> True
api.play(artist="Portishead", title="Glory Box")
api.sample(artist="A", title="B")
api.scan_folder("~/Music", dry_run=True)
api.record_start(note="evening")
```

The TUI (`karaoke.tui.KaraokeTui`) routes player controls (play/pause, next,
previous, seek) and record start/stop through this client; a web UI does the
same over HTTP with no new backend code.

Also ported onto `ApiClient` (HTTP-first, SQLite/in-process fallback):

- `karaoke.browse.KaraokeBrowser` — track listing (`list_tracks`) and playback (`play`).
- `karaoke-stats` (`cli.stats_main`) — reads `GET /api/stats?limit=&days=`.
- `karaoke-recording --analyse/--discard` (`recording_worker.recording_main`) —
  mutating actions go through `record_analyse` / `record_discard_audio`.

`GET /api/stats` now accepts `limit` (top-N) and `days` (time window) so the CLI
and a web dashboard read identical numbers.

## Related

- [Library API](api.md) — read-only tracks, lyrics, stats, recordings inspection
- [Folder scan CLI](makefile-targets.md) — `make folder-scan DIR=...`

## CORS (web dashboard)

The control API binds loopback, but a browser-based frontend (e.g. the Angular
app at `karaoke/web/`) still needs CORS headers to call it cross-origin from the
dev server or a packaged build. Allowed origins default to
`http://localhost:4200` and are configurable via the comma-separated
`KARAOKE_WEB_ORIGIN` environment variable.

## Queue (mutable play queue)

```
GET   /api/queue                 list the active queue: [{index, artist, title, url, key, note}]
POST  /api/queue                 append a track   body: {"artist": "...", "title": "...", "url": "..."?}
DELETE /api/queue/{index}        remove one item at that position
PATCH /api/queue/reorder         move an item      body: {"from_index": 2, "to_index": 0}
```

Backed by the same `active_queue_state` SQLite table the TUI's queue view reads
and writes (`localcache.load_active_queue`/`save_active_queue`), so mutating the
queue from the web dashboard is immediately visible to a running `karaoke-tui`.

## Jobs (background task status)

Endpoints that dispatch background work (`POST /api/recordings/{id}/analyse`,
`POST /api/scan/folder` in non-dry-run mode, and the staging endpoints on the
library API) now return a `job_id` alongside `status: "accepted"`. Poll:

```
GET /api/jobs/{job_id}   -> {"job_id", "status": "pending"|"running"|"done"|"error", "progress", "result", "error"}
```

The job registry is in-memory per-process (see `karaoke.jobs`), so a job created
by the control API is only visible via the control API's `/api/jobs/{job_id}`,
and a staging job created by the library API only via the library API's route.
