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
    analyze, clap_vector, genre, key_progression, localcache,
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
    force: bool = False,
) -> dict[str, Any]:
    """Scan a directory of audio files, enrich with fingerprinting, audio analysis,
    Spotify/YouTube links, and ingest into SQLite + OpenSearch.

    ``only_paths`` restricts processing to that explicit set of absolute file
    paths (still discovered under ``music_dir``). This powers a resumable retry
    pass that re-ingests only the files a previous run skipped, without
    re-touching everything already in the database.

    By default a file whose track already has every artefact -- a CLAP vector
    and a harmonic progression -- is skipped untouched. Re-running otherwise
    costs a decode and an analysis per file, and overwrites a vector that may
    have been made from a better copy of the audio than the one on disk now.
    Pass ``force=True`` to redo them anyway, which is what you want after
    changing how a vector is computed.
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
        "embedded": 0,
        "progressions": 0,
        "skipped": 0,
        "errors": 0,
        "items": [],
    }

    labels = genre.label_vectors() if (classify_audio and clap_vector.available()) else {}

    # One query per index rather than one per file: the check has to be cheap
    # or it costs more than the work it avoids.
    done_clap: set[int] = set()
    done_prog: set[int] = set()
    if not force and not dry_run:
        try:
            from .osclient import client as get_os_client

            os_client = get_os_client()
            if os_client is not None:
                for index_name, bucket in ((clap_vector.CLAP_INDEX, done_clap),
                                           (key_progression.PROGRESSION_INDEX, done_prog)):
                    try:
                        res = os_client.search(index=index_name, body={
                            "size": 10000, "_source": ["track_id"],
                            "query": {"match_all": {}},
                        })
                        bucket.update(int(h["_source"]["track_id"])
                                      for h in res["hits"]["hits"])
                    except Exception:
                        pass
        except Exception:
            log.debug("could not read existing vectors; scanning everything")

    for index, path in enumerate(audio_files, start=1):
        emit("item_start", index=index, total=len(audio_files), path=str(path), name=path.name)
        log.info("Folder scan %s/%s: %s", index, len(audio_files), path)
        try:
            # Already fully processed? Skip before the decode, which is the
            # expensive part -- not after it.
            if not force and c is not None:
                known = localcache.find_track_by_url(str(path), c)
                if known and known[0] in done_clap and known[0] in done_prog:
                    stats["skipped"] += 1
                    emit("skip", index=index, total=len(audio_files), path=str(path),
                         name=path.name, reason="already analysed")
                    continue
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
                            WHERE s.url LIKE %s
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
                        "SELECT recording_id FROM recordings WHERE dir LIKE %s OR dir = %s",
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

            # Save the CLAP vector, not just the word read off it. Embedding
            # is the whole cost of this step; indexing is one small write.
            # Dropping it here is why a full library scan produced thousands
            # of genre labels and almost no searchable vectors -- and the
            # audio it came from is often gone by the time that is noticed.
            if clap_vec:
                key_obj = getattr(analysis_res, "key", None)
                if clap_vector.store(
                    track_id, clap_vec,
                    artist=artist or "", title=title or "", album=album or "",
                    detected_key=getattr(key_obj, "name", "") or "",
                    bpm=getattr(analysis_res, "bpm", None),
                ):
                    stats["embedded"] += 1

            # Harmonic shape: how the key moves across the track, rather than
            # the single label a whole-file estimate collapses it to. Key
            # detection runs at about 0.002x realtime, so next to the decode
            # already paid for above this is close to free.
            if classify_audio:
                try:
                    prog = key_progression.analyse(str(path))
                    if prog and key_progression.store(track_id, prog,
                                                      artist=artist or "",
                                                      title=title or ""):
                        stats["progressions"] += 1
                except Exception:
                    log.debug("progression analysis failed for %s", path, exc_info=True)

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
