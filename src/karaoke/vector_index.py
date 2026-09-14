"""Build derived OpenSearch vector indexes from the local SQLite database.

SQLite remains the operational source of truth. This module reads approved tracks,
sources and lyrics from SQLite and writes rebuildable documents to OpenSearch for
semantic search and future training features.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import localcache
from .config import settings
from .lyrics import parse_lrc


@dataclass
class VectorIndexStats:
    """Counters returned by a SQLite -> OpenSearch indexing run."""

    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    errors: int = 0
    line_docs: int = 0
    note_docs: int = 0


def track_doc_id(track_id: int) -> str:
    """Stable OpenSearch id for a SQLite-backed track document."""
    return f"sqlite:{track_id}"


def line_doc_id(track_id: int, line_index: int) -> str:
    """Stable OpenSearch id for a SQLite-backed lyric-line document."""
    return f"sqlite-line:{track_id}:{line_index}"


def _embedding_text(row: Any) -> str:
    plain = row["plain_lyrics"] or ""
    if plain.strip():
        return plain
    return f"{row['title']} {row['artist']} {row['album'] or ''}".strip()


def iter_track_rows(conn: Any) -> Iterable[Any]:
    """Yield one row per SQLite track with preferred source, approved lyrics, and audio genre."""
    cur = conn.cursor()
    has_track_genre = True
    try:
        t_cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_genre'")
        if not t_cur.fetchone():
            has_track_genre = False
    except Exception:
        has_track_genre = False

    genre_cols = "tg.genre AS audio_genre, tg.runner_up AS audio_genre_runner_up" if has_track_genre else "'' AS audio_genre, '' AS audio_genre_runner_up"
    genre_join = "LEFT JOIN track_genre tg ON tg.track_id = t.track_id" if has_track_genre else ""

    cur.execute(
        f"""
        SELECT
            t.track_id,
            t.artist,
            t.title,
            t.album,
            t.duration,
            s.kind AS source_kind,
            s.url AS source_url,
            s.player_name,
            l.source AS lyrics_source,
            l.synced_lyrics,
            l.plain_lyrics,
            {genre_cols}
        FROM tracks t
        {genre_join}
        LEFT JOIN sources s ON s.source_id = (
            SELECT source_id FROM sources
            WHERE track_id = t.track_id
            ORDER BY CASE kind
                WHEN 'youtube' THEN 0
                WHEN 'spotify' THEN 1
                WHEN 'local' THEN 2
                ELSE 3
            END, source_id
            LIMIT 1
        )
        LEFT JOIN lyrics l ON l.lyric_id = (
            SELECT lyric_id FROM lyrics
            WHERE track_id = t.track_id AND kind = 'approved'
            ORDER BY lyric_id DESC
            LIMIT 1
        )
        ORDER BY t.artist, t.title
        """
    )
    yield from cur.fetchall()


def build_track_doc(
    row: Any,
    *,
    embed: bool = True,
    artist_genres_map: Optional[dict[str, dict[str, list[str]]]] = None,
) -> dict[str, Any]:
    """Build one OpenSearch track document from a SQLite query row."""
    synced = row["synced_lyrics"] or ""
    plain = row["plain_lyrics"] or ""
    lines = parse_lrc(synced) if synced else []
    keys = row.keys() if hasattr(row, "keys") else ()
    audio_g = row["audio_genre"] if "audio_genre" in keys else ""
    audio_ru = row["audio_genre_runner_up"] if "audio_genre_runner_up" in keys else ""

    from .artist_classifier import classify_artist, normalize_artist
    norm_artist = normalize_artist(row["artist"] or "")
    artist_info = artist_genres_map.get(norm_artist) if artist_genres_map else None
    if artist_info:
        artist_genres = list(artist_info.get("specific") or [])
        broad_genres = list(artist_info.get("broad") or [])
    else:
        artist_genres, broad_genres = classify_artist(row["artist"] or "", online=False)

    doc: dict[str, Any] = {
        "track_id": row["track_id"],
        "path": row["source_url"] or "",
        "source_url": row["source_url"] or "",
        "source_kind": row["source_kind"] or "sqlite",
        "player_name": row["player_name"] or "",
        "title": row["title"] or "",
        "artist": row["artist"] or "",
        "album": row["album"] or "",
        "duration": row["duration"],
        "source": "sqlite",
        "has_synced": bool(lines),
        "lyrics_source": row["lyrics_source"] or "none",
        "plain_lyrics": plain,
        "synced_lyrics": synced,
        "artist_genres": artist_genres,
        "broad_genres": broad_genres,
        "audio_genre": audio_g or "",
        "audio_genre_runner_up": audio_ru or "",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    lyrics_text = plain or ("\n".join(t for _, t in lines) if lines else "")
    if lyrics_text:
        from . import smartlist, visuals
        profile = visuals.analyze_sentiment(lyrics_text)
        doc["dominant_mood"] = profile.dominant
        doc["sentiment_hits"] = profile.total_hits
        doc["sentiment_vector"] = list(smartlist.mood_vector(profile))
    else:
        doc["dominant_mood"] = "neutral"
        doc["sentiment_hits"] = 0
        doc["sentiment_vector"] = [0.0, 0.0, 0.0, 0.0]

    if embed:
        from .embed import embed_text

        doc["lyrics_vector"] = embed_text(_embedding_text(row))
    return doc


def build_line_docs(row: Any, *, embed: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Build line-level documents for future semantic/timing experiments."""
    synced = row["synced_lyrics"] or ""
    parsed = parse_lrc(synced) if synced else []
    if not parsed:
        return []
    docs: list[tuple[str, dict[str, Any]]] = []
    texts: list[str] = []
    base_docs: list[dict[str, Any]] = []
    for i, (start_s, text) in enumerate(parsed):
        end_s = parsed[i + 1][0] if i + 1 < len(parsed) else None
        previous_text = parsed[i - 1][1] if i else ""
        next_text = parsed[i + 1][1] if i + 1 < len(parsed) else ""
        context = "\n".join(t for t in (previous_text, text, next_text) if t)
        doc = {
            "track_id": row["track_id"],
            "line_index": i,
            "artist": row["artist"] or "",
            "title": row["title"] or "",
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": (end_s - start_s) if end_s is not None else None,
            "text": text,
            "context": context,
            "source": "sqlite-line",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        docs.append((line_doc_id(row["track_id"], i), doc))
        texts.append(context or text)
        base_docs.append(doc)
    if embed:
        from .embed import embed_batch

        for doc, vec in zip(base_docs, embed_batch(texts)):
            doc["line_vector"] = vec
    return docs


def ensure_line_index(os_client: Any, index_name: str) -> bool:
    """Create the line-level vector index if absent."""
    if os_client.indices.exists(index=index_name):
        return False
    from .osclient import synonym_settings
    body = {
        "settings": {
            "index": {"knn": True, "number_of_replicas": 0},
            "analysis": synonym_settings()["analysis"],
        },
        "mappings": {
            "properties": {
                "track_id": {"type": "integer"},
                "line_index": {"type": "integer"},
                "artist": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer",
                    "fields": {"raw": {"type": "keyword"}}
                },
                "title": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer",
                    "fields": {"raw": {"type": "keyword"}}
                },
                "start_s": {"type": "float"},
                "end_s": {"type": "float"},
                "duration_s": {"type": "float"},
                "text": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer"
                },
                "context": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer"
                },
                "source": {"type": "keyword"},
                "indexed_at": {"type": "date"},
                "line_vector": {
                    "type": "knn_vector",
                    "dimension": settings.embed_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
            }
        },
    }
    os_client.indices.create(index=index_name, body=body)
    return True


