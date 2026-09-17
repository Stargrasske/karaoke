"""Retry pass: re-ingest only the collection files not yet in the database.

The overnight import skips a file whenever the DB write loses the SQLite lock
(``database is locked``). Those files never get a ``sources`` row, so this pass
finds every on-disk audio file under the collection root that has NO
``kind='local'`` source pointing at its exact path and re-runs the full folder
pipeline over just those. It is resumable by design: each run recomputes the
missing set, so interrupting and re-launching simply continues where it left
off. WAL mode (now the default) makes lock-skips rare on the retry.

Usage:
    PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py [--dry-run]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from karaoke import folder_scan, localcache, tags, vector_index
from karaoke.logger import log

ROOT = Path("/run/media/tina/52C3-BEF5/Music-backup-DATA/")
LOG_PATH = Path("~/.local/share/karaoke/logs/retry_skipped_collection.log").expanduser()
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

DRY_RUN = "--dry-run" in sys.argv


def write(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    print(text, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


def progress(event: str, payload: dict) -> None:
    index = payload.get("index")
    total = payload.get("total")
    prefix = f"{index}/{total}" if index and total else ""
    name = payload.get("name") or payload.get("root") or ""
    artist = payload.get("artist") or ""
    title = payload.get("title") or ""
    track = f" -> {artist} - {title}" if artist or title else ""

    if event == "found":
        write(f"RETRY TARGETS {payload.get('total', 0)} file(s) under {name}")
    elif event == "item_start":
        write(f"START {prefix} {name}")
    elif event == "source_done":
        online = []
        if payload.get("yt_url"):
            online.append("YouTube")
        if payload.get("spotify_uri"):
            online.append("Spotify")
        write(f"SOURCES {prefix} {', '.join(online) or 'none'}{track}")
    elif event == "analysis_done":
        write(f"ANALYSIS {prefix} key={payload.get('key')} bpm={payload.get('bpm')}{track}")
    elif event == "item_done":
        write(f"DONE {prefix} track_id={payload.get('track_id', '?')}{track}")
    elif event == "skip":
        write(f"SKIP {prefix} {name}: {payload.get('reason')}")
    elif event == "error":
        write(f"ERROR {prefix} {name}: {payload.get('error')}")


def already_ingested_paths() -> set[str]:
    """Absolute paths that already have a local source row (case as stored)."""
    conn = localcache.connect()
    try:
        rows = conn.execute(
            "SELECT url FROM sources WHERE kind = 'local' AND url IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def main() -> int:
    write(f"BEGIN retry root={ROOT} dry_run={DRY_RUN}")
    write(f"Log file={LOG_PATH}")

    if not ROOT.is_dir():
        write(f"FATAL ROOT not mounted: {ROOT}")
        return 2

    on_disk = [p for p in sorted(ROOT.rglob("*")) if p.is_file() and tags.is_audio(p)]
    have = already_ingested_paths()
    missing = {str(p) for p in on_disk if str(p) not in have}
    write(f"On disk={len(on_disk)} already ingested(local)={len(have)} missing={len(missing)}")

    if not missing:
        write("NOTHING TO RETRY — every on-disk file already has a local source.")
        return 0

    try:
        stats = folder_scan.scan_and_ingest_folder(
            ROOT,
            use_fingerprint=True,
            classify_audio=True,
            resolve_streaming=True,
            dry_run=DRY_RUN,
            only_paths=missing,
            progress=progress,
        )
        write(
            "RETRY SCAN COMPLETE "
            + " ".join(f"{key}={stats.get(key)}" for key in (
                "seen", "processed", "fingerprinted", "classified", "sourced", "errors"))
        )

        if not DRY_RUN and stats.get("processed"):
            write("VECTOR REBUILD BEGIN")
            conn = localcache.connect()
            try:
                if missing:
                    placeholders = ",".join("?" for _ in missing)
                    rows = conn.execute(
                        f"SELECT DISTINCT track_id FROM sources WHERE kind = 'local' AND url IN ({placeholders})",
                        list(missing),
                    ).fetchall()
                    newly_ingested_ids = [r[0] for r in rows if r[0]]
                else:
                    newly_ingested_ids = []
            finally:
                conn.close()

            if newly_ingested_ids:
                write(f"Targeted vector indexing for {len(newly_ingested_ids)} tracks...")
                result = vector_index.rebuild_from_sqlite(
                    track_ids=newly_ingested_ids,
                    embed=True,
                    include_lines=True,
                )
            else:
                result = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
            write(
                f"VECTOR REBUILD COMPLETE indexed={result.indexed} "
                f"line_docs={result.line_docs} note_docs={result.note_docs}"
            )
        write("ALL DONE")
        return 0
    except Exception as exc:
        log.exception("Retry skipped-collection scan failed")
        write(f"FATAL ERROR {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
