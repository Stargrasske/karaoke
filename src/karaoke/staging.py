"""Unapproved/staged lyrics store.

Lower-trust lyrics sources (YouTube captions, web crawls, Whisper transcripts)
should not be written straight into the approved lyrics cache/index. This module
keeps them in a review queue inside the local SQLite DB. A reviewed item can then
be approved into the normal local lyrics cache.
"""
from __future__ import annotations

import psycopg
from psycopg import Connection, Cursor
import time
from dataclasses import dataclass
from typing import Optional

from .lyrics import Lyrics, parse_lrc
from . import localcache

_SCHEMA = """
CREATE TABLE IF NOT EXISTS staged_lyrics (
    id            SERIAL PRIMARY KEY,
    key           TEXT NOT NULL,
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    album         TEXT DEFAULT '',
    duration      REAL,
    source_kind   TEXT NOT NULL,      -- youtube_caption | web | whisper
    source_url    TEXT DEFAULT '',
    confidence    REAL DEFAULT 0.0,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    plain_lyrics  TEXT DEFAULT '',
    synced_lyrics TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_staged_lyrics_status ON staged_lyrics(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_staged_lyrics_key ON staged_lyrics(key);
"""


@dataclass(frozen=True)
class StagedLyrics:
    """One unapproved lyrics candidate."""

    id: int
    artist: str
    title: str
    album: str
    duration: Optional[float]
    source_kind: str
    source_url: str
    confidence: float
    status: str
    plain_lyrics: str
    synced_lyrics: str
    notes: str
    created_at: float
    updated_at: float

    @property
    def has_synced(self) -> bool:
        return bool(parse_lrc(self.synced_lyrics))


