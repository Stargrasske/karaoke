"""Library and pipeline statistics.

``localcache.summarize`` already covers listening history (plays, cache hits,
top artists). This adds the other half — what the library actually *contains*
and how far the processing pipeline has got — as plain data, so the TUI, a CLI
or the API can all render the same numbers.

Every query is read-only and returns totals rather than rows, so this stays
cheap on a large library.
"""
from __future__ import annotations

import psycopg
from psycopg import Connection, Cursor
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class LibraryStats:
    """A snapshot of library contents and pipeline progress."""

    tracks: int = 0
    with_lyrics: int = 0
    synced: int = 0
    plain_only: int = 0
    sources: int = 0
    staged: int = 0
    analysed: int = 0
    word_timed: int = 0
    lyric_sources: list[tuple[str, int]] = field(default_factory=list)
    gaps: list[tuple[str, int]] = field(default_factory=list)
    keys: list[tuple[str, int]] = field(default_factory=list)
    tempo_bands: list[tuple[str, int]] = field(default_factory=list)

    @property
    def unanalysed(self) -> int:
        """Tracks with no key/BPM yet — the post-processing backlog."""
        return max(0, self.tracks - self.analysed)

    @property
    def synced_share(self) -> float:
        """Fraction of the library that can actually drive a karaoke session."""
        return self.synced / self.tracks if self.tracks else 0.0


def _scalar(conn: Connection, sql: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
    except psycopg.Error:
        return 0
    return int(list(row.values())[0] or 0) if row else 0


def _pairs(conn: Connection, sql: str) -> list[tuple[str, int]]:
    try:
        return [(str(list(r.values())[0]), int(list(r.values())[1])) for r in conn.execute(sql) if list(r.values())[0] is not None]
    except psycopg.Error:
        return []


def collect(conn: Optional[Connection] = None) -> LibraryStats:
    """Gather library and pipeline statistics."""
    from . import localcache

    own = conn is None
    c = conn or localcache.connect()
    try:
        approved = "FROM lyrics WHERE kind = 'approved'"
        return LibraryStats(
            tracks=_scalar(c, "SELECT count(*) FROM tracks"),
            with_lyrics=_scalar(c, f"SELECT count(DISTINCT track_id) {approved}"),
            synced=_scalar(c, f"SELECT count(DISTINCT track_id) {approved}"
                              " AND length(COALESCE(synced_lyrics,'')) > 0"),
            plain_only=_scalar(c, f"SELECT count(DISTINCT track_id) {approved}"
                                  " AND length(COALESCE(synced_lyrics,'')) = 0"
                                  " AND length(COALESCE(plain_lyrics,'')) > 0"),
            sources=_scalar(c, "SELECT count(*) FROM sources"),
            staged=_scalar(c, "SELECT count(*) FROM staged_lyrics"),
            analysed=_scalar(c, "SELECT count(*) FROM track_analysis"
                                " WHERE bpm IS NOT NULL"),
            # Enhanced LRC carries per-word tags in angle brackets.
            word_timed=_scalar(c, f"SELECT count(*) {approved}"
                                  " AND synced_lyrics LIKE '%%<%%'"),
            lyric_sources=_pairs(c, f"SELECT source, count(*) n {approved}"
                                    " GROUP BY source ORDER BY n DESC"),
            gaps=_pairs(c, "SELECT status, count(*) n FROM lyric_gaps"
                           " GROUP BY status ORDER BY n DESC"),
            keys=_pairs(c, "SELECT detected_key, count(*) n FROM track_analysis"
                           " WHERE detected_key IS NOT NULL AND bpm IS NOT NULL"
                           " GROUP BY detected_key ORDER BY n DESC LIMIT 8"),
            tempo_bands=_pairs(c, """
                SELECT CASE
                    WHEN bpm < 70  THEN 'largo   <70'
                    WHEN bpm < 100 THEN 'andante 70-99'
                    WHEN bpm < 130 THEN 'moderato 100-129'
                    WHEN bpm < 160 THEN 'allegro 130-159'
                    ELSE                'presto  160+'
                END band, count(*) n
                FROM track_analysis WHERE bpm IS NOT NULL
                GROUP BY band ORDER BY min(bpm)
            """),
        )
    finally:
        if own:
            c.close()


def bar(value: int, total: int, width: int = 12) -> str:
    """A proportional ASCII bar.

    ASCII rather than block characters: the same reason the sentiment bars use
    it — block glyphs are East-Asian ambiguous and can shift a column.
    """
    if total <= 0 or width <= 0:
        return " " * width
    filled = max(0, min(width, round(width * value / total)))
    return "#" * filled + "." * (width - filled)
