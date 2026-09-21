"""Persistent per-track musical analysis (key + tempo) and key verification.

Stores detected/reference key and BPM in a dedicated ``track_analysis`` table so
the existing ``tracks`` schema is untouched. The reconciliation logic lives in
``musictheory`` (pure); this module is the SQLite + orchestration layer.
"""
from __future__ import annotations

import psycopg
from psycopg import Connection, Cursor
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import localcache
from .musictheory import Key, KeyReconciliation, parse_key, reconcile_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_analysis (
    track_id        INTEGER PRIMARY KEY,
    detected_key    TEXT DEFAULT '',   -- e.g. "A minor"
    key_confidence  REAL DEFAULT 0.0,
    key_agreement   TEXT DEFAULT '',   -- e.g. "4/6"
    reference_key   TEXT DEFAULT '',   -- online/human-verified key
    reference_src   TEXT DEFAULT '',   -- where the reference came from
    resolved_key    TEXT DEFAULT '',   -- the key we trust after reconciliation
    key_relation    TEXT DEFAULT '',   -- exact | relative | parallel | conflict
    bpm             REAL,
    method          TEXT DEFAULT '',
    energy          REAL,
    brightness      REAL,
    analyzer_version INTEGER DEFAULT 0,
    updated_at      REAL NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS track_analysis_history (
    history_id      SERIAL PRIMARY KEY,
    track_id        INTEGER NOT NULL,
    source_kind     TEXT DEFAULT '',
    detected_key    TEXT DEFAULT '',
    key_confidence  REAL DEFAULT 0.0,
    key_agreement   TEXT DEFAULT '',
    bpm             REAL,
    method          TEXT DEFAULT '',
    energy          REAL,
    brightness      REAL,
    analyzer_version INTEGER DEFAULT 0,
    created_at      REAL NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);
"""

# Columns added after the initial schema; applied idempotently on connect so
# existing databases pick them up without a manual migration.
_ADDED_COLUMNS = {
    "energy": "REAL",
    "brightness": "REAL",
}


@dataclass(frozen=True)
class TrackAnalysis:
    """A stored analysis row for one track."""

    track_id: int
    detected_key: Optional[Key]
    key_confidence: float
    key_agreement: str
    reference_key: Optional[Key]
    reference_src: str
    resolved_key: Optional[Key]
    key_relation: str
    bpm: Optional[float]
    method: str
    analyzer_version: int
    updated_at: float
    energy: Optional[float] = None
    brightness: Optional[float] = None


def ensure_schema(conn: Connection) -> None:
    """Create the analysis table (and add newer columns) in the local cache DB."""
    conn.execute(_SCHEMA)
    existing = {
        row["name"] for row in conn.execute("SELECT column_name as name FROM information_schema.columns WHERE table_name = %s", ("track_analysis",))
    }
    for col, coltype in _ADDED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE track_analysis ADD COLUMN {col} {coltype}")
    history_count = conn.execute("SELECT COUNT(*) FROM track_analysis_history").fetchone()["count"]
    if history_count == 0:
        conn.execute(
            """
            INSERT INTO track_analysis_history
                (track_id, source_kind, detected_key, key_confidence, key_agreement,
                 bpm, method, energy, brightness, analyzer_version, created_at)
            SELECT track_id, COALESCE(NULLIF(method, ''), 'baseline'), detected_key,
                   key_confidence, key_agreement, bpm, method, energy, brightness,
                   analyzer_version, updated_at
            FROM track_analysis
            """
        )
    conn.commit()


def _row_to_analysis(row: dict) -> TrackAnalysis:
    keys = row.keys()
    return TrackAnalysis(
        track_id=int(row["track_id"]),
        detected_key=parse_key(row["detected_key"] or ""),
        key_confidence=float(row["key_confidence"] or 0.0),
        key_agreement=row["key_agreement"] or "",
        reference_key=parse_key(row["reference_key"] or ""),
        reference_src=row["reference_src"] or "",
        resolved_key=parse_key(row["resolved_key"] or ""),
        key_relation=row["key_relation"] or "",
        bpm=row["bpm"],
        method=row["method"] or "",
        analyzer_version=int(row["analyzer_version"] or 0),
        updated_at=float(row["updated_at"] or 0.0),
        energy=row["energy"] if "energy" in keys else None,
        brightness=row["brightness"] if "brightness" in keys else None,
    )


def get_analysis(track_id: int, conn: Connection) -> Optional[TrackAnalysis]:
    """Return the stored analysis for a track, or None."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM track_analysis WHERE track_id = %s", (track_id,)
    ).fetchone()
    return _row_to_analysis(row) if row else None