def _row_to_item(row: dict) -> StagedLyrics:
    return StagedLyrics(
        id=int(row["id"]),
        artist=row["artist"] or "",
        title=row["title"] or "",
        album=row["album"] or "",
        duration=row["duration"],
        source_kind=row["source_kind"] or "",
        source_url=row["source_url"] or "",
        confidence=float(row["confidence"] or 0.0),
        status=row["status"] or "pending",
        plain_lyrics=row["plain_lyrics"] or "",
        synced_lyrics=row["synced_lyrics"] or "",
        notes=row["notes"] or "",
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def ensure_schema(conn: Connection) -> None:
    """Create staged-lyrics tables in the existing local cache DB.

    Also enforces one row per (track, source): staging used to be a bare
    INSERT, so re-staging the same track appended another identical row and the
    review queue filled with copies of one song. Existing duplicates are folded
    into the newest row before the unique index is added, since the index cannot
    be created while they are present.
    """
    conn.execute(_SCHEMA)
    conn.execute(
        """
        DELETE FROM staged_lyrics
        WHERE id NOT IN (
            SELECT max(id) FROM staged_lyrics GROUP BY key, source_kind
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_staged_lyrics_unique "
        "ON staged_lyrics(key, source_kind)"
    )
    conn.commit()


def stage_lyrics(
    artist: str,
    title: str,
    lyrics: Lyrics,
    *,
    album: str = "",
    duration: Optional[float] = None,
    source_kind: str,
    source_url: str = "",
    confidence: float = 0.0,
    notes: str = "",
    conn: Optional[Connection] = None,
) -> int:
    """Stage a lower-trust lyrics candidate and return its staging id.

    Upserts on (track, source): re-staging the same track from the same source
    refreshes that candidate rather than adding another copy to the queue.

    An existing decision is preserved — a candidate you already rejected stays
    rejected and does not silently reappear as pending just because something
    staged it again.
    """
    if not (lyrics.synced_raw or lyrics.plain):
        raise ValueError("cannot stage empty lyrics")
    own = conn is None
    c = conn or localcache.connect()
    ensure_schema(c)
    now = time.time()
    try:
        c.execute(
            """
            INSERT INTO staged_lyrics
                (key, artist, title, album, duration, source_kind, source_url,
                 confidence, status, plain_lyrics, synced_lyrics, notes,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s)
            ON CONFLICT(key, source_kind) DO UPDATE SET
                artist        = excluded.artist,
                title         = excluded.title,
                album         = excluded.album,
                duration      = excluded.duration,
                source_url    = excluded.source_url,
                confidence    = excluded.confidence,
                plain_lyrics  = excluded.plain_lyrics,
                synced_lyrics = excluded.synced_lyrics,
                notes         = excluded.notes,
                updated_at    = excluded.updated_at
            """,
            (
                localcache._key(artist, title), artist, title, album, duration,
                source_kind, source_url, confidence, lyrics.plain,
                lyrics.synced_raw, notes, now, now,
            ),
        )
        c.commit()
        row = c.execute(
            "SELECT id FROM staged_lyrics WHERE key = %s AND source_kind = %s",
            (localcache._key(artist, title), source_kind),
        ).fetchone()
        if row is None:
            raise RuntimeError("staged lyrics upsert did not yield an id")
        return int(row["id"])
    finally:
        if own:
            c.close()


def list_staged(
    *,
    status: str = "pending",
    limit: int = 20,
    conn: Optional[Connection] = None,
) -> list[StagedLyrics]:
    """List staged lyric candidates newest-first."""
    own = conn is None
    c = conn or localcache.connect()
    ensure_schema(c)
    try:
        rows = c.execute(
            """
            SELECT * FROM staged_lyrics
            WHERE (%s = 'all' OR status = %s)
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
            """,
            (status, status, limit),
        ).fetchall()
        return [_row_to_item(r) for r in rows]
    finally:
        if own:
            c.close()


def get_staged(item_id: int, *, conn: Optional[Connection] = None) -> Optional[StagedLyrics]:
    """Return one staged lyrics candidate by id."""
    own = conn is None
    c = conn or localcache.connect()
    ensure_schema(c)
    try:
        row = c.execute("SELECT * FROM staged_lyrics WHERE id = %s", (item_id,)).fetchone()
        return _row_to_item(row) if row else None
    finally:
        if own:
            c.close()


def approve_staged(item_id: int, *, conn: Optional[Connection] = None) -> StagedLyrics:
    """Mark a staged candidate approved and copy it into the local lyrics cache."""
    own = conn is None
    c = conn or localcache.connect()
    ensure_schema(c)
    try:
        item = get_staged(item_id, conn=c)
        if item is None:
            raise KeyError(f"no staged lyrics with id {item_id}")
        lyrics = Lyrics(
            plain=item.plain_lyrics,
            synced_raw=item.synced_lyrics,
            source=item.source_kind,
            lines=parse_lrc(item.synced_lyrics) if item.synced_lyrics else [],
        )
        localcache.put_cached_lyrics(
            item.artist, item.title, lyrics,
            album=item.album, duration=item.duration, conn=c,
        )
        c.execute(
            "UPDATE staged_lyrics SET status = 'approved', updated_at = %s WHERE id = %s",
            (time.time(), item_id),
        )
        c.commit()
        updated = get_staged(item_id, conn=c)
        assert updated is not None
        return updated
    finally:
        if own:
            c.close()


def reject_staged(item_id: int, *, conn: Optional[Connection] = None) -> StagedLyrics:
    """Mark a staged candidate rejected without touching the approved cache."""
    own = conn is None
    c = conn or localcache.connect()
    ensure_schema(c)
    try:
        item = get_staged(item_id, conn=c)
        if item is None:
            raise KeyError(f"no staged lyrics with id {item_id}")
        c.execute(
            "UPDATE staged_lyrics SET status = 'rejected', updated_at = %s WHERE id = %s",
            (time.time(), item_id),
        )
        c.commit()
        updated = get_staged(item_id, conn=c)
        assert updated is not None
        return updated
    finally:
        if own:
            c.close()


def whitelist_staged(item_id: int, *, conn: Optional[Connection] = None) -> StagedLyrics:
    """Whitelist a staged candidate: approve it into the working lyrics cache.

    Alias for :func:`approve_staged` with the domain term the TUI uses — once
    whitelisted, the track carries approved lyrics and shows in the working list.
    """
    return approve_staged(item_id, conn=conn)


def find_pending_by_track(
    artist: str, title: str, *, conn: Optional[Connection] = None
) -> Optional[StagedLyrics]:
    """Return the newest non-rejected staged candidate for a track, if any."""
    own = conn is None
    c = conn or localcache.connect()
    ensure_schema(c)
    try:
        row = c.execute(
            """
            SELECT * FROM staged_lyrics
            WHERE key = %s AND status != 'rejected'
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (localcache._key(artist, title),),
        ).fetchone()
        return _row_to_item(row) if row else None
    finally:
        if own:
            c.close()
