"""FastAPI library backend for the karaoke song library.

This service is deployment-safe: it only reads the SQLite library database and
the application log file. It deliberately contains **no** desktop-bound
behaviour (``xdg-open``/``playerctl``), so it can run inside a container where
no display or media player exists.

Playback control lives in :mod:`karaoke.ctrl_api`, which runs on the host
alongside the user's desktop session.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import localcache
from .logger import LOG_FILE
from .staging_api import router as staging_router

API_VERSION = "0.3.0"

app = FastAPI(
    title="Karaoke Library API",
    description=(
        "Read-only REST API over the karaoke SQLite library: tracks, lyrics, "
        "stats and logs. Playback is handled by the host-side control API."
    ),
    version=API_VERSION,
)

# Allow the Angular web dashboard (karaoke/web) to call this API from a different
# origin/port in dev and production. Configurable since this service binds 0.0.0.0.
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

app.include_router(staging_router)


class TrackResponse(BaseModel):
    track_id: int
    artist: str
    title: str
    album: Optional[str] = None
    duration: Optional[float] = None
    url: Optional[str] = None
    kind: Optional[str] = None
    key: Optional[str] = None
    bpm: Optional[float] = None
    energy: Optional[float] = None
    brightness: Optional[float] = None
    genre: Optional[str] = None
    play_count: int = 0
    has_synced_lyrics: bool = False


class UpdateRecordingRequest(BaseModel):
    note: Optional[str] = None
    keep_audio: Optional[bool] = None


class LyricsLookupRequest(BaseModel):
    artist: str
    title: str
    album: Optional[str] = None
    duration: Optional[float] = None


class QueueSuggestRequest(BaseModel):
    track_ids: list[int]
    limit: int = 10
    per_artist: int = 2


class SuggestionResponse(BaseModel):
    track_id: int
    artist: str
    title: str
    score: float
    seeds_matched: int
    space: str
    url: Optional[str] = None


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness/readiness probe.

    Reports whether the configured SQLite database is reachable so that a
    misconfigured volume mount surfaces as an unhealthy pod rather than as
    empty track listings.
    """
    db_ok = True
    detail = None
    try:
        with localcache.connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        db_ok = False
        detail = str(exc)

    return {
        "status": "ok" if db_ok else "degraded",
        "version": API_VERSION,
        "database": "ok" if db_ok else "unavailable",
        "detail": detail,
    }


