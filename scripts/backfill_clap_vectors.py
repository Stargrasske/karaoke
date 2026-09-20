#!/usr/bin/env python
"""Embed local audio into the CLAP index, and keep the vector this time.

A full-library labelling pass left roughly 13k `clap-zero-shot` genre labels
but only 246 searchable vectors, because the labelling path computed an
embedding, read a genre word off it, and dropped it (fixed in sample_audio).
Decoding the audio and running CLAP is the entire cost; classifying and
indexing are free by comparison. This script pays that cost once more for the
audio that still exists, and stores the result.

Candidates come from two places:

* **local source rows** whose file is still on disk
* **the YouTube audio cache**, whose filenames are video ids and so map
  straight back to `sources.url`

Already-embedded tracks are skipped, so the run is resumable -- interrupt it
and start again. Use ``--limit`` to bound a first pass, and ``--dry-run`` to
see the work and the bytes without touching the GPU.

Usage::

    python scripts/backfill_clap_vectors.py --dry-run
    python scripts/backfill_clap_vectors.py --limit 20
    python scripts/backfill_clap_vectors.py --source cache
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache  # noqa: E402

OPENSEARCH_URL = "http://localhost:9200"
YT_CACHE = Path.home() / ".local/share/karaoke/youtube"
_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")


def _source_to_path(url: str | None) -> Path | None:
    """`sources.url` holds file:// URIs for local rows, not bare paths."""
    if not url:
        return None
    if url.startswith("file://"):
        return Path(unquote(urlparse(url).path))
    if url.startswith("/"):
        return Path(url)
    return None