def _key_name(key: Optional[Key]) -> str:
    return key.name if key else ""


def save_detected(
    track_id: int,
    *,
    detected_key: Optional[Key],
    key_confidence: float = 0.0,
    key_agreement: str = "",
    bpm: Optional[float] = None,
    method: str = "",
    analyzer_version: int = 0,
    energy: Optional[float] = None,
    brightness: Optional[float] = None,
    source_kind: str = "",
    conn: Connection,
) -> TrackAnalysis:
    """Upsert a locally-detected key/tempo/energy analysis and re-reconcile.

    Preserves any existing reference key so re-running detection keeps the
    reconciliation up to date.
    """
    ensure_schema(conn)
    existing = get_analysis(track_id, conn)
    reference = existing.reference_key if existing else None
    reference_src = existing.reference_src if existing else ""
    rec = reconcile_key(detected_key, reference)
    now = time.time()
    conn.execute(
        """
        INSERT INTO track_analysis
            (track_id, detected_key, key_confidence, key_agreement,
             reference_key, reference_src, resolved_key, key_relation,
             bpm, method, energy, brightness, analyzer_version, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            detected_key = excluded.detected_key,
            key_confidence = excluded.key_confidence,
            key_agreement = excluded.key_agreement,
            resolved_key = excluded.resolved_key,
            key_relation = excluded.key_relation,
            bpm = COALESCE(excluded.bpm, track_analysis.bpm),
            method = excluded.method,
            energy = COALESCE(excluded.energy, track_analysis.energy),
            brightness = COALESCE(excluded.brightness, track_analysis.brightness),
            analyzer_version = excluded.analyzer_version,
            updated_at = excluded.updated_at
        """,
        (
            track_id, _key_name(detected_key), key_confidence, key_agreement,
            _key_name(reference), reference_src, _key_name(rec.resolved),
            rec.relation, bpm, method, energy, brightness,
            analyzer_version, now,
        ),
    )
    # Record history version entry
    conn.execute(
        """
        INSERT INTO track_analysis_history
            (track_id, source_kind, detected_key, key_confidence, key_agreement,
             bpm, method, energy, brightness, analyzer_version, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            track_id, source_kind or method, _key_name(detected_key), key_confidence, key_agreement,
            bpm, method, energy, brightness, analyzer_version, now,
        ),
    )
    conn.commit()
    result = get_analysis(track_id, conn)
    assert result is not None
    return result


def get_analysis_history(track_id: int, conn: Connection) -> list[dict[str, Any]]:
    """Return all historical analysis versions for a track, newest first."""
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT history_id, track_id, source_kind, detected_key, key_confidence,
               key_agreement, bpm, method, energy, brightness, analyzer_version, created_at
        FROM track_analysis_history
        WHERE track_id = %s
        ORDER BY history_id DESC
        """,
        (track_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "history_id": r["history_id"],
            "track_id": r["track_id"],
            "source_kind": r["source_kind"],
            "detected_key": r["detected_key"],
            "key_confidence": float(r["key_confidence"] or 0.0),
            "key_agreement": r["key_agreement"],
            "bpm": r["bpm"],
            "method": r["method"],
            "energy": r["energy"],
            "brightness": r["brightness"],
            "analyzer_version": r["analyzer_version"],
            "created_at": float(r["created_at"] or 0.0),
        })
    return out


def verify_key(
    track_id: int,
    reference: Key | str,
    *,
    reference_src: str = "online",
    conn: Connection,
) -> KeyReconciliation:
    """Reconcile a stored detected key against an online/reference key.

    This is the "we detected Am but the web says C major" path: relatives are
    recognised as the same tonality. Persists the reference + resolved key and
    returns the reconciliation so callers can explain the outcome.
    """
    ensure_schema(conn)
    ref_key = parse_key(reference) if isinstance(reference, str) else reference
    existing = get_analysis(track_id, conn)
    detected = existing.detected_key if existing else None
    rec = reconcile_key(detected, ref_key)
    conn.execute(
        """
        INSERT INTO track_analysis
            (track_id, reference_key, reference_src, resolved_key,
             key_relation, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            reference_key = excluded.reference_key,
            reference_src = excluded.reference_src,
            resolved_key = excluded.resolved_key,
            key_relation = excluded.key_relation,
            updated_at = excluded.updated_at
        """,
        (
            track_id, _key_name(ref_key), reference_src,
            _key_name(rec.resolved), rec.relation, time.time(),
        ),
    )
    conn.commit()
    return rec