@app.get("/api/tracks", response_model=list[TrackResponse])
def list_tracks(
    q: Optional[str] = Query(None, description="Search query for artist or title"),
    genre: Optional[str] = Query(None, description="Filter by genre"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """List tracks in the local library with optional search and genre filtering."""
    with localcache.connect() as conn:
        from .track_analysis import ensure_schema
        ensure_schema(conn)
        cur = conn.cursor()
        base = """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration, t.play_count, s.url, s.kind,
                   a.detected_key AS key, a.bpm, a.energy, a.brightness, g.genre,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
        """
        where_clauses = []
        params: list[Any] = []
        if q:
            pattern = f"%{q.strip()}%"
            where_clauses.append("(t.artist ILIKE %s OR t.title ILIKE %s OR t.album ILIKE %s)")
            params.extend([pattern, pattern, pattern])
        if genre and genre.strip().lower() != "all":
            where_clauses.append("LOWER(TRIM(g.genre)) = LOWER(TRIM(%s))")
            params.append(genre.strip())

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = (
            base
            + f"""
            {where_sql}
                        ORDER BY t.artist, t.title
            LIMIT %s OFFSET %s
            """
        )
        params.extend([limit, offset])
        cur.execute(sql, params)
        return [
            {
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "album": row["album"],
                "duration": row["duration"],
                "url": row["url"],
                "kind": row["kind"],
                "key": row["key"] or "",
                "bpm": row["bpm"],
                "energy": row["energy"],
                "brightness": row["brightness"],
                "genre": row["genre"] or "",
                "play_count": row["play_count"] or 0,
                "has_synced_lyrics": bool(row["has_synced"]),
            }
            for row in cur.fetchall()
        ]


@app.post("/api/lyrics/lookup")
def lookup_lyrics(req: LyricsLookupRequest) -> dict[str, Any]:
    """Fetch lyrics by artist/title and add a successful result to the Songbook."""
    from . import lyrics as lyrics_client

    artist = req.artist.strip()
    title = req.title.strip()
    if not artist or not title:
        raise HTTPException(status_code=400, detail="Artist and title are required")

    with localcache.connect() as conn:
        result = localcache.get_cached_lyrics(artist, title, conn=conn)
        if result is None:
            result = lyrics_client.fetch_lrclib(
                artist, title, req.album, req.duration, timeout=10.0)
            if not (result.plain or result.synced_raw):
                raise HTTPException(status_code=404, detail="No lyrics found on LRCLIB")
            localcache.put_cached_lyrics(
                artist,
                title,
                result,
                album=req.album or "",
                duration=req.duration,
                conn=conn,
            )
        track_id = localcache.find_track_id(artist, title, conn)

    if track_id is None:
        raise HTTPException(status_code=500, detail="Lyrics were found but could not be cached")
    return get_track(track_id)


@app.get("/api/tracks/{track_id}")
def get_track(track_id: int) -> dict[str, Any]:
    """Get detailed information for a track including lyrics and sources."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT track_id, artist, title, album, duration FROM tracks WHERE track_id = %s",
            (track_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Track not found")

        cur.execute(
            "SELECT url, kind, player_name FROM sources WHERE track_id = %s",
            (track_id,),
        )
        sources = [dict(s) for s in cur.fetchall()]

        lyrics = localcache.get_lyrics_by_track_id(track_id, conn)
        lyrics_data = None
        if lyrics:
            lyrics_data = {
                "plain": lyrics.plain,
                "synced_raw": lyrics.synced_raw,
                "source": lyrics.source,
                "has_synced": lyrics.has_synced,
                "line_count": len(lyrics.lines),
            }

        return {
            "track_id": row["track_id"],
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
            "duration": row["duration"],
            "sources": sources,
            "lyrics": lyrics_data,
        }


@app.get("/api/tracks/{track_id}/analysis/history")
def get_track_analysis_history(track_id: int) -> dict[str, Any]:
    """Get historical analysis versions (scans, sample events, multi-source checks) for a track."""
    from . import track_analysis

    with localcache.connect() as conn:
        history = track_analysis.get_analysis_history(track_id, conn)
        return {"track_id": track_id, "history": history, "count": len(history)}


@app.get("/api/stats")
def get_stats(
    limit: int = Query(10, ge=1, le=100),
    days: Optional[float] = Query(None, description="Only count events in the last N days"),
) -> dict[str, Any]:
    """Return local cache summary statistics, with optional top-N and time window."""
    import time as _time
    since = _time.time() - days * 86400 if days else None
    summary = localcache.summarize(limit=limit, since=since)
    return {
        "total_events": summary.total_events,
        "plays": summary.plays,
        "discoveries": summary.discoveries,
        "cache_hits": summary.cache_hits,
        "cache_misses": summary.cache_misses,
        "cache_hit_rate": summary.cache_hit_rate,
        "distinct_tracks": summary.distinct_tracks,
        "distinct_artists": summary.distinct_artists,
        "top_tracks": summary.top_tracks,
        "top_artists": summary.top_artists,
        "by_mode": summary.by_mode,
    }


@app.get("/api/recordings")
def list_recordings(
    status: Optional[str] = Query(None, description="Filter by status (e.g. recording, complete, analysed, discarded). Comma-separated allowed."),
    source: Optional[str] = Query(None, description="Filter by audio source substring"),
    has_marks: Optional[bool] = Query(None, description="Filter to recordings with (true) or without (false) marks"),
    identified_only: Optional[bool] = Query(None, description="Filter to recordings with at least 1 identified track"),
    keep_audio: Optional[bool] = Query(None, description="Filter by keep_audio flag"),
    since: Optional[float] = Query(None, description="Filter recordings started at or after Unix timestamp"),
    until: Optional[float] = Query(None, description="Filter recordings started at or before Unix timestamp"),
    q: Optional[str] = Query(None, description="Search query matching note or source"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Record-mode sessions with filtering, search and pagination, newest first.

    Read-only over SQLite like the rest of this service. Starting and stopping
    a capture needs PipeWire and a desktop audio session, so that lives in the
    control API instead.
    """
    from . import recorder

    with localcache.connect() as conn:
        clauses = []
        params: list[Any] = []

        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if len(statuses) == 1:
                clauses.append("r.status = %s")
                params.append(statuses[0])
            elif len(statuses) > 1:
                placeholders = ",".join(["%s"] * len(statuses))
                clauses.append(f"r.status IN ({placeholders})")
                params.extend(statuses)

        if source:
            clauses.append("r.source ILIKE %s")
            params.append(f"%{source.strip()}%")

        if keep_audio is not None:
            clauses.append("r.keep_audio = %s")
            params.append(1 if keep_audio else 0)

        if since is not None:
            clauses.append("r.started_at >= %s")
            params.append(since)

        if until is not None:
            clauses.append("r.started_at <= %s")
            params.append(until)

        if q:
            pattern = f"%{q.strip()}%"
            clauses.append("(r.note ILIKE %s OR r.source ILIKE %s)")
            params.extend([pattern, pattern])

        if has_marks is True:
            clauses.append("(SELECT count(*) FROM recording_marks m WHERE m.recording_id = r.recording_id) > 0")
        elif has_marks is False:
            clauses.append("(SELECT count(*) FROM recording_marks m WHERE m.recording_id = r.recording_id) = 0")

        if identified_only is True:
            clauses.append("(SELECT COALESCE(sum(m.ok), 0) FROM recording_marks m WHERE m.recording_id = r.recording_id) > 0")
        elif identified_only is False:
            clauses.append("(SELECT COALESCE(sum(m.ok), 0) FROM recording_marks m WHERE m.recording_id = r.recording_id) = 0")

        where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""
            SELECT r.recording_id, r.started_at, r.ended_at, r.status,
                   r.source, r.dir, r.keep_audio, r.note,
                   (SELECT count(*) FROM recording_marks m
                     WHERE m.recording_id = r.recording_id) AS marks,
                   (SELECT COALESCE(sum(m.ok), 0) FROM recording_marks m
                     WHERE m.recording_id = r.recording_id) AS identified
             FROM recordings r
             {where_sql}
             ORDER BY r.recording_id DESC
             LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()

    out = []
    for row in rows:
        directory = Path(row["dir"])
        size = (sum(f.stat().st_size for f in directory.glob("seg-*.flac")
                    if f.is_file()) if directory.is_dir() else 0)
        out.append({
            "recording_id": row["recording_id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "status": row["status"],
            "source": row["source"],
            "marks": row["marks"],
            "identified": row["identified"],
            "audio_bytes": size,
            "keep_audio": bool(row["keep_audio"]),
            "note": row["note"],
            # Whether a capture is live in *this* process. A row can read
            # 'recording' after a crash, so the flag and the status disagree
            # on purpose rather than one being derived from the other.
            "running": recorder.is_running(int(row["recording_id"])),
        })
    return {"recordings": out, "count": len(out)}


@app.get("/api/recordings/{recording_id}")
def get_recording(
    recording_id: int,
    confident_only: bool = Query(False, description="Include only confident identified tracks"),
    min_marks: Optional[int] = Query(None, ge=1, description="Minimum mark count for track"),
) -> dict[str, Any]:
    """One session with the track list its markers resolve to.

    The segments are derived on read rather than stored: they are a function of
    the markers, and recomputing keeps the two from drifting apart.
    """
    from . import recorder, recording_worker
    from .recording_slice import is_confident, segments

    record = recording_worker.load_recording(recording_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    marks = recorder.load_marks(recording_id)
    files = recording_worker.segment_files(Path(record["dir"]))
    span = recording_worker.recording_span(files)

    tracks = []
    for segment in segments(marks):
        confident = is_confident(segment)
        if confident_only and not confident:
            continue
        if min_marks is not None and segment.marks < min_marks:
            continue
        window = recording_worker.clamp(segment, span) if span else None
        tracks.append({
            "artist": segment.artist,
            "title": segment.title,
            "start_wall": segment.start_wall,
            "end_wall": segment.end_wall,
            "duration_s": segment.duration,
            "marks": segment.marks,
            # How far the per-marker start estimates disagree. Low means the
            # boundary is corroborated; high means the track was changed,
            # repeated or seeked, and it is gated out of analysis.
            "spread_s": None if segment.spread == float("inf") else segment.spread,
            "confident": confident,
            "audio_available": window is not None,
        })

    ok, total = recorder.mark_count(recording_id)
    return {
        "recording_id": recording_id,
        "status": record["status"],
        "started_at": record["started_at"],
        "ended_at": record["ended_at"],
        "source": record["source"],
        "keep_audio": bool(record["keep_audio"]),
        "note": record["note"],
        "marks": total,
        "identified": ok,
        "segment_files": len(files),
        "captured_s": (span[1] - span[0]) if span else 0.0,
        "running": recorder.is_running(recording_id),
        "tracks": tracks,
    }


@app.patch("/api/recordings/{recording_id}")
def update_recording(recording_id: int, req: UpdateRecordingRequest) -> dict[str, Any]:
    """Update metadata (note, keep_audio) for a recording session."""
    from . import recording_worker

    record = recording_worker.load_recording(recording_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Recording not found")

    updates = []
    params: list[Any] = []
    if req.note is not None:
        updates.append("note = %s")
        params.append(req.note)
    if req.keep_audio is not None:
        updates.append("keep_audio = %s")
        params.append(1 if req.keep_audio else 0)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    params.append(recording_id)
    with localcache.connect() as conn:
        conn.execute(
            f"UPDATE recordings SET {', '.join(updates)} WHERE recording_id = %s",
            params,
        )
        conn.commit()

    updated = recording_worker.load_recording(recording_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    return {
        "recording_id": recording_id,
        "note": updated["note"],
        "keep_audio": bool(updated["keep_audio"]),
        "status": updated["status"],
    }


@app.get("/api/radio/sessions")
def get_radio_sessions(limit: int = 50) -> dict[str, Any]:
    """List radio listening sessions with metadata and recording details."""
    with localcache.connect() as conn:
        sessions = localcache.get_radio_sessions(limit=limit, conn=conn)
        return {"sessions": sessions, "count": len(sessions)}


@app.get("/api/radio/sessions/{session_id}")
def get_radio_session_detail(session_id: int) -> dict[str, Any]:
    """Get metadata and discovered tracks for a specific radio session."""
    with localcache.connect() as conn:
        session = localcache.get_radio_session(session_id, conn=conn)
        if session is None:
            raise HTTPException(status_code=404, detail="Radio session not found")
        tracks = localcache.get_radio_session_tracks(session_id, conn=conn)
        return {"session": session, "tracks": tracks, "count": len(tracks)}


@app.get("/api/workers")
def get_workers() -> dict[str, Any]:
    """Post-processing worker and queue statistics.

    Best-effort by design: if RabbitMQ is unreachable, or the workers run on a
    different host (inside a container ``/proc`` shows none of them), this
    reports ``available: false`` with a reason rather than failing. The rest of
    the API stays read-only over SQLite, so a broker outage cannot take the
    library endpoints down with it.

    CPU and memory are summed across every worker, since they scale
    horizontally and a single worker's usage would understate the load.
    """
    from .postprocess_status import QUEUE_NAME, get_status

    st = get_status(sample_cpu=True)
    return {
        "available": st.available,
        "reason": st.reason,
        "queue": {
            "name": QUEUE_NAME,
            "ready": st.ready,
            "unacked": st.unacked,
            "queued": st.queued,
            "consumers": st.consumers,
            "deliver_rate": round(st.deliver_rate, 3),
            "publish_rate": round(st.publish_rate, 3),
            "busy": st.busy,
        },
        "workers": {
            "count": st.workers,
            "running": st.worker_running,
            "pids": list(st.worker_pids),
            "cpu_percent": (round(st.worker_cpu, 1)
                            if st.worker_cpu is not None else None),
            "rss_mb": (round(st.worker_rss_mb, 1)
                       if st.worker_rss_mb is not None else None),
        },
    }


@app.get("/api/logs")
def get_logs(lines: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Get the most recent log lines from the application log file."""
    if not LOG_FILE.exists():
        return {"file": str(LOG_FILE), "lines": []}
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"file": str(LOG_FILE), "lines": content[-lines:]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {exc}")


@app.post("/api/queue/suggest", response_model=list[SuggestionResponse])
def suggest_queue(req: QueueSuggestRequest) -> list[dict[str, Any]]:
    """Suggest tracks that keep the vibe of a whole queue going.

    Seeds on every track in ``track_ids`` (by audio similarity, CLAP with a
    per-seed spectral fallback), pools their neighbours, and returns tracks
    that fit the set. Needs the OpenSearch audio/CLAP indexes; returns an empty
    list when no seed has a usable vector.
    """
    from . import queue_suggest

    suggestions = queue_suggest.suggest_for_queue(
        req.track_ids, limit=req.limit, per_artist=req.per_artist)
    if not suggestions:
        return []
    with localcache.connect() as conn:
        out: list[dict[str, Any]] = []
        for s in suggestions:
            out.append({
                "track_id": s.track_id,
                "artist": s.artist,
                "title": s.title,
                "score": s.score,
                "seeds_matched": s.seeds_matched,
                "space": s.space,
                "url": queue_suggest.playable_url(s.track_id, conn),
            })
    return out


@app.get("/api/tracks/{track_id}/sounds-like", response_model=list[SuggestionResponse])
def sounds_like_track(track_id: int, limit: int = Query(10, ge=1, le=50),
                      per_artist: int = Query(2, ge=1, le=10)) -> list[dict[str, Any]]:
    """Return tracks that acoustically sound like the given track via CLAP similarity."""
    from . import queue_suggest

    suggestions = queue_suggest.suggest_for_track(track_id, limit=limit, per_artist=per_artist)
    if not suggestions:
        return []
    with localcache.connect() as conn:
        out: list[dict[str, Any]] = []
        for s in suggestions:
            out.append({
                "track_id": s.track_id,
                "artist": s.artist,
                "title": s.title,
                "score": s.score,
                "seeds_matched": s.seeds_matched,
                "space": s.space,
                "url": queue_suggest.playable_url(s.track_id, conn),
            })
    return out


class FindSourceRequest(BaseModel):
    track_id: int


@app.post("/api/sources/find")
def api_find_sources(req: FindSourceRequest) -> dict[str, Any]:
    """Find and save a playable YouTube source for a specific track."""
    with localcache.connect() as conn:
        from .find_sources import resolve_and_save_source
        url = resolve_and_save_source(req.track_id, conn)
        if url:
            return {"status": "success", "url": url}
        raise HTTPException(status_code=404, detail="No matching source found on YouTube")


def main() -> None:
    """Entrypoint for starting the library API server.

    Binds 0.0.0.0 by default so the process is reachable inside a container;
    override with ``KARAOKE_API_HOST``/``KARAOKE_API_PORT``.
    """
    import uvicorn

    uvicorn.run(
        "karaoke.api:app",
        host=os.environ.get("KARAOKE_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("KARAOKE_API_PORT", "8000")),
        reload=bool(os.environ.get("KARAOKE_API_RELOAD")),
    )


if __name__ == "__main__":
    main()
