#!/usr/bin/env python
"""Back up and restore the audio-derived vectors held in OpenSearch.

Text vectors are cheap: the lyrics are in Postgres, so `tracks` and
`tracks-lines` can be rebuilt from the store whenever they are lost. Audio
vectors cannot. A CLAP embedding needs the audio file and a GPU pass, and on
this library 244 of 246 embedded tracks no longer have their audio: 170 point
at a local file that is gone and 74 never had a local source at all. Losing
`karaoke-clap` or `karaoke-audio` means losing "sounds like" and `/mood` for
those tracks permanently, with no way to regenerate them.

They are also small -- together well under a megabyte -- so there is no reason
not to keep a copy.

Two destinations, because they protect against different things:

* **Postgres** (`vector_backup` table) survives an OpenSearch wipe or reindex,
  which is the likely accident, and restores in one command.
* **A portable JSONL file** survives losing the machine. Postgres has no
  backup of its own here, so the database copy alone is not a real backup.

Usage::

    python scripts/vector_backup.py backup           # OpenSearch -> Postgres
    python scripts/vector_backup.py export FILE.jsonl.gz
    python scripts/vector_backup.py verify           # compare the two
    python scripts/vector_backup.py restore          # Postgres -> OpenSearch
    python scripts/vector_backup.py restore --from FILE.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache  # noqa: E402
from karaoke.audio_vector import AUDIO_INDEX  # noqa: E402
from karaoke.clap_vector import CLAP_INDEX  # noqa: E402
from karaoke.key_progression import PROGRESSION_INDEX  # noqa: E402

OPENSEARCH_URL = "http://localhost:9200"

#: Only audio-derived indexes. Text indexes are deliberately excluded: they are
#: rebuildable from Postgres and are two orders of magnitude larger.
INDEXES = {
    CLAP_INDEX: "clap_vector",
    AUDIO_INDEX: "audio_vector",
    PROGRESSION_INDEX: "progression_vector",
}

PAGE = 500


def _os_request(path: str, body: dict[str, Any] | None = None,
                method: str = "GET") -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{OPENSEARCH_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def ensure_schema(conn) -> None:
    """Create the backup table.

    The whole `_source` is kept as JSONB rather than a typed vector column so a
    restore reproduces the document exactly, including the metadata the search
    code reads back (artist, title, bpm, detected_key). It also means a change
    to the embedding dimension needs no migration.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS vector_backup (
            index_name   TEXT NOT NULL,
            doc_id       TEXT NOT NULL,
            track_id     INTEGER,
            dim          INTEGER,
            payload      JSONB NOT NULL,
            backed_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (index_name, doc_id)
        );
        CREATE INDEX IF NOT EXISTS idx_vector_backup_track
            ON vector_backup(track_id);
        """
    )
    conn.commit()


def scan_index(index: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield every (doc_id, _source) in an index, paged by search_after.

    search_after rather than scroll: these indexes are small, and a sorted
    walk needs no server-side cursor to clean up if this script dies.
    """
    after = None
    while True:
        body: dict[str, Any] = {"size": PAGE, "sort": [{"_id": "asc"}]}
        if after:
            body["search_after"] = after
        try:
            hits = _os_request(f"/{index}/_search", body)["hits"]["hits"]
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"  {index}: index does not exist, skipping")
                return
            raise
        if not hits:
            return
        for h in hits:
            yield h["_id"], h["_source"]
        after = hits[-1]["sort"]
        if len(hits) < PAGE:
            return


