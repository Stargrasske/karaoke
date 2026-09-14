"""Overnight full collection ingestion and vector indexing job."""
from __future__ import annotations

import time
from pathlib import Path

from karaoke import folder_scan, vector_index
from karaoke.logger import log

ROOT = Path("/run/media/tina/52C3-BEF5/Music-backup-DATA/")
LOG_PATH = Path("~/.local/share/karaoke/logs/overnight_collection_scan.log").expanduser()
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def write(line: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = f"[{stamp}] {line}"
    # Persist first: unattended runs may outlive the launching terminal/process,
    # leaving stdout as a broken pipe. The logfile must keep advancing even then.
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    try:
        print(text, flush=True)
    except BrokenPipeError:
        pass


def progress(event: str, payload: dict) -> None:
    index = payload.get("index")
    total = payload.get("total")
    prefix = f"{index}/{total}" if index and total else ""
    name = payload.get("name") or payload.get("root") or ""
    artist = payload.get("artist") or ""
    title = payload.get("title") or ""
    track = f" -> {artist} - {title}" if artist or title else ""

    if event == "found":
        write(f"FOUND {payload.get('total', 0)} audio files under {name}")
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


write(f"BEGIN root={ROOT}")
write(f"Log file={LOG_PATH}")

try:
    stats = folder_scan.scan_and_ingest_folder(
        ROOT,
        use_fingerprint=True,
        classify_audio=True,
        resolve_streaming=True,
        dry_run=False,
        progress=progress,
    )
    write(
        "SCAN COMPLETE "
        + " ".join(f"{key}={stats.get(key)}" for key in (
            "seen", "processed", "fingerprinted", "classified", "sourced", "errors"))
    )

    write("VECTOR REBUILD BEGIN")
    result = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
    write(f"VECTOR REBUILD COMPLETE indexed={result.indexed} line_docs={result.line_docs} note_docs={result.note_docs}")
    write("ALL DONE")
except Exception as exc:
    log.exception("Overnight collection scan failed")
    write(f"FATAL ERROR {type(exc).__name__}: {exc}")
    raise
