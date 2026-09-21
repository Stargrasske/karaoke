"""Unified YouTube audio cache ingestion, analysis, and vector indexing.

Processes downloaded YouTube audio files from `<data_dir>/youtube/`:
1. Discovers and ingests new cache tracks & sources into SQLite.
2. Runs audio analysis (Key/BPM/Energy/Brightness) on tracks missing it.
3. Generates CLAP audio embeddings and indexes them into OpenSearch `karaoke-clap`.
4. Predicts zero-shot genres using CLAP and records them in SQLite and OpenSearch.
5. Enqueues tracks needing word-level timing upgrades to the background Celery queue.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
import psycopg
from psycopg import Connection, Cursor

from . import analyze, clap_vector, genre, localcache, track_analysis
from .config import settings
from .logger import log
from .musictheory import parse_key
from .youtube import resolve_youtube

MEDIA_SUFFIXES = (".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg", ".mp3", ".flac")


@dataclass
class CacheIngestStats:
    """Summary of operations performed during a cache ingestion pass."""

    total_files: int = 0
    new_tracks: int = 0
    analyzed: int = 0
    clap_embedded: int = 0
    genres_labeled: int = 0
    enqueued: int = 0
    skipped: int = 0
    errors: int = 0


def process_youtube_cache(
    *,
    yt_dir: Optional[Path] = None,
    conn: Optional[Connection] = None,
    os_client: Any = None,
    embed_clap: bool = True,
    classify_genres: bool = True,
    analyze_audio_features: bool = True,
    enqueue_postprocessing: bool = True,
    progress_callback: Optional[Callable[[str, int, int, str], None]] = None,
) -> CacheIngestStats:
    """End-to-end ingestion and enrichment pipeline for cached YouTube audio.

    Safe, cumulative, and idempotent: skips files and tasks that are already
    present in SQLite or OpenSearch.
    """
    stats = CacheIngestStats()
    cache_dir = yt_dir or Path(settings.youtube_dir).expanduser()
    if not cache_dir.is_dir():
        log.warning("YouTube cache directory not found: %s", cache_dir)
        return stats

    files = [
        f for f in sorted(cache_dir.glob("*.*"))
        if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in MEDIA_SUFFIXES
    ]
    stats.total_files = len(files)
    if not files:
        return stats

    own_conn = conn is None
    c = conn or localcache.connect()

    try:
        localcache.delete_empty_approved_lyrics(conn=c)

        # 1. Map existing sources in DB
        cur = c.cursor()
        cur.execute("SELECT url, track_id FROM sources WHERE kind = 'youtube'")
        url_to_track: dict[str, int] = {
            row["url"]: int(row["track_id"]) for row in cur.fetchall() if row["url"]
        }

        # 2. Ingest unindexed audio files
        for i, file_path in enumerate(files, 1):
            vid_id = file_path.stem
            url = f"https://www.youtube.com/watch?v={vid_id}"
            if url in url_to_track:
                stats.skipped += 1
                continue

            if progress_callback:
                progress_callback("ingest", i, len(files), f"Resolving metadata: {vid_id}")

            ref = resolve_youtube(url, download=False)
            if not ref or not ref.artist or not ref.title:
                stats.errors += 1
                continue

            try:
                tid = localcache.add_track_source(
                    artist=ref.artist,
                    title=ref.title,
                    duration=ref.duration,
                    url=url,
                    kind="youtube",
                    conn=c,
                )
                url_to_track[url] = tid
                stats.new_tracks += 1
                if progress_callback:
                    progress_callback("ingest", i, len(files), f"Added: {ref.artist} - {ref.title}")
            except Exception as exc:
                log.debug("DB error adding cache source %s: %s", url, exc)
                stats.errors += 1

        # 3. Audio Analysis (Key/BPM/Energy) for cached tracks
        if analyze_audio_features:
            track_analysis.ensure_schema(c)
            cur.execute("SELECT track_id FROM track_analysis")
            analyzed_set = {int(row["track_id"]) for row in cur.fetchall()}

            for i, file_path in enumerate(files, 1):
                vid_id = file_path.stem
                url = f"https://www.youtube.com/watch?v={vid_id}"
                tid = url_to_track.get(url)
                if not tid or tid in analyzed_set:
                    continue

                if progress_callback:
                    progress_callback("analysis", i, len(files), f"Key/BPM: {file_path.name}")

                try:
                    res = analyze.analyze_audio(str(file_path))
                    detected_key = parse_key(res.key) if isinstance(res.key, str) else res.key
                    track_analysis.save_detected(
                        tid,
                        detected_key=detected_key,
                        key_confidence=res.key_confidence,
                        key_agreement=res.key_agreement,
                        bpm=res.bpm,
                        energy=getattr(res, "energy", None),
                        brightness=getattr(res, "brightness", None),
                        method=getattr(res, "method", "librosa"),
                        source_kind="youtube_cache",
                        conn=c,
                    )
                    analyzed_set.add(tid)
                    stats.analyzed += 1
                except Exception as exc:
                    log.debug("Analysis failed for %s: %s", file_path.name, exc)
                    stats.errors += 1

        # 4. OpenSearch CLAP vector embedding & Genre Classification
        can_clap = embed_clap and clap_vector.available()
        client = None
        if can_clap or classify_genres:
            try:
                if os_client is not None:
                    client = os_client
                else:
                    from .osclient import client as get_os_client
                    client = get_os_client()
            except Exception as exc:
                log.debug("OpenSearch client unavailable for cache ingest: %s", exc)
                client = None

        if can_clap and client is not None:
            clap_vector.ensure_index(client)
            try:
                res = client.search(
                    index=clap_vector.CLAP_INDEX,
                    body={"size": 10000, "_source": ["track_id"], "query": {"match_all": {}}},
                )
                embedded_set = {int(h["_source"]["track_id"]) for h in res["hits"]["hits"]}
            except Exception:
                embedded_set = set()

            # Cache of fresh vectors in this run so genre classifier doesn't need to re-fetch
            fresh_vectors: dict[int, list[float]] = {}

            # Pre-load track metadata
            cur.execute("SELECT track_id, artist, title, album FROM tracks")
            track_meta = {
                int(r["track_id"]): {
                    "artist": r["artist"] or "",
                    "title": r["title"] or "",
                    "album": r["album"] or "",
                }
                for r in cur.fetchall()
            }

            for i, file_path in enumerate(files, 1):
                vid_id = file_path.stem
                url = f"https://www.youtube.com/watch?v={vid_id}"
                tid = url_to_track.get(url)
                if not tid or tid in embedded_set:
                    continue

                meta = track_meta.get(tid, {"artist": "", "title": "", "album": ""})
                if progress_callback:
                    progress_callback(
                        "clap", i, len(files), f"CLAP embedding: {meta['artist']} - {meta['title']}"
                    )

                try:
                    vec = clap_vector.embed_audio(str(file_path))
                    if vec is not None:
                        doc = clap_vector.build_doc(
                            track_id=tid,
                            artist=meta["artist"],
                            title=meta["title"],
                            album=meta["album"],
                            vector=vec,
                            embedded_at=datetime.now(timezone.utc).isoformat(),
                            source="youtube_cache",
                        )
                        client.index(
                            index=clap_vector.CLAP_INDEX,
                            id=clap_vector.doc_id(tid),
                            body=doc,
                        )
                        embedded_set.add(tid)
                        fresh_vectors[tid] = vec
                        stats.clap_embedded += 1
                except Exception as exc:
                    log.debug("CLAP embedding failed for %s: %s", file_path.name, exc)
                    stats.errors += 1

            # 5. Genre Labelling
            if classify_genres:
                localcache.ensure_genre_table(c)
                cur.execute(
                    "SELECT track_id FROM track_genre WHERE genre IS NOT NULL AND genre != ''"
                )
                genred_set = {int(r["track_id"]) for r in cur.fetchall()}

                labels = genre.label_vectors()
                if labels:
                    for i, file_path in enumerate(files, 1):
                        vid_id = file_path.stem
                        url = f"https://www.youtube.com/watch?v={vid_id}"
                        tid = url_to_track.get(url)
                        if not tid or tid in genred_set:
                            continue

                        # Grab vector from fresh memory or OpenSearch
                        vec = fresh_vectors.get(tid)
                        if vec is None:
                            try:
                                get_res = client.get(
                                    index=clap_vector.CLAP_INDEX,
                                    id=clap_vector.doc_id(tid),
                                    _source=["clap_vector"],
                                )
                                vec = get_res["_source"].get("clap_vector")
                            except Exception:
                                vec = None

                        if not vec:
                            continue

                        meta = track_meta.get(tid, {"artist": "", "title": ""})
                        if progress_callback:
                            progress_callback(
                                "genre", i, len(files), f"Classifying genre: {meta['artist']} - {meta['title']}"
                            )

                        verdict = genre.classify(vec, labels)
                        if verdict:
                            localcache.record_genre(tid, verdict, c)
                            genred_set.add(tid)
                            stats.genres_labeled += 1

                            # Mirror genre tags to OpenSearch docs
                            body = {
                                "doc": {
                                    "genre": verdict.genre,
                                    "genre_score": verdict.score,
                                    "genre_runner_up": verdict.runner_up,
                                }
                            }
                            try:
                                client.update(
                                    index=clap_vector.CLAP_INDEX,
                                    id=clap_vector.doc_id(tid),
                                    body=body,
                                )
                            except Exception:
                                pass
                            try:
                                from .vector_index import track_doc_id
                                client.update(
                                    index=settings.index_name,
                                    id=track_doc_id(tid),
                                    body={
                                        "doc": {
                                            "audio_genre": verdict.genre,
                                            "audio_genre_runner_up": verdict.runner_up,
                                        }
                                    },
                                )
                            except Exception:
                                pass

        # 6. Enqueue for word timings via Celery
        if enqueue_postprocessing:
            try:
                from .postprocess_queue import needs_postprocessing, publish_postprocess_task

                for url, tid in url_to_track.items():
                    pending = needs_postprocessing(tid, c)
                    if "timings" in pending or "sync" in pending:
                        cur.execute("SELECT artist, title FROM tracks WHERE track_id = %s", (tid,))
                        row = cur.fetchone()
                        if row and publish_postprocess_task(row["artist"], row["title"], url):
                            stats.enqueued += 1
            except Exception as exc:
                log.debug("Error during cache postprocessing enqueue: %s", exc)

    finally:
        if own_conn:
            c.close()

    return stats