def note_doc_id(note_id: int) -> str:
    """Stable OpenSearch id for a SQLite-backed note document."""
    return f"sqlite-note:{note_id}"


def build_note_doc(row: Any, *, embed: bool = True) -> dict[str, Any]:
    """Build one document for text about a track that is not its lyrics.

    Notes are their own documents rather than another vector on the track doc,
    for two reasons. A track can hold both a biography and a transcription and
    they should be findable separately, each saying which it is. And a
    1400-character band history folded into a track's lyric embedding would
    drag that track toward every prose query in the index, degrading the search
    that matters most.
    """
    doc: dict[str, Any] = {
        "note_id": row["note_id"],
        "track_id": row["track_id"],
        "kind": row["kind"],
        "artist": row["artist"] or "",
        "title": row["title"] or "",
        "album": row["album"] or "",
        "text": row["text"] or "",
        "note_source": row["source"] or "",
        "confidence": row["confidence"],
        "source": "sqlite-note",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    if embed:
        from .embed import embed_text

        # The artist and title are prepended because a biography rarely repeats
        # the band's own name in a searchable way, and a query is far more often
        # "that Belgian nineties band" than a phrase from the text itself.
        context = f"{doc['artist']} {doc['title']}\n{doc['text']}".strip()
        doc["note_vector"] = embed_text(context)
    return doc


def ensure_note_index(os_client: Any, index_name: str) -> bool:
    """Create the note-level vector index if absent."""
    if os_client.indices.exists(index=index_name):
        return False
    from .osclient import synonym_settings
    body = {
        "settings": {
            "index": {"knn": True, "number_of_replicas": 0},
            "analysis": synonym_settings()["analysis"],
        },
        "mappings": {
            "properties": {
                "note_id": {"type": "integer"},
                "track_id": {"type": "integer"},
                "kind": {"type": "keyword"},
                "artist": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer",
                    "fields": {"raw": {"type": "keyword"}}
                },
                "title": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer",
                    "fields": {"raw": {"type": "keyword"}}
                },
                "album": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer",
                    "fields": {"raw": {"type": "keyword"}}
                },
                "text": {
                    "type": "text",
                    "analyzer": "synonym_analyzer",
                    "search_analyzer": "synonym_analyzer"
                },
                "note_source": {"type": "keyword"},
                "confidence": {"type": "float"},
                "source": {"type": "keyword"},
                "indexed_at": {"type": "date"},
                "note_vector": {
                    "type": "knn_vector",
                    "dimension": settings.embed_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
            }
        },
    }
    os_client.indices.create(index=index_name, body=body)
    return True