def indexed_track_ids(index: str) -> set[int]:
    """Track ids already present in an index.

    Checked per index rather than once for the whole run: a track embedded
    before progressions existed has a CLAP vector and no progression, and
    treating "already embedded" as "already done" would skip it forever.
    """
    out: set[int] = set()
    after = None
    while True:
        body: dict = {"size": 500, "_source": ["track_id"], "sort": [{"track_id": "asc"}]}
        if after:
            body["search_after"] = after
        req = urllib.request.Request(
            f"{OPENSEARCH_URL}/{index}/_search",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            hits = json.load(urllib.request.urlopen(req, timeout=30))["hits"]["hits"]
        except Exception:
            return out
        if not hits:
            return out
        out.update(h["_source"]["track_id"] for h in hits)
        after = hits[-1]["sort"]
        if len(hits) < 500:
            return out


def discover(source: str) -> dict[int, Path]:
    """Map track_id -> playable audio file, preferring local originals."""
    found: dict[int, Path] = {}

    with localcache.connect() as conn:
        if source in ("all", "local"):
            for row in conn.execute(
                "SELECT track_id, url FROM sources WHERE kind = 'local'"
            ).fetchall():
                path = _source_to_path(row["url"])
                if path and path.exists() and row["track_id"] not in found:
                    found[row["track_id"]] = path

        if source in ("all", "cache") and YT_CACHE.is_dir():
            cached = {p.stem: p for p in YT_CACHE.iterdir() if p.is_file()}
            for row in conn.execute(
                "SELECT track_id, url FROM sources WHERE url LIKE %s", ("%youtu%",)
            ).fetchall():
                match = _VIDEO_ID.search(row["url"] or "")
                if match and match.group(1) in cached and row["track_id"] not in found:
                    found[row["track_id"]] = cached[match.group(1)]

    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", choices=["all", "local", "cache"], default="all",
                        help="where to look for audio (default: all)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after this many tracks (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the work and the bytes, embed nothing")
    parser.add_argument("--no-genre", action="store_true",
                        help="skip genre labelling (it is nearly free -- reuses the vector)")
    parser.add_argument("--no-progression", action="store_true",
                        help="skip harmonic-progression analysis")
    args = parser.parse_args(argv)

    candidates = discover(args.source)
    have_clap = indexed_track_ids("karaoke-clap")
    have_prog = set() if args.no_progression else indexed_track_ids("karaoke-progression")

    need_clap = {t for t in candidates if t not in have_clap}
    need_prog = set() if args.no_progression else {t for t in candidates if t not in have_prog}
    todo = {t: p for t, p in candidates.items() if t in need_clap or t in need_prog}

    print(f"candidates with audio : {len(candidates)}")
    print(f"  need a CLAP vector  : {len(need_clap)}")
    print(f"  need a progression  : {len(need_prog)}")
    print(f"  to process          : {len(todo)}  (each does only what it is missing)")

    if args.limit:
        todo = dict(list(todo.items())[:args.limit])
        print(f"limited to            : {len(todo)}")
    if not todo:
        print("nothing to do")
        return 0

    total_bytes = sum(p.stat().st_size for p in todo.values())
    print(f"audio to decode       : {total_bytes / 1e9:.2f} GB")

    if args.dry_run:
        for tid, path in list(todo.items())[:10]:
            print(f"  would embed track {tid}: {path.name}")
        if len(todo) > 10:
            print(f"  … and {len(todo) - 10} more")
        return 0

    from karaoke import clap_vector, genre, key_progression
    from karaoke.osclient import client as get_os_client

    if not clap_vector.available():
        print("CLAP model unavailable — nothing to do", file=sys.stderr)
        return 1

    os_client = get_os_client()
    if os_client is None:
        print("OpenSearch unavailable — refusing to embed with nowhere to store it",
              file=sys.stderr)
        return 1
    clap_vector.ensure_index(os_client)

    labels = None if args.no_genre else genre.label_vectors()
    ok = failed = labelled = progressions = 0
    started = time.time()

    with localcache.connect() as conn:
        for n, (track_id, path) in enumerate(todo.items(), 1):
            # Only the missing half is recomputed. Re-embedding a track that
            # already has a vector would also overwrite it with one made from
            # whichever copy of the audio happens to be on disk now, which may
            # be worse than the one it replaces.
            vector = None
            if track_id in need_clap:
                try:
                    vector = clap_vector.embed_audio(str(path))
                except Exception as exc:
                    print(f"  [{n}/{len(todo)}] track {track_id}: embed failed ({exc})")
                    failed += 1
                    continue
                if vector is None:
                    print(f"  [{n}/{len(todo)}] track {track_id}: no vector ({path.name})")
                    failed += 1
                    continue

            row = conn.execute(
                "SELECT t.artist, t.title, t.album, a.detected_key, a.bpm"
                " FROM tracks t LEFT JOIN track_analysis a ON a.track_id = t.track_id"
                " WHERE t.track_id = %s",
                (track_id,),
            ).fetchone()
            meta = dict(row) if row else {}

            if vector is not None:
                doc = clap_vector.build_doc(
                    track_id=track_id,
                    artist=meta.get("artist") or "",
                    title=meta.get("title") or "",
                    album=meta.get("album") or "",
                    vector=vector,
                    embedded_at=datetime.now(timezone.utc).isoformat(),
                    detected_key=meta.get("detected_key") or "",
                    bpm=meta.get("bpm"),
                )
                os_client.index(index=clap_vector.CLAP_INDEX,
                                id=clap_vector.doc_id(track_id), body=doc)
                ok += 1

            # How the key moves across the track. A second decode, but key
            # detection itself is ~0.002x realtime, so it is small next to the
            # embedding that just ran.
            if track_id in need_prog:
                try:
                    prog = key_progression.analyse(str(path))
                    if prog and key_progression.store(
                        track_id, prog,
                        artist=meta.get("artist") or "", title=meta.get("title") or "",
                    ):
                        progressions += 1
                except Exception:
                    pass

            # The vector is already in hand, so a label costs a dot product.
            if labels is not None and vector is not None:
                try:
                    verdict = genre.classify(vector, labels)
                    if verdict is not None:
                        localcache.ensure_genre_table(conn)
                        localcache.record_genre(track_id, verdict, conn,
                                                method="clap-zero-shot")
                        labelled += 1
                except Exception:
                    pass

            rate = n / max(time.time() - started, 1e-6)
            remaining = (len(todo) - n) / rate if rate else 0
            print(f"  [{n}/{len(todo)}] track {track_id} {meta.get('artist','?')[:22]} — "
                  f"{meta.get('title','?')[:26]}  ({rate*60:.1f}/min, ~{remaining/60:.0f}m left)")

    elapsed = time.time() - started
    print(f"\nembedded {ok}, failed {failed}, labelled {labelled}, "
          f"progressions {progressions} in {elapsed/60:.1f} min")
    print("run `python scripts/vector_backup.py backup` to protect the new vectors")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
