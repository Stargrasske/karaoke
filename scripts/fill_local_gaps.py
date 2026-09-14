#!/usr/bin/env python3
"""Fill local-ingestion gaps by walking disk and comparing against SQLite.

The overnight scan walks every file and re-does the expensive work regardless of
what is already stored, so an interrupted run cannot be cheaply resumed. This
script inverts that: it discovers what is on disk, subtracts the ``local``
source paths already in SQLite, and hands only the difference to
``folder_scan.scan_and_ingest_folder`` via ``only_paths``.

Offline by default (``resolve_streaming=False``): filling gaps should not spend
YouTube/Spotify lookups per file. Pass ``--online`` when stream links matter.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

from karaoke import folder_scan, localcache, tags
from karaoke.logger import log

DEFAULT_ROOT = Path("/run/media/tina/52C3-BEF5/Music-backup-DATA")
LOG_PATH = Path("~/.local/share/karaoke/logs/fill_local_gaps.log").expanduser()


def write(line: str) -> None:
    """Log to file first, then stdout; unattended runs may lose their terminal."""
    text = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")
    try:
        print(text, flush=True)
    except BrokenPipeError:
        pass


def known_local_paths() -> set[str]:
    """Absolute paths already registered as a ``local`` source.

    Read-only so this stays safe to run while another writer holds the DB.
    """
    db = localcache.settings.local_db
    con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
    try:
        return {row[0] for row in con.execute(
            "SELECT url FROM sources WHERE kind = 'local'")}
    finally:
        con.close()


def audio_on_disk(root: Path) -> list[str]:
    """Every audio file under ``root``, using the same predicate as the scanner."""
    found: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            candidate = Path(dirpath) / name
            if tags.is_audio(candidate):
                found.append(str(candidate))
    return sorted(found)


def progress(event: str, payload: dict) -> None:
    index, total = payload.get("index"), payload.get("total")
    prefix = f"{index}/{total}" if index and total else ""
    name = payload.get("name") or ""
    artist = payload.get("artist") or ""
    title = payload.get("title") or ""
    track = f" -> {artist} - {title}" if artist or title else ""

    if event == "item_start":
        write(f"START {prefix} {name}")
    elif event == "analysis_done":
        write(f"ANALYSIS {prefix} key={payload.get('key')} bpm={payload.get('bpm')}{track}")
    elif event == "item_done":
        write(f"DONE {prefix} track_id={payload.get('track_id', '?')}{track}")
    elif event == "skip":
        write(f"SKIP {prefix} {name}: {payload.get('reason')}")
    elif event == "error":
        write(f"ERROR {prefix} {name}: {payload.get('error')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="library root to walk")
    ap.add_argument("--limit", type=int, help="process at most N missing files")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the gap and exit without ingesting")
    ap.add_argument("--online", action="store_true",
                    help="also resolve YouTube/Spotify links (slower, network)")
    ap.add_argument("--no-fingerprint", action="store_true",
                    help="skip songrec fingerprinting for untagged files")
    ap.add_argument("--no-classify", action="store_true",
                    help="skip key/BPM/genre analysis")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"Root not found: {root}", file=sys.stderr)
        return 2

    write(f"GAP SCAN root={root} online={args.online}")
    known = known_local_paths()
    disk = audio_on_disk(root)
    missing = [p for p in disk if p not in known]

    # A path in the DB that no longer exists is the opposite problem: the row is
    # stale rather than absent. Report it so a moved/unmounted drive is visible
    # instead of silently looking like a clean library.
    stale = sum(1 for p in known if p.startswith(str(root)) and not os.path.exists(p))

    write(f"on_disk={len(disk)} known={len(known)} missing={len(missing)} stale_rows={stale}")
    for sample in missing[:10]:
        write(f"  MISSING {sample}")

    if args.dry_run or not missing:
        write("DRY RUN — nothing ingested" if args.dry_run else "nothing to fill")
        return 0

    wanted = set(missing[:args.limit] if args.limit else missing)
    write(f"INGEST BEGIN files={len(wanted)}")
    try:
        stats = folder_scan.scan_and_ingest_folder(
            root,
            use_fingerprint=not args.no_fingerprint,
            classify_audio=not args.no_classify,
            resolve_streaming=args.online,
            dry_run=False,
            only_paths=wanted,
            progress=progress,
        )
    except Exception as exc:
        log.exception("gap fill failed")
        write(f"FATAL {type(exc).__name__}: {exc}")
        return 1

    write("INGEST COMPLETE " + " ".join(
        f"{key}={stats.get(key)}" for key in
        ("seen", "processed", "fingerprinted", "classified", "sourced", "errors")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