def cmd_backup(args: argparse.Namespace) -> int:
    total = 0
    with localcache.connect() as conn:
        ensure_schema(conn)
        for index, vector_field in INDEXES.items():
            count = 0
            for doc_id, src in scan_index(index):
                vec = src.get(vector_field)
                if not isinstance(vec, list) or not vec:
                    # A document without its vector is worthless as a backup
                    # and would silently restore a broken doc.
                    print(f"  ! {index}/{doc_id} has no {vector_field}, skipped")
                    continue
                conn.execute(
                    """
                    INSERT INTO vector_backup
                        (index_name, doc_id, track_id, dim, payload, backed_up_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (index_name, doc_id) DO UPDATE SET
                        track_id = EXCLUDED.track_id,
                        dim      = EXCLUDED.dim,
                        payload  = EXCLUDED.payload,
                        backed_up_at = now()
                    """,
                    (index, doc_id, src.get("track_id"), len(vec), json.dumps(src)),
                )
                count += 1
            conn.commit()
            print(f"  {index}: {count} vectors backed up")
            total += count
    print(f"backed up {total} vectors to Postgres")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Write the Postgres copy out as portable JSONL.

    Postgres is not itself backed up on this host, so a database-only copy
    protects against an OpenSearch accident but not against losing the disk.
    """
    path = Path(args.path)
    opener = gzip.open if path.name.endswith(".gz") else open
    written = 0
    with localcache.connect() as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT index_name, doc_id, track_id, dim, payload FROM vector_backup"
            " ORDER BY index_name, doc_id"
        ).fetchall()
    with opener(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "_meta": "karaoke vector backup",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(rows),
        }) + "\n")
        for r in rows:
            fh.write(json.dumps({
                "index_name": r["index_name"],
                "doc_id": r["doc_id"],
                "track_id": r["track_id"],
                "dim": r["dim"],
                "payload": r["payload"],
            }) + "\n")
            written += 1
    size = path.stat().st_size
    print(f"exported {written} vectors to {path} ({size/1024:.1f} KB)")
    if written == 0:
        print("  ! nothing exported -- run `backup` first")
        return 1
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Compare live OpenSearch against the Postgres copy, per index."""
    rc = 0
    with localcache.connect() as conn:
        ensure_schema(conn)
        for index in INDEXES:
            live = {doc_id for doc_id, _ in scan_index(index)}
            saved = {
                r["doc_id"] for r in conn.execute(
                    "SELECT doc_id FROM vector_backup WHERE index_name = %s", (index,)
                ).fetchall()
            }
            missing = live - saved
            stale = saved - live
            status = "OK" if not missing else "STALE"
            if missing:
                rc = 1
            print(f"  {index}: live={len(live)} backed_up={len(saved)} "
                  f"not_backed_up={len(missing)} only_in_backup={len(stale)}  [{status}]")
            if stale:
                print(f"    ({len(stale)} in the backup are gone from OpenSearch — "
                      "these are what a restore would bring back)")
    return rc


def _load_file(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.name.endswith(".gz") else open
    out = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if "_meta" in rec:
                continue
            out.append(rec)
    return out


def cmd_restore(args: argparse.Namespace) -> int:
    if args.source:
        records = _load_file(Path(args.source))
        print(f"restoring from {args.source} ({len(records)} vectors)")
    else:
        with localcache.connect() as conn:
            ensure_schema(conn)
            records = [dict(r) for r in conn.execute(
                "SELECT index_name, doc_id, payload FROM vector_backup"
            ).fetchall()]
        print(f"restoring from Postgres ({len(records)} vectors)")

    if not records:
        print("  nothing to restore")
        return 1

    if args.dry_run:
        by_index: dict[str, int] = {}
        for r in records:
            by_index[r["index_name"]] = by_index.get(r["index_name"], 0) + 1
        for idx, n in sorted(by_index.items()):
            print(f"  would restore {n} docs into {idx}")
        return 0

    # Bulk in chunks: one request per index keeps the payload modest and makes
    # a partial failure easy to read in the response.
    restored = 0
    for index in INDEXES:
        docs = [r for r in records if r["index_name"] == index]
        if not docs:
            continue
        for start in range(0, len(docs), 100):
            chunk = docs[start:start + 100]
            lines = []
            for r in chunk:
                payload = r["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                lines.append(json.dumps({"index": {"_index": index, "_id": r["doc_id"]}}))
                lines.append(json.dumps(payload))
            body = "\n".join(lines) + "\n"
            req = urllib.request.Request(
                f"{OPENSEARCH_URL}/_bulk", data=body.encode(), method="POST",
                headers={"Content-Type": "application/x-ndjson"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.load(resp)
            if result.get("errors"):
                first = next((i for i in result["items"] if "error" in i.get("index", {})), None)
                print(f"  ! {index}: bulk reported errors, first: {first}")
                return 1
            restored += len(chunk)
        print(f"  {index}: {len(docs)} docs restored")
    print(f"restored {restored} vectors")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="copy vectors from OpenSearch into Postgres")

    p_exp = sub.add_parser("export", help="write the Postgres copy to a portable file")
    p_exp.add_argument("path", help="destination, .jsonl or .jsonl.gz")

    sub.add_parser("verify", help="compare OpenSearch against the backup")

    p_res = sub.add_parser("restore", help="write vectors back into OpenSearch")
    p_res.add_argument("--from", dest="source", default=None,
                       help="restore from a file instead of Postgres")
    p_res.add_argument("--dry-run", action="store_true",
                       help="report what would be restored and stop")

    args = parser.parse_args(argv)
    return {
        "backup": cmd_backup,
        "export": cmd_export,
        "verify": cmd_verify,
        "restore": cmd_restore,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