PROGRESS_FILE = "vector_index_progress.json"


def _progress_path(path: Optional[Path] = None) -> Path:
    return path if path is not None else Path(settings.data_dir) / PROGRESS_FILE


def get_progress(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Return the most recent progress report if it exists."""
    p = _progress_path(path)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_progress(data: dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomically record progress to a local JSON file."""
    p = _progress_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(p)
    except Exception:
        pass


def _iter_actions(
    rows: list[Any],
    note_rows: list[Any],
    *,
    embed: bool,
    include_lines: bool,
    include_notes: bool,
    dry_run: bool,
    stats: VectorIndexStats,
    progress_callback: Optional[Callable[[VectorIndexStats, int], None]] = None,
    artist_genres_map: Optional[dict[str, dict[str, list[str]]]] = None,
) -> Iterable[tuple[str, str, dict[str, Any]]]:
    """Yield ``(index, doc_id, source)`` for every document to write.

    Documents are produced one track at a time so the embedding of a track's
    lines is the only vector batch held in memory; the caller flushes in chunks,
    keeping a full rebuild's footprint bounded regardless of library size. Stats
    are counted here as each doc is produced; transport-level failures are
    counted separately by the flusher.
    """
    total = len(rows)
    for row in rows:
        stats.seen += 1
        try:
            doc = build_track_doc(row, embed=embed, artist_genres_map=artist_genres_map)
            if dry_run:
                stats.skipped += 1
            else:
                stats.indexed += 1
            yield settings.index_name, track_doc_id(row["track_id"]), doc
            if include_lines:
                for _doc_id, line_doc in build_line_docs(row, embed=embed):
                    stats.line_docs += 1
                    yield f"{settings.index_name}-lines", _doc_id, line_doc
        except Exception:
            stats.errors += 1

        if progress_callback is not None and (stats.seen % 250 == 0 or stats.seen == total):
            progress_callback(stats, total)

    if include_notes:
        for note_row in note_rows:
            try:
                note_doc = build_note_doc(note_row, embed=embed)
                stats.note_docs += 1
                yield (f"{settings.index_name}-notes",
                       note_doc_id(note_row["note_id"]), note_doc)
            except Exception:
                stats.errors += 1


def _bulk_write(
    c: Any,
    actions: Iterable[tuple[str, str, dict[str, Any]]],
    *,
    chunk_size: int,
    request_timeout: int = 120,
) -> int:
    """Write documents to OpenSearch in batches via the native ``_bulk`` API.

    One HTTP request carries up to ``chunk_size`` documents, replacing the
    per-document round-trip that dominated a full rebuild (~one request per doc
    at ~13ms each). Fewer, larger requests are also gentler on the cluster and
    leave headroom for a concurrent TUI/web-TUI. Returns the number of documents
    the cluster rejected.
    """
    errors = 0
    batch: list[tuple[str, str, dict[str, Any]]] = []

    def flush() -> int:
        if not batch:
            return 0
        body: list[dict[str, Any]] = []
        for index, doc_id, source in batch:
            body.append({"index": {"_index": index, "_id": doc_id}})
            body.append(source)
        resp = c.bulk(body=body, request_timeout=request_timeout)
        failed = 0
        if resp.get("errors"):
            for item in resp.get("items", []):
                if item.get("index", {}).get("error"):
                    failed += 1
        batch.clear()
        return failed

    for action in actions:
        batch.append(action)
        if len(batch) >= chunk_size:
            errors += flush()
    errors += flush()
    return errors


def rebuild_from_sqlite(
    *,
    db_path: Optional[str] = None,
    limit: Optional[int] = None,
    embed: bool = True,
    include_lines: bool = False,
    include_notes: bool = False,
    dry_run: bool = False,
    os_client: Any = None,
    chunk_size: int = 500,
) -> VectorIndexStats:
    """Index SQLite tracks into OpenSearch; safe to re-run.

    Writes go out in batches of ``chunk_size`` documents through the OpenSearch
    ``_bulk`` API rather than one request per document.
    """
    # Production rebuilds touch the shared OpenSearch indexes and need a global
    # lock. Explicit test/spike rebuilds against an explicit DB and either a
    # fake client or dry-run are isolated; let them run even while a live Admin
    # TUI rebuild holds the production lock.
    isolated = db_path is not None and (dry_run or os_client is not None)
    lock = None
    if not isolated:
        from .lockfile import ProcessLock
        lock = ProcessLock("vector_rebuild")
        if not lock.acquire():
            from .logger import log
            log.info("vector_index: rebuild lock held by another process; skipping concurrent run.")
            return VectorIndexStats()

    started_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    def progress_cb(st: VectorIndexStats, total: int) -> None:
        pct = round(st.seen / total * 100, 1) if total else 0.0
        from .logger import log
        log.info(
            "vector_index rebuild progress: %d/%d tracks (%.1f%%), %d lines, %d errors",
            st.seen, total, pct, st.line_docs, st.errors,
        )
        if not isolated:
            import os
            _write_progress({
                "status": "running",
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "total_tracks": total,
                "processed_tracks": st.seen,
                "indexed_tracks": st.indexed,
                "line_docs": st.line_docs,
                "note_docs": st.note_docs,
                "errors": st.errors,
                "percent": pct,
                "pid": os.getpid(),
            })

    try:
        from .osclient import client, ensure_index

        conn = localcache.connect(None if db_path is None else Path(db_path))
        stats = VectorIndexStats()
        try:
            artist_genres_map = localcache.get_all_artist_genres_map(conn)
            rows = list(iter_track_rows(conn))
            note_rows = list(localcache.iter_note_rows(conn)) if include_notes else []
        finally:
            conn.close()

        if limit is not None:
            rows = rows[:limit]

        c = os_client if os_client is not None else (None if dry_run else client())
        if c is not None:
            ensure_index(c)
            if include_lines:
                ensure_line_index(c, f"{settings.index_name}-lines")
            if include_notes:
                ensure_note_index(c, f"{settings.index_name}-notes")

        actions = _iter_actions(
            rows, note_rows, embed=embed, include_lines=include_lines,
            include_notes=include_notes, dry_run=dry_run, stats=stats,
            progress_callback=progress_cb if not isolated else None,
            artist_genres_map=artist_genres_map,
        )

        if dry_run or c is None:
            # Consume the generator to build docs and populate counts without
            # touching the cluster.
            for _ in actions:
                pass
        else:
            stats.errors += _bulk_write(c, actions, chunk_size=chunk_size)
            c.indices.refresh(index=settings.index_name)
            if include_lines:
                c.indices.refresh(index=f"{settings.index_name}-lines")
            if include_notes:
                c.indices.refresh(index=f"{settings.index_name}-notes")

        if not isolated:
            import os
            total = len(rows)
            pct = 100.0 if total and stats.seen == total else (round(stats.seen / total * 100, 1) if total else 0.0)
            _write_progress({
                "status": "completed" if stats.errors == 0 else "completed_with_errors",
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "total_tracks": total,
                "processed_tracks": stats.seen,
                "indexed_tracks": stats.indexed,
                "line_docs": stats.line_docs,
                "note_docs": stats.note_docs,
                "errors": stats.errors,
                "percent": pct,
                "pid": os.getpid(),
            })

        return stats
    except Exception as exc:
        if not isolated:
            import os
            _write_progress({
                "status": "failed",
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "error": str(exc),
                "pid": os.getpid(),
            })
        raise
    finally:
        if lock is not None:
            lock.release()


def vector_index_status(
    *,
    db_path: Optional[str] = None,
    os_client: Any = None,
) -> dict[str, Any]:
    """Inspect the current vector indexing state across SQLite, OpenSearch, and locks."""
    from .lockfile import ProcessLock
    lock = ProcessLock("vector_rebuild")
    is_running = lock.is_locked()
    progress = get_progress()

    db_tracks = None
    db_error = None
    try:
        conn = localcache.connect(None if db_path is None else Path(db_path))
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM tracks")
            row = cursor.fetchone()
            db_tracks = row[0] if row else 0
        finally:
            conn.close()
    except Exception as exc:
        db_error = str(exc)

    c = os_client
    os_connected = False
    os_error = None
    os_indices: dict[str, dict[str, Any]] = {}
    if c is None:
        try:
            from .osclient import client
            c = client()
        except Exception as exc:
            os_error = str(exc)

    if c is not None:
        try:
            if hasattr(c, "cat") and hasattr(c.cat, "indices"):
                items = c.cat.indices(index=f"{settings.index_name}*", format="json")
                os_connected = True
                for item in items:
                    name = item.get("index")
                    if name:
                        os_indices[name] = {
                            "docs_count": int(item.get("docs.count", 0)),
                            "store_size": item.get("store.size", "0b"),
                            "health": item.get("health", "unknown"),
                            "status": item.get("status", "unknown"),
                        }
            else:
                os_connected = True
        except Exception as exc:
            os_error = str(exc)

    tracks_indexed = os_indices.get(settings.index_name, {}).get("docs_count") if os_connected else None
    lines_indexed = os_indices.get(f"{settings.index_name}-lines", {}).get("docs_count") if os_connected else None
    notes_indexed = os_indices.get(f"{settings.index_name}-notes", {}).get("docs_count") if os_connected else None

    pct = None
    if db_tracks and tracks_indexed is not None and db_tracks > 0:
        pct = round(min(100.0, (tracks_indexed / db_tracks) * 100), 1)

    return {
        "is_running": is_running,
        "db_tracks": db_tracks,
        "db_error": db_error,
        "os_connected": os_connected,
        "os_error": os_error,
        "tracks_indexed": tracks_indexed,
        "lines_indexed": lines_indexed,
        "notes_indexed": notes_indexed,
        "percent_complete": pct,
        "indices": os_indices,
        "progress": progress,
    }


def format_status_report(status: dict[str, Any]) -> str:
    lines = ["Vector Index Status:"]
    running = status.get("is_running")
    lines.append(f"  Rebuild running: {'YES (in progress)' if running else 'NO (idle)'}")

    prog = status.get("progress")
    if prog:
        st = prog.get("status", "unknown")
        proc = prog.get("processed_tracks", 0)
        tot = prog.get("total_tracks", 0)
        pct = prog.get("percent", 0.0)
        started = prog.get("started_at", "")
        updated = prog.get("updated_at", "")
        lines.append(f"  Last run status: {st} ({proc:,}/{tot:,} tracks, {pct}%)")
        if started or updated:
            lines.append(f"    Started: {started} | Updated: {updated}")
        if prog.get("error"):
            lines.append(f"    Error: {prog['error']}")

    db_tracks = status.get("db_tracks")
    if db_tracks is not None:
        lines.append(f"  SQLite library:  {db_tracks:,} tracks")
    elif status.get("db_error"):
        lines.append(f"  SQLite library:  Error ({status['db_error']})")

    if status.get("os_connected"):
        lines.append(f"  OpenSearch:      Connected ({settings.opensearch_url})")
        tracks_count = status.get("tracks_indexed")
        pct = status.get("percent_complete")
        pct_str = f" ({pct}% of SQLite library)" if pct is not None else ""
        if tracks_count is not None:
            lines.append(f"    - tracks:       {tracks_count:,} docs{pct_str}")
        else:
            lines.append("    - tracks:       index not found")

        lines_count = status.get("lines_indexed")
        if lines_count is not None:
            lines.append(f"    - tracks-lines: {lines_count:,} docs")
        notes_count = status.get("notes_indexed")
        if notes_count is not None:
            lines.append(f"    - tracks-notes: {notes_count:,} docs")
    else:
        err = status.get("os_error", "Not reachable")
        lines.append(f"  OpenSearch:      Disconnected ({err})")

    return "\n".join(lines)


def vector_index_main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for rebuilding derived vector indexes from SQLite."""
    ap = argparse.ArgumentParser(
        prog="karaoke-vector-index",
        description="Rebuild optional OpenSearch vector indexes from the SQLite DB",
    )
    ap.add_argument("--rebuild", action="store_true", help="index SQLite tracks into OpenSearch")
    ap.add_argument("--lines", action="store_true", help="also build a line-level index")
    ap.add_argument("--notes", action="store_true",
                    help="also index track notes (biographies, transcriptions)")
    ap.add_argument("--no-embed", action="store_true", help="skip embedding generation")
    ap.add_argument("--dry-run", action="store_true", help="read/build docs without writing OpenSearch")
    ap.add_argument("--status", action="store_true", help="show current indexing status and progress")
    ap.add_argument("--json", action="store_true", help="output status in JSON format")
    ap.add_argument("--limit", type=int, default=None, help="maximum tracks to process")
    ap.add_argument("--db", default=None, help="SQLite DB path (default: configured local DB)")
    ap.add_argument("--chunk-size", type=int, default=500,
                    help="documents per OpenSearch _bulk request (default 500)")
    ap.add_argument("--threads", type=int, default=None,
                    help="cap CPU threads used for embedding, to leave headroom "
                         "for a running TUI/web-TUI (default: all cores)")
    args = ap.parse_args(argv)

    if args.status:
        st = vector_index_status(db_path=args.db)
        if args.json:
            print(json.dumps(st, indent=2))
        else:
            print(format_status_report(st))
        return 0

    if not args.rebuild and not args.dry_run:
        ap.error("choose --rebuild, --dry-run, or --status")

    if args.threads and args.threads > 0:
        # Bound the embedding thread pool so a rebuild does not saturate every
        # core and starve a concurrent TUI/web-TUI. Set before torch imports.
        import os
        for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "TORCH_NUM_THREADS"):
            os.environ[var] = str(args.threads)
        try:
            import torch
            torch.set_num_threads(args.threads)
        except Exception:
            pass

    stats = rebuild_from_sqlite(
        db_path=args.db,
        limit=args.limit,
        embed=not args.no_embed,
        include_lines=args.lines,
        include_notes=args.notes,
        dry_run=args.dry_run,
        chunk_size=args.chunk_size,
    )
    action = "dry-run" if args.dry_run else "indexed"
    print(
        f"{action}: seen={stats.seen} indexed={stats.indexed} "
        f"skipped={stats.skipped} line_docs={stats.line_docs} "
        f"note_docs={stats.note_docs} errors={stats.errors}"
    )
    return 1 if stats.errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(vector_index_main())
