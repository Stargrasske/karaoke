"""Host-side control API for karaoke playback.

This service is the counterpart to :mod:`karaoke.api`. It runs **on the user's
desktop machine**, not in the cluster, because launching playback requires a
display server and a media player (``xdg-open`` / ``playerctl``) that do not
exist inside a container.

It binds to loopback by default: it can spawn local processes, so it must not
be exposed to the network.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Optional, List, Dict
from urllib.parse import quote_plus

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import jobs
from .logger import log
from .player_open import open_song_url

CTRL_API_VERSION = "0.5.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import recorder
    try:
        recorder.reconcile_stale()
    except Exception:
        log.debug("reconciling stale recordings on ctrl_api startup failed", exc_info=True)
    yield


app = FastAPI(
    title="Karaoke Control API",
    description=(
        "Host-side playback control for karaoke. Runs on the desktop session "
        "and launches media players; not intended for cluster deployment."
    ),
    version=CTRL_API_VERSION,
    lifespan=lifespan,
)

# The control API binds to loopback, but the Angular dev server (a different
# origin/port) still needs CORS headers to call it from the browser.
_web_origins = [
    origin.strip()
    for origin in os.environ.get("KARAOKE_WEB_ORIGIN", "http://localhost:4200").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_web_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PlayRequest(BaseModel):
    url: Optional[str] = None
    kind: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None
    prefer_audio: bool = True


_PLAY_SESSIONS: dict[str, dict[str, Any]] = {}


def _play_session_view(session: dict[str, Any]) -> dict[str, Any]:
    return dict(session)


class QueueDetails(BaseModel):
    name: str
    messages: int
    messages_ready: int
    messages_unacknowledged: int
    consumers: int


class WorkerUnitDetails(BaseModel):
    unit: str
    active: str
    running: bool


class WorkerStatus(BaseModel):
    orchestrator: str
    available: bool
    dashboard_url: Optional[str] = None
    workers_active: int
    queue_depth: int
    queue_details: QueueDetails
    worker_details: List[WorkerUnitDetails]
    reason: Optional[str] = None


class ScaleRequest(BaseModel):
    target: int


class RecordRequest(BaseModel):
    """Start a capture. Everything is optional; the defaults are the useful ones."""

    source: Optional[str] = None       # PipeWire source; default is the playing sink
    keep_audio: bool = False           # audio is a means to metadata, not a library
    note: Optional[str] = None         # optional note / description for the recording session


class StopRequest(BaseModel):
    recording_id: Optional[int] = None  # omit to stop every session this process owns


class SampleRequest(BaseModel):
    """Analyse what is playing right now by recording a short excerpt."""

    artist: Optional[str] = None
    title: Optional[str] = None
    seconds: Optional[float] = None
    source: Optional[str] = None


class PlayerControlRequest(BaseModel):
    """Target a specific MPRIS player, or let playerctl choose when omitted."""

    player: Optional[str] = None


class SeekRequest(BaseModel):
    """Seek the (targeted) player by a relative offset in seconds."""

    offset_s: float
    player: Optional[str] = None


class FolderScanRequest(BaseModel):
    """Scan a local music folder and ingest it into the library."""

    dir: str
    use_fingerprint: bool = True
    classify_audio: bool = True
    resolve_streaming: bool = True
    dry_run: bool = False
    limit: Optional[int] = None


class AudioCutRequest(BaseModel):
    """Cut a slice out of an audio file with ffmpeg."""

    file_path: str
    start_s: float = 0.0
    duration_s: float
    output_path: Optional[str] = None


class WindowRequest(BaseModel):
    """Control Chrome CDP window state or bounds."""

    state: Optional[str] = None  # "normal", "minimized", "maximized", "fullscreen", "focus"
    left: Optional[int] = None
    top: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness probe reporting whether a desktop session is present."""
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return {
        "status": "ok",
        "version": CTRL_API_VERSION,
        "role": "control",
        "display": "available" if has_display else "headless",
    }


