"""Folder Ingestion & Audio Enrichment Engine.

Scans local directories, extracts tags + Shazam/songrec fingerprints, calculates
Key/BPM/CLAP vectors, resolves YouTube & Spotify links, and ingests into SQLite + OpenSearch.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

ProgressCallback = Callable[[str, dict[str, Any]], None]

from . import (
    analyze, clap_vector, genre, localcache,
    lyrics, source_select, tags, youtube
)
from .identify import identify_file_fingerprint
from .logger import log


def scan_and_ingest_folder(
    music_dir: str | Path,
    *,
    use_fingerprint: bool = True,
    classify_audio: bool = True,
    resolve_streaming: bool = True,
    dry_run: bool = False,
    limit: Optional[int] = None,
    only_paths: Optional[set[str]] = None,
    conn: Optional[Any] = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Scan a directory of audio files, enrich with fingerprinting, audio analysis,
    Spotify/YouTube links, and ingest into SQLite + OpenSearch.

    ``only_paths`` restricts processing to that explicit set of absolute file
    paths (still discovered under ``music_dir``). This powers a resumable retry
    pass that re-ingests only the files a previous run skipped, without
    re-touching everything already in the database.
    """
    def emit(event: str, **payload: Any) -> None:
        payload.setdefault("event", event)
        try:
            progress and progress(event, payload)
        except Exception:
            log.debug("folder-scan progress callback failed", exc_info=True)

    root = Path(music_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}")

    audio_files = [p for p in sorted(root.rglob("*")) if p.is_file() and tags.is_audio(p)]
    if only_paths is not None:
        wanted = {str(Path(p)) for p in only_paths}
        audio_files = [p for p in audio_files if str(p) in wanted]
    if limit:
        audio_files = audio_files[:limit]
    emit("found", root=str(root), total=len(audio_files), dry_run=dry_run)
    log.info("Folder scan found %s audio file(s) under %s", len(audio_files), root)

    own_conn = conn is None
    c = conn or (None if dry_run else localcache.connect())

    stats: dict[str, Any] = {
        "seen": len(audio_files),
        "processed": 0,
        "fingerprinted": 0,
        "sourced": 0,
        "classified": 0,
        "errors": 0,
        "items": [],
    }

    labels = genre.label_vectors() if (classify_audio and clap_vector.available()) else {}

    for index, path in enumerate(audio_files, start=1):
        emit("item_start", index=index, total=len(audio_files), path=str(path), name=path.name)
        log.info("Folder scan %s/%s: %s", index, len(audio_files), path)
        try:
            # 1. Tags, YouTube ID lookup, Shazam Fingerprint, and Recording Markers
            t = tags.extract_tags(path)
            artist, title, album = t.artist, t.title, t.album
            duration = t.duration
            emit("tagged", index=index, total=len(audio_files), path=str(path), name=path.name,
                 artist=artist, title=title, album=album, duration=duration)

            # 1a. Fallback: YouTube Video ID lookup (e.g. -1jPUB7gRyg.webm in cache)
            if not artist or not title or artist.lower() in ("unknown", "track"):
                vid = localcache.extract_youtube_id(path.name) or localcache.extract_youtube_id(str(path))
                if vid:
                    with (localcache.connect()) as conn_check:
                        row = conn_check.execute(
                            """
                            SELECT t.artist, t.title, t.album, t.duration
                            FROM tracks t
                            JOIN sources s ON s.track_id = t.track_id
                            WHERE s.url LIKE ?
                            LIMIT 1
                            """,
                            (f"%{vid}%",),
                        ).fetchone()
                        if row:
                            artist, title = row[0], row[1]
                            album = row[2] or album
                            duration = duration or row[3]

            # 1b. Fallback: Recording Session markers (e.g. seg-*.flac in recordings/)
            if (not artist or not title or artist.lower() in ("unknown", "track")) and "recordings" in str(path):
                rec_dir = path.parent
                with localcache.connect() as conn_check:
                    rec_row = conn_check.execute(
                        "SELECT recording_id FROM recordings WHERE dir LIKE ? OR dir = ?",
                        (f"%{rec_dir.name}%", str(rec_dir)),
                    ).fetchone()
                    if rec_row:
                        rec_id = rec_row[0]
                        from . import recorder
                        from .recording_slice import segments
                        marks = recorder.load_marks(rec_id)
                        segs = segments(marks)
                        if segs:
                            artist, title = segs[0].artist, segs[0].title

            # 1c. Fallback: Shazam/songrec Fingerprint
            if use_fingerprint and (not artist or not title or artist.lower() in ("unknown", "track")):
                fp = identify_file_fingerprint(path)
                if fp and fp.artist and fp.title:
                    artist, title = fp.artist, fp.title
                    album = fp.album or album
                    stats["fingerprinted"] += 1

            # 1d. Fallback: Filename parser for "Artist - Title.ext" pattern
            if not artist or artist.lower() in ("unknown", "track") or title == path.stem:
                import re
                stem = path.stem
                parts = re.split(r'\s+-\s+', stem)
                if len(parts) == 1 and "-" in stem:
                    parts = re.split(r'-', stem)
                
                parts = [p.strip() for p in parts if p.strip()]
                if len(parts) >= 2:
                    fn_artist = parts[0]
                    fn_title = " - ".join(parts[1:])
                    # Clean track number prefixes (e.g. "07 ", "(01) ", "01. ", "01 - ")
                    fn_artist_clean = re.sub(r'^(?:\d+|\(\d+\))[\s._-]*', '', fn_artist).strip()
                    if fn_artist_clean:
                        fn_artist = fn_artist_clean
                    
                    if fn_artist and fn_title:
                        if not artist or artist.lower() in ("unknown", "track"):
                            artist = fn_artist
                        if title == path.stem:
                            title = fn_title

            if not artist or not title:
                log.warning("Skipping %s: missing artist/title after tags & fingerprint", path.name)
                emit("skip", index=index, total=len(audio_files), path=str(path), name=path.name,
                     reason="missing artist/title after tags & fingerprint")
                stats["errors"] += 1
                continue

            # 2. Audio Analysis (Key/BPM/Energy/Brightness)
            analysis_res = None
            if classify_audio:
                emit("analysis_start", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title)
                analysis_res = analyze.analyze_audio(str(path))
                if analysis_res and (analysis_res.key or analysis_res.bpm):
                    stats["classified"] += 1
                emit("analysis_done", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title,
                     key=getattr(getattr(analysis_res, "key", None), "name", None),
                     bpm=getattr(analysis_res, "bpm", None))

            # 3. CLAP Vector & Zero-Shot Genre
            clap_vec = None
            genre_verdict = None
            if classify_audio and clap_vector.available():
                emit("clap_start", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title)
                clap_vec = clap_vector.embed_audio(str(path))
                if clap_vec and labels:
                    genre_verdict = genre.classify(clap_vec, labels)
                emit("clap_done", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title,
                     genre=getattr(genre_verdict, "genre", None))

            # 4. Streaming platform resolution (Spotify & YT Music)
            yt_url = None
            spotify_uri = None
            if resolve_streaming:
                emit("source_start", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title)
                vid = localcache.extract_youtube_id(path.name) or localcache.extract_youtube_id(str(path))
                if vid:
                    yt_url = f"https://www.youtube.com/watch?v={vid}"
                else:
                    try:
                        yt_candidates = youtube.search(f"{artist} {title}", limit=5)
                        best_yt = source_select.select_best_source(
                            yt_candidates, artist, title, reference_duration=duration)
                        if best_yt:
                            yt_url = best_yt.get("url")
                    except Exception:
                        pass

                try:
                    from .spotify_client import SpotifyClient
                    spotify_uri = SpotifyClient().search_track(artist, title)
                except Exception:
                    pass

                if yt_url or spotify_uri:
                    stats["sourced"] += 1
                emit("source_done", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title, yt_url=yt_url, spotify_uri=spotify_uri)

            item_summary = {
                "path": str(path),
                "artist": artist,
                "title": title,
                "album": album,
                "duration": duration,
                "key": getattr(getattr(analysis_res, "key", None), "name", None),
                "bpm": getattr(analysis_res, "bpm", None),
                "genre": getattr(genre_verdict, "genre", None),
                "yt_url": yt_url,
                "spotify_uri": spotify_uri,
            }
            stats["items"].append(item_summary)

            if dry_run:
                stats["processed"] += 1
                emit("item_done", index=index, total=len(audio_files), path=str(path), name=path.name,
                     artist=artist, title=title, processed=stats["processed"], dry_run=True)
                continue

            # 5. SQLite Ingestion
            emit("ingest_start", index=index, total=len(audio_files), path=str(path), name=path.name,
                 artist=artist, title=title)
            assert c is not None
            ly = lyrics.fetch_lrclib(artist, title, album, duration)
            # Register the local file as the primary source and get the track id.
            track_id = localcache.add_track_source(
                artist, title, album=album, duration=duration,
                url=str(path), kind="local", conn=c)

            # Attach lyrics (LRCLIB) to that track, or log a gap for backfill.
            localcache.add_track_and_lyrics(
                artist, title, ly, album=album, duration=duration, conn=c)

            # Additional streaming sources.
            if yt_url:
                localcache.add_track_source(
                    artist, title, album=album, duration=duration,
                    url=yt_url, kind="youtube", conn=c)
            if spotify_uri:
                localcache.add_track_source(
                    artist, title, album=album, duration=duration,
                    url=spotify_uri, kind="spotify", conn=c)

            # Save Analysis
            if analysis_res and (analysis_res.key or analysis_res.bpm):
                from .track_analysis import save_detected
                save_detected(
                    track_id,
                    detected_key=analysis_res.key,
                    key_confidence=analysis_res.key_confidence,
                    key_agreement=analysis_res.key_agreement,
                    bpm=analysis_res.bpm,
                    method=f"{analysis_res.method}+folder_scan",
                    energy=analysis_res.energy,
                    brightness=analysis_res.brightness,
                    source_kind=(
                        "recording" if "recordings" in str(path)
                        else "youtube_cache" if localcache.extract_youtube_id(path.name)
                        else "local"
                    ),
                    conn=c,
                )

            # If key/BPM analysis or lyric timing is missing, dispatch to background workers
            if not (analysis_res and analysis_res.key and analysis_res.bpm):
                try:
                    from .postprocess_queue import enqueue_if_needed
                    enqueue_if_needed(artist, title, yt_url or str(path), conn=c)
                except Exception:
                    log.debug("folder_scan: postprocess enqueue failed for %s - %s", artist, title)

            # Save Genre
            if genre_verdict:
                localcache.record_genre(track_id, genre_verdict, c)

            stats["processed"] += 1
            emit("item_done", index=index, total=len(audio_files), path=str(path), name=path.name,
                 artist=artist, title=title, track_id=track_id, processed=stats["processed"],
                 errors=stats["errors"])

        except Exception as exc:
            log.exception("Error processing %s", path)
            emit("error", index=index, total=len(audio_files), path=str(path), name=path.name,
                 error=str(exc))
            stats["errors"] += 1

    if own_conn and c:
        c.close()

    emit("done", root=str(root), **stats)
    log.info(
        "Folder scan done for %s: seen=%s processed=%s classified=%s sourced=%s errors=%s",
        root, stats["seen"], stats["processed"], stats["classified"], stats["sourced"], stats["errors"],
    )
    return stats