@app.get("/api/events/recent")
def recent_task_events(
    since_ts: Optional[float] = Query(default=None, description="Only events newer than this epoch ts"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Recent Celery task-completion events from the OpenSearch ledger.

    Populated by the Argo Events sensor when a `karaoke.tasks.*` task succeeds.
    A TUI / web-UI poller reads this to refresh a just-finished track in place
    (new key/BPM/lyrics) without a full reload that would kill in-process work.
    Best-effort: returns an empty list when the cluster/index is unavailable.
    """
    from . import events
    items = events.recent_events(since_ts=since_ts, limit=limit)
    return {"events": items, "count": len(items)}


@app.post("/api/play")
def play_track(req: PlayRequest) -> dict[str, Any]:
    """Open/play a song URL and return a durable-ish session handle."""
    url = req.url
    kind = req.kind
    artist = req.artist or ""
    title = req.title or ""

    if not url:
        query = quote_plus(f"{artist} {title}".strip())
        if not query:
            raise HTTPException(
                status_code=400, detail="No URL or artist/title provided"
            )
        url = f"https://music.youtube.com/search?q={query}"
        kind = "youtube_music_search"

    try:
        pid = open_song_url(
            url, kind, artist=artist, title=title, prefer_audio=req.prefer_audio)
    except Exception as exc:
        log.exception("Control API play error for %s", url)
        raise HTTPException(status_code=500, detail=f"Failed to launch player: {exc}")

    session_id = f"play_{uuid.uuid4().hex[:12]}"
    session = {
        "session_id": session_id,
        "status": "launched",
        "url": url,
        "kind": kind,
        "pid": pid,
        "artist": artist,
        "title": title,
        "prefer_audio": req.prefer_audio,
        "launched_at": time.time(),
    }
    _PLAY_SESSIONS[session_id] = session
    return _play_session_view(session)


@app.get("/api/play/sessions")
def list_play_sessions() -> dict[str, Any]:
    sessions = sorted(_PLAY_SESSIONS.values(),
                      key=lambda s: s.get("launched_at", 0), reverse=True)
    return {"sessions": [_play_session_view(s) for s in sessions],
            "count": len(sessions)}


@app.get("/api/play/sessions/{session_id}")
def get_play_session(session_id: str) -> dict[str, Any]:
    session = _PLAY_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Play session not found")
    return _play_session_view(session)


@app.delete("/api/play/sessions/{session_id}")
def stop_play_session(session_id: str) -> dict[str, Any]:
    session = _PLAY_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Play session not found")
    # The dedicated player is usually browser/MPRIS-controlled rather than a
    # child process we should kill. Pause it and mark the logical session stopped.
    try:
        from . import playerctl
        playerctl.pause()
    except Exception:
        log.debug("failed to pause player for session %s", session_id, exc_info=True)
    session["status"] = "stopped"
    session["stopped_at"] = time.time()
    return _play_session_view(session)


# -- record mode ----------------------------------------------------------
#
# These live here rather than in karaoke.api because capture needs PipeWire, a
# desktop audio session and a long-lived ffmpeg child -- none of which exist in
# a container. Inspecting recordings is read-only over SQLite and stays there.


@app.get("/api/record/devices")
def record_devices() -> dict[str, Any]:
    """List audio input devices this host can record from.

    Windows only (DirectShow enumeration via ffmpeg) — on Linux the source is
    resolved automatically from PipeWire/PulseAudio instead, so this always
    returns an empty list there.
    """
    from . import audio_backend

    ffmpeg_path = shutil.which("ffmpeg")
    if shutil.which("songrec"):
        identification_backend = "songrec"
    else:
        from . import identify_shazamio
        identification_backend = "shazamio" if identify_shazamio.available() else "unavailable"
    if not audio_backend.IS_WINDOWS:
        return {
            "devices": [],
            "platform": "linux",
            "capture_backend": "PulseAudio / PipeWire",
            "capture_mode": "system output",
            "ffmpeg_path": ffmpeg_path,
            "identification_backend": identification_backend,
        }
    devices = audio_backend.list_windows_audio_devices()
    return {
        "devices": devices,
        "platform": "windows",
        "capture_backend": "DirectShow",
        "capture_mode": "microphone",
        "ffmpeg_path": ffmpeg_path,
        "identification_backend": identification_backend,
    }


@app.post("/api/record/start")
def record_start(req: RecordRequest) -> dict[str, Any]:
    """Begin recording the playing output and marking what is on it."""
    from . import recorder

    active = recorder.active_sessions()
    if active:
        for sid in active:
            src = recorder.session_source(sid)
            if not req.source or req.source == src:
                dir_path = recorder.session_directory(sid)
                return {
                    "status": "recording",
                    "recording_id": sid,
                    "source": src or "",
                    "dir": str(dir_path) if dir_path else "",
                    "reused": True,
                }

    try:
        session = recorder.start(req.source or "", keep_audio=req.keep_audio, note=req.note)
    except recorder.RecorderError as exc:
        # Nothing playing, or no ffmpeg: the caller's problem to fix, not a bug.
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        log.exception("Control API failed to start recording")
        raise HTTPException(status_code=500, detail=f"Could not record: {exc}")

    return {
        "status": "recording",
        "recording_id": session.recording_id,
        "source": session.source,
        "dir": str(session.directory),
    }


@app.post("/api/record/stop")
def record_stop(req: StopRequest) -> dict[str, Any]:
    """Stop one capture, or every one this process owns."""
    from . import recorder

    if req.recording_id is None:
        stopped = recorder.active_sessions()
        recorder.stop_all()
    else:
        if not recorder.is_running(req.recording_id):
            raise HTTPException(status_code=404,
                                detail="No such recording running here")
        stopped = [req.recording_id]
        recorder.stop(req.recording_id)
    return {"status": "stopped", "stopped": stopped}


@app.get("/api/record/status")
def record_status() -> dict[str, Any]:
    """Captures running in this process.

    Only this process: a session lives in the one that started it, so a TUI's
    recording is not visible here and vice versa. ``/api/recordings`` on the
    read-only API is the view across all of them.
    """
    from . import recorder

    sessions = []
    for recording_id in recorder.active_sessions():
        marks = recorder.load_marks(recording_id)
        successful_marks = [mark for mark in marks if mark.ok]
        latest = successful_marks[-1] if successful_marks else None
        directory = recorder.session_directory(recording_id)
        sessions.append({
            "recording_id": recording_id,
            "elapsed_s": recorder.elapsed(recording_id) or 0.0,
            "source": recorder.session_source(recording_id),
            "marks": len(marks),
            "identified": len(successful_marks),
            "audio_bytes": recorder.directory_size(directory) if directory else 0,
            "level_db": recorder.audio_level(recording_id),
            "detected_artist": latest.artist if latest else None,
            "detected_title": latest.title if latest else None,
            "detected_at": latest.at_wall if latest else None,
        })
    return {"recording": sessions, "count": len(sessions)}


@app.post("/api/recordings/{recording_id}/analyse")
def record_analyse(recording_id: int, background: BackgroundTasks,
                   keep: bool = True, prune_after: bool = False) -> dict[str, Any]:
    """Decompile a recording into the database.

    Returns immediately: analysing a couple of hours takes minutes, which no
    HTTP client should be asked to hold open. Poll ``/api/recordings/{id}`` on
    the read-only API for the ``analysed`` status, or poll the returned
    ``job_id`` via ``GET /api/jobs/{job_id}`` for pending/running/done/error.
    """
    from . import recorder, recording_worker

    record = recording_worker.load_recording(recording_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if recorder.is_running(recording_id):
        raise HTTPException(status_code=409,
                            detail="Recording is still capturing; stop it first")

    # Retention is no longer a side effect of analysing: a caller asking to
    # decompile one session should not silently drop another past the age or
    # size cap. `keep` is accepted and ignored; it is now the default.
    job_id = jobs.create_job()
    background.add_task(jobs.run_job, job_id, recording_worker.analyse, recording_id,
                        prune_after=prune_after)
    return {"status": "accepted", "recording_id": recording_id, "job_id": job_id}


@app.delete("/api/recordings/{recording_id}/audio")
def record_discard(recording_id: int) -> dict[str, Any]:
    """Delete a recording's audio, keeping its markers."""
    from . import recorder, recording_worker

    if recording_worker.load_recording(recording_id) is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if recorder.is_running(recording_id):
        raise HTTPException(status_code=409,
                            detail="Recording is still capturing; stop it first")
    freed = recording_worker.discard_audio(recording_id)
    return {"status": "discarded", "recording_id": recording_id,
            "freed_bytes": freed}


@app.post("/api/radio/sessions/{session_id}/import")
def radio_session_import(session_id: int, background: BackgroundTasks,
                         save_audio: bool = True, resolve_streaming: bool = True) -> dict[str, Any]:
    """Import tracks from a radio session into the karaoke library."""
    from . import localcache, radio_pipeline
    with localcache.connect() as conn:
        session = localcache.get_radio_session(session_id, conn=conn)
        if session is None:
            raise HTTPException(status_code=404, detail="Radio session not found")

    def _bg():
        try:
            radio_pipeline.import_radio_session(session_id, save_audio=save_audio,
                                                resolve_streaming=resolve_streaming)
        except Exception as exc:
            log.error("Background radio session import failed for %s: %s", session_id, exc)

    background.add_task(_bg)
    return {"status": "accepted", "session_id": session_id}



@app.get("/api/players/window")
def get_player_window() -> dict[str, Any]:
    """Get the current Chrome kiosk window bounds and state over CDP."""
    from . import player_open
    info = player_open.get_window_bounds()
    if not info:
        raise HTTPException(status_code=503, detail="Chrome CDP window is not active (:9222 down)")
    return info


@app.post("/api/players/window")
def set_player_window(req: WindowRequest) -> dict[str, Any]:
    """Control Chrome kiosk window state (fullscreen, normal, focus, etc.) or geometry."""
    from . import player_open

    if req.state == "focus":
        ok = player_open.bring_to_front()
        return {"status": "ok" if ok else "failed", "focused": ok}

    ok = player_open.set_window_bounds(
        state=req.state,
        left=req.left,
        top=req.top,
        width=req.width,
        height=req.height,
    )
    if not ok:
        raise HTTPException(status_code=503, detail="Failed to update Chrome window state/bounds")
    return {"status": "ok", "state": req.state}


@app.post("/api/players/window/restart")
def restart_kiosk_player() -> dict[str, Any]:
    """Restart the Chrome kiosk window service or processes."""
    from .player_open import restart_kiosk_browser
    ok = restart_kiosk_browser()
    return {"status": "ok" if ok else "failed", "restarted": ok}


# Deliberately plain. This page exists to hold one <audio> element and to
# declare what is playing; anything more would be a second UI competing with
# the TUI, which is where browsing actually happens.
_TRACK_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>{artist} - {title}</title>
<style>
  body {{ background:#111; color:#eee; font:16px/1.5 sans-serif;
         display:flex; flex-direction:column; align-items:center;
         justify-content:center; height:100vh; margin:0; gap:1rem; }}
  .artist {{ opacity:.7 }}
  button {{ font-size:1.2rem; padding:.6rem 1.4rem; cursor:pointer }}
  audio {{ width:min(90vw, 40rem) }}
</style>
<div class="artist">{artist}</div>
<h1>{title}</h1>
<audio id="a" src="{src}" controls preload="auto"></audio>
<button id="go" hidden>Play</button>
<script>
  const a = document.getElementById('a'), go = document.getElementById('go');
  // Chromium republishes this over MPRIS, so playerctl reports the real track
  // and the existing detection path needs no special case for recordings.
  if ('mediaSession' in navigator) {{
    navigator.mediaSession.metadata = new MediaMetadata({{
      title: {title_js}, artist: {artist_js}, album: {album_js}
    }});
  }}
  a.play().catch(() => {{ go.hidden = false; }});
  go.addEventListener('click', () => {{ a.play(); go.hidden = true; }});
</script>
"""


@app.get("/api/recordings/{recording_id}/tracks/{index}/audio")
def record_track_audio(recording_id: int, index: int):
    """One track out of a recording, cut on demand and cached.

    Served rather than played here. The browser window already open for
    YouTube Music and Spotify is the project's only audio player, and it
    publishes MPRIS -- which is where position for lyric sync is already read
    from. Handing it a URL costs no second player and no second clock.

    Starlette's FileResponse answers Range requests, so seeking works without
    anything further.
    """
    from fastapi.responses import FileResponse

    from . import recording_audio

    path = recording_audio.track_audio(recording_id, index)
    if path is None:
        raise HTTPException(status_code=404,
                            detail="No audio for that track")
    return FileResponse(path, media_type="audio/flac",
                        filename=path.name)


@app.get("/recordings/{recording_id}/tracks/{index}")
def record_track_page(recording_id: int, index: int):
    """A page that plays one recorded track, and names it to the desktop.

    The Media Session metadata is the point. Chromium republishes it over
    MPRIS, so ``playerctl metadata`` reports this track's real artist and
    title -- which means :mod:`karaoke.detect` identifies it exactly as it
    does a stream, and lyrics follow with no special case for recordings.

    Autoplay may be refused without a user gesture, so the page tries and
    falls back to a button rather than sitting silent with no explanation.
    """
    from fastapi.responses import HTMLResponse

    from . import recording_audio

    segment = recording_audio.track_segment(recording_id, index)
    if segment is None:
        raise HTTPException(status_code=404, detail="No such track")

    raw_artist = segment.artist or "Unknown artist"
    raw_title = segment.title or "Unknown track"
    album = f"Recording {recording_id} — track {index}"
    src = f"/api/recordings/{recording_id}/tracks/{index}/audio"
    # Escaped for the context each value lands in: HTML entities in the markup,
    # JSON for the script. A track title is arbitrary text from a third-party
    # identification service, so it is never pasted into either raw.
    return HTMLResponse(_TRACK_PAGE.format(
        artist=html.escape(raw_artist), title=html.escape(raw_title),
        src=html.escape(src),
        artist_js=json.dumps(raw_artist), title_js=json.dumps(raw_title),
        album_js=json.dumps(album)))


@app.post("/api/sample")
def sample_now(req: SampleRequest) -> dict[str, Any]:
    """Detect key/BPM by recording a short excerpt of what is playing.

    Synchronous, unlike analyse: this is bounded by ``seconds`` and the caller
    is waiting on the answer. It is the API form of the TUI's `k`.
    """
    from . import sample_audio

    seconds = req.seconds or sample_audio.DEFAULT_SECONDS
    try:
        result = sample_audio.sample_and_analyse(
            req.artist or "", req.title or "", seconds, source=req.source or "")
    except sample_audio.CaptureError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except sample_audio.AnalysisUnavailable as exc:
        # The audio was captured fine; this host just cannot examine it.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log.exception("Control API sample failed")
        raise HTTPException(status_code=500, detail=f"Sample failed: {exc}")

    return {
        "status": "analysed",
        "artist": req.artist,
        "title": req.title,
        "seconds": seconds,
        "key": result.key.name if result.key else None,
        "bpm": result.bpm,
        "stored": bool(req.artist and req.title),
    }


@app.get("/api/sample/stream")
def sample_stream(
    artist: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    seconds: Optional[float] = Query(None),
    source: Optional[str] = Query(None),
):
    """Stream live sample progress and result as Server-Sent Events.

    This keeps the TUI/web UI off direct callbacks while still reporting the
    real-time capture phase. Event names: start, progress, complete, error.
    """
    import queue
    import threading

    from . import sample_audio

    sec = float(seconds or sample_audio.DEFAULT_SECONDS)
    art = artist or ""
    tit = title or ""
    events: "queue.Queue[tuple[str, dict[str, Any]] | None]" = queue.Queue()

    def worker() -> None:
        started = time.monotonic()
        last_emit = 0.0

        def progress() -> bool:
            nonlocal last_emit
            elapsed = max(0.0, time.monotonic() - started)
            if elapsed - last_emit >= 1.0 or elapsed >= sec:
                last_emit = elapsed
                events.put(("progress", {
                    "status": "capturing",
                    "elapsed_s": round(min(elapsed, sec), 2),
                    "percent": round(min(elapsed / sec, 1.0) * 100, 1),
                    "seconds": sec,
                }))
            return True

        try:
            result = sample_audio.sample_and_analyse(
                art, tit, sec, source=source or "", should_continue=progress)
            events.put(("complete", {"status": "analysed",
                                    "artist": art,
                                    "title": tit,
                                    "seconds": sec,
                                    "key": result.key.name if result.key else None,
                                    "bpm": result.bpm,
                                    "stored": bool(art and tit)}))
        except sample_audio.CaptureError as exc:
            events.put(("error", {"status": "error", "detail": str(exc)}))
        except sample_audio.AnalysisUnavailable as exc:
            events.put(("error", {"status": "unavailable", "detail": str(exc)}))
        except Exception as exc:
            log.exception("Control API streaming sample failed")
            events.put(("error", {"status": "failed", "detail": str(exc)}))
        finally:
            events.put(None)

    def body():
        events.put(("start", {
            "status": "started", "artist": art, "title": tit, "seconds": sec,
        }))
        thread = threading.Thread(target=worker, name="sample-stream", daemon=True)
        thread.start()
        while True:
            item = events.get()
            if item is None:
                break
            event, payload = item
            yield f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


# -- player controls (MPRIS via playerctl) --------------------------------
#
# These live here rather than in karaoke.api because playerctl talks to the
# desktop session's MPRIS bus, which does not exist in a container. Everything
# the TUI does to a player is exposed so a future web UI can do the same.


@app.get("/api/players")
def players_list() -> dict[str, Any]:
    """Every MPRIS player, which are playing, and which one is active."""
    from . import playerctl

    names = playerctl.list_players()
    playing = playerctl.playing_players()
    return {
        "players": names,
        "playing": playing,
        "active": playerctl.playing_player(),
        "count": len(names),
    }


@app.get("/api/players/current")
def player_current(player: str = "") -> dict[str, Any]:
    """Current track metadata + playback state for a player (or the active one)."""
    from . import playerctl

    target = player or playerctl.playing_player()
    meta = playerctl.current_metadata(target)
    return {
        "player": target,
        "status": playerctl.status(target),
        "position_s": playerctl.position(target),
        "art_url": playerctl.art_url(target),
        "metadata": None if meta is None else {
            "artist": meta.artist,
            "title": meta.title,
            "album": meta.album,
            "url": meta.url,
            "player": meta.player,
            "mpris_name": meta.mpris_name,
            "duration": meta.duration,
        },
    }


def _player_action(name: str, fn, req: PlayerControlRequest) -> dict[str, Any]:
    ok = fn((req.player or ""))
    if not ok:
        raise HTTPException(status_code=409,
                            detail=f"No player could handle '{name}'")
    return {"status": "ok", "action": name, "player": req.player or ""}


@app.post("/api/players/play-pause")
def player_play_pause(req: PlayerControlRequest) -> dict[str, Any]:
    """Toggle play/pause on the (targeted) player."""
    from . import playerctl
    return _player_action("play-pause", playerctl.play_pause, req)


@app.post("/api/players/pause")
def player_pause(req: PlayerControlRequest) -> dict[str, Any]:
    """Pause the (targeted) player (never resumes)."""
    from . import playerctl
    return _player_action("pause", playerctl.pause, req)


@app.post("/api/players/next")
def player_next(req: PlayerControlRequest) -> dict[str, Any]:
    """Skip to the next track."""
    from . import playerctl
    return _player_action("next", playerctl.next_track, req)


@app.post("/api/players/previous")
def player_previous(req: PlayerControlRequest) -> dict[str, Any]:
    """Skip to the previous track."""
    from . import playerctl
    return _player_action("previous", playerctl.previous_track, req)


@app.post("/api/players/seek")
def player_seek(req: SeekRequest) -> dict[str, Any]:
    """Seek the (targeted) player by a relative offset in seconds."""
    from . import playerctl

    if not playerctl.seek(req.offset_s, req.player or ""):
        raise HTTPException(status_code=409,
                            detail="No player could handle 'seek'")
    return {"status": "ok", "action": "seek", "offset_s": req.offset_s,
            "player": req.player or ""}


# -- library ingestion (folder scan, audio cut) ---------------------------
#
# Folder scanning needs ffmpeg + songrec + the analysis stack, and audio cut
# needs ffmpeg, none of which belong in the slim library container.


@app.post("/api/scan/folder")
def scan_folder(req: FolderScanRequest, background: BackgroundTasks) -> dict[str, Any]:
    """Scan a music folder: tags, fingerprint, classify, source, and ingest.

    A dry run returns the enrichment preview synchronously. A real ingest can
    run long over a big library, so it is dispatched to the background and the
    caller polls the library API for the new tracks.
    """
    from . import folder_scan

    root = Path(req.dir).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Directory not found: {root}")

    if req.dry_run:
        stats = folder_scan.scan_and_ingest_folder(
            root,
            use_fingerprint=req.use_fingerprint,
            classify_audio=req.classify_audio,
            resolve_streaming=req.resolve_streaming,
            dry_run=True,
            limit=req.limit,
        )
        return {"status": "preview", **stats}

    job_id = jobs.create_job()
    background.add_task(
        jobs.run_job,
        job_id,
        folder_scan.scan_and_ingest_folder,
        root,
        use_fingerprint=req.use_fingerprint,
        classify_audio=req.classify_audio,
        resolve_streaming=req.resolve_streaming,
        dry_run=False,
        limit=req.limit,
    )
    return {"status": "accepted", "dir": str(root), "job_id": job_id}


@app.post("/api/audio/cut")
def audio_cut(req: AudioCutRequest) -> dict[str, Any]:
    """Cut a slice out of an audio file with ffmpeg. Returns the output path."""
    import subprocess
    import tempfile

    src = Path(req.file_path).expanduser()
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"File not found: {src}")
    if req.duration_s <= 0:
        raise HTTPException(status_code=400, detail="duration_s must be positive")
    if not shutil.which("ffmpeg"):
        raise HTTPException(status_code=503, detail="ffmpeg is not installed")

    if req.output_path:
        out = Path(req.output_path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, tmp = tempfile.mkstemp(suffix=src.suffix or ".wav", prefix="karaoke-cut-")
        os.close(fd)
        out = Path(tmp)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-ss", f"{req.start_s:.3f}", "-t", f"{req.duration_s:.3f}",
        "-ac", "2", "-ar", "44100", str(out),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True,
                       timeout=max(120.0, req.duration_s * 4))
    except subprocess.CalledProcessError as exc:
        raise HTTPException(status_code=500,
                            detail=f"ffmpeg failed: {exc.stderr.decode(errors='ignore')[:200]}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Cut failed: {exc}")

    if not out.is_file() or out.stat().st_size == 0:
        raise HTTPException(status_code=500, detail="Cut produced no output")

    return {
        "status": "ok",
        "output_path": str(out),
        "start_s": req.start_s,
        "duration_s": req.duration_s,
        "bytes": out.stat().st_size,
    }


# Celery worker status and scaling
@app.get("/api/workers/status", response_model=WorkerStatus)
def get_workers_status() -> WorkerStatus:
    """Celery worker, queue and Flower dashboard status.

    Allows monitoring the post-processing pipeline without screen-scraping the TUI.
    """
    from . import postprocess_status as ps

    status = ps.get_status()
    return WorkerStatus(
        orchestrator=status.orchestrator,
        available=status.available,
        dashboard_url=status.dashboard_url,
        workers_active=status.workers_active,
        queue_depth=status.queue_depth,
        queue_details=QueueDetails(
            name=status.queue_details.name,
            messages=status.queue_details.messages,
            messages_ready=status.queue_details.messages_ready,
            messages_unacknowledged=status.queue_details.messages_unacknowledged,
            consumers=status.queue_details.consumers
        ),
        worker_details=[
            WorkerUnitDetails(unit=w.unit, active=w.active, running=w.running)
            for w in status.worker_details
        ],
        reason=status.reason,
    )


@app.post("/api/workers/scale")
def scale_workers(req: ScaleRequest) -> dict[str, Any]:
    """Scales the Celery worker pool (starts/stops `karaoke-celery-worker.service`)."""
    from . import postprocess_status as ps

    if req.target == 0:
        ok = ps.stop_worker()
        log.info("control: scaled Celery worker down (stopped)")
    else:
        ok = ps.start_worker()
        log.info("control: scaled Celery worker up (started)")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to scale worker")
    return {"status": "ok", "target": req.target}


@app.get("/api/logs/errors")
def get_error_logs(lines: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Get the latest error log entries from karaoke.log."""
    log_file = Path.home() / ".local/share/karaoke/logs/karaoke.log"
    if not log_file.is_file():
        return {"status": "ok", "logs": []}

    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        errs = [l.rstrip() for l in all_lines if "ERROR" in l or "CRITICAL" in l or "Traceback" in l or "Exception" in l]
        return {"status": "ok", "count": len(errs), "logs": errs[-lines:]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {exc}")


# -- stage / tv prompter view --------------------------------------------


@app.get("/stage", response_class=HTMLResponse)
@app.get("/tv", response_class=HTMLResponse)
def stage_page() -> HTMLResponse:
    """Dedicated full-screen stage view for TV/prompter displays."""
    from . import stage_view

    return HTMLResponse(stage_view.render_stage_html())


@app.get("/api/stage/stream")
async def stage_stream() -> StreamingResponse:
    """Real-time SSE stream of live playback, lyrics, rhythm, and queue for stage view."""
    from . import stage_view

    return StreamingResponse(
        stage_view.stage_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/stage/state")
def stage_state() -> dict[str, Any]:
    """Single-snapshot REST endpoint for current stage state."""
    from . import stage_view

    return stage_view.get_stage_state()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    """Poll status for a background job returned by an `accepted`-status endpoint."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


class EnqueueRequest(BaseModel):
    artist: str
    title: str
    url: Optional[str] = None


class QueueReorderRequest(BaseModel):
    from_index: int
    to_index: int


def _queue_rows() -> tuple[str, str, int, List[Dict[str, Any]]]:
    from . import localcache

    saved = localcache.load_active_queue()
    if not saved:
        return "", "", 0, []
    return (
        saved.get("playlist_id", ""),
        saved.get("name", ""),
        saved.get("current_index", 0),
        list(saved.get("rows", [])),
    )


def _queue_item_view(row: Dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "artist": row.get("artist", ""),
        "title": row.get("title", ""),
        "url": row.get("url") or None,
        "key": row.get("key") or None,
        "note": row.get("genre") or None,
    }


@app.get("/api/queue")
def get_queue() -> dict[str, Any]:
    """The mutable play queue, shared with the TUI via the active_queue_state table."""
    _, _, _, rows = _queue_rows()
    items = [_queue_item_view(row, i) for i, row in enumerate(rows)]
    return {"items": items, "count": len(items)}


@app.post("/api/queue")
def enqueue_track(req: EnqueueRequest) -> dict[str, Any]:
    """Append a track to the end of the active queue."""
    from . import localcache

    playlist_id, name, current_index, rows = _queue_rows()
    rows.append({"artist": req.artist, "title": req.title, "url": req.url or ""})
    localcache.save_active_queue(playlist_id, name, rows, current_index)
    return _queue_item_view(rows[-1], len(rows) - 1)


@app.delete("/api/queue/{index}")
def remove_from_queue(index: int) -> dict[str, Any]:
    """Remove one item from the active queue by its position."""
    from . import localcache

    playlist_id, name, current_index, rows = _queue_rows()
    if index < 0 or index >= len(rows):
        raise HTTPException(status_code=404, detail="Queue index out of range")
    rows.pop(index)
    if current_index > index:
        current_index -= 1
    localcache.save_active_queue(playlist_id, name, rows, current_index)
    return {"status": "removed", "index": index}


@app.patch("/api/queue/reorder")
def reorder_queue(req: QueueReorderRequest) -> dict[str, Any]:
    """Move one queued item from one position to another."""
    from . import localcache

    playlist_id, name, current_index, rows = _queue_rows()
    if not (0 <= req.from_index < len(rows)) or not (0 <= req.to_index < len(rows)):
        raise HTTPException(status_code=404, detail="Queue index out of range")
    item = rows.pop(req.from_index)
    rows.insert(req.to_index, item)
    localcache.save_active_queue(playlist_id, name, rows, current_index)
    return {"items": [_queue_item_view(row, i) for i, row in enumerate(rows)], "count": len(rows)}


def main() -> None:
    """Entrypoint for the host-side control API.

    Binds loopback only by default, since this endpoint spawns local processes.
    """
    import uvicorn

    uvicorn.run(
        "karaoke.ctrl_api:app",
        host=os.environ.get("KARAOKE_CTRL_HOST", "127.0.0.1"),
        port=int(os.environ.get("KARAOKE_CTRL_PORT", "8765")),
        reload=bool(os.environ.get("KARAOKE_CTRL_RELOAD")),
    )


if __name__ == "__main__":
    main()
