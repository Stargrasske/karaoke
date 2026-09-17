"""Cluster-independent local cache + play/discovery stats (SQLite).

The OpenSearch index on the kind cluster is the rich search/index store, but it
is only available when the cluster is running. This module adds a small, always-
available SQLite database (``~/.local/share/karaoke/karaoke.db`` by default) that:

- Caches lyrics and track metadata.
- Records every play/identification event so ``karaoke-stats`` can report play
  counts, top artists, and radio-discovery stats.
"""
from __future__ import annotations

import re
import psycopg
from psycopg import Connection, Cursor
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import settings
from .logger import log
from .lyrics import Lyrics, clean_artist, clean_page_title, parse_lrc

_IS_PG = True

_NEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id    SERIAL PRIMARY KEY,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    album       TEXT,
    duration    REAL,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id   SERIAL PRIMARY KEY,
    track_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    url         TEXT UNIQUE,
    player_name TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS lyrics (
    lyric_id        SERIAL PRIMARY KEY,
    track_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'approved', -- approved | staged | rejected
    source          TEXT, -- lrclib | youtube_caption | whisper | user_submitted
    synced_lyrics   TEXT,
    plain_lyrics    TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS lyric_gaps (
    gap_id          SERIAL PRIMARY KEY,
    artist          TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | processed | failed
    created_at      REAL NOT NULL,
    processed_at    REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS recordings (
    recording_id SERIAL PRIMARY KEY,
    started_at   REAL NOT NULL,
    ended_at     REAL,
    source       TEXT NOT NULL,
    dir          TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'recording',
                 -- recording | complete | analysed | discarded | failed
    keep_audio   INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS recording_marks (
    mark_id      SERIAL PRIMARY KEY,
    recording_id INTEGER NOT NULL,
    at_wall      REAL NOT NULL,   -- when the identification landed
    at_mono      REAL,            -- monotonic pair, so clock drift is measurable
    at_offset    REAL,            -- position within the track, per songrec
    artist       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    -- Failed identifications are stored rather than dropped: a gap in the marks
    -- is evidence about the recording (silence, speech, an unknown track), and
    -- discarding it makes the timeline look continuous when it is not.
    ok           INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
);

CREATE INDEX IF NOT EXISTS idx_recording_marks_rec
    ON recording_marks (recording_id, at_wall);

CREATE TABLE IF NOT EXISTS restricted_tracks (
    track_id   INTEGER PRIMARY KEY,
    -- What the fetcher was asked for when it hit the wall, so a later pass can
    -- retry the same thing rather than guess.
    query      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL DEFAULT '',
    noted_at   REAL NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS spotify_lookups (
    track_id   INTEGER PRIMARY KEY,
    -- NULL means Spotify was asked and had no match. That is a real result and
    -- must be remembered: without it, every track Spotify does not carry is
    -- re-searched on every play, and search is the rate-limited endpoint.
    uri        TEXT,
    checked_at REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS track_sync_offsets (
    track_id    INTEGER PRIMARY KEY,
    offset_s    REAL NOT NULL,
    updated_at  REAL NOT NULL,
    -- Which clock the offset was tuned against: 'radio' (dead-reckoned from
    -- songrec, carries DEFAULT_LEAD_S) or 'player' (MPRIS position, no lead).
    -- An offset is only valid for its own clock; see offset_mode().
    mode        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

-- Text about a track that is not its lyrics. The lyrics panel renders the
-- artist biography in the same element as the words, so a reader that only
-- decides "lyrics / not lyrics" throws away a band history it already fetched
-- -- which is what happened to four Wizards of Ooze tracks. Classifying is
-- cheap; the classification is what belongs in a column.
CREATE TABLE IF NOT EXISTS track_notes (
    note_id    SERIAL PRIMARY KEY,
    track_id   INTEGER NOT NULL,
    kind       TEXT NOT NULL,   -- biography | transcription | commentary | credits
    text       TEXT NOT NULL,
    source     TEXT NOT NULL,   -- ytmusic_panel | whisper | lrclib | ...
    -- Mean word probability for a transcription; NULL when it does not apply.
    -- A Whisper guess and a LyricFind biography are not equally trustworthy and
    -- a search result should be able to say which it is looking at.
    confidence REAL,
    noted_at   REAL NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE INDEX IF NOT EXISTS idx_track_notes_track ON track_notes (track_id);

-- One note per (track, kind, source): the panel is re-read on every play, and
-- without this a much-played track collects the same biography fifty times.
CREATE UNIQUE INDEX IF NOT EXISTS idx_track_notes_dedup
    ON track_notes (track_id, kind, source);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS play_events (
    id         SERIAL PRIMARY KEY,
    ts         REAL NOT NULL,
    mode       TEXT NOT NULL,          -- radio | spotify | listen | output | file | query | print
    artist     TEXT DEFAULT '',
    title      TEXT DEFAULT '',
    event      TEXT NOT NULL,          -- play | discover | relock | cache_hit | cache_miss | no_lyrics
    source     TEXT DEFAULT '',        -- local | opensearch | lrclib | whisper | none
    has_synced INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_play_events_ts ON play_events (ts);
CREATE INDEX IF NOT EXISTS idx_play_events_track ON play_events (artist, title);
"""


def _key(artist: str, title: str) -> str:
    """Stable case-insensitive artist/title cache key for staging metadata."""
    return f"{artist.strip().casefold()}\u2400{title.strip().casefold()}"


def ensure_gap_columns(conn: Connection) -> None:
    """Add the gap diagnostics columns to an existing DB (idempotent).

    ``attempts`` and ``last_error`` let the backfill runner distinguish a
    transient throttle from a real miss, and make ``failed`` rows retryable
    instead of terminal.
    """
    have = {r["name"] for r in conn.execute("SELECT column_name as name FROM information_schema.columns WHERE table_name = %s", ("lyric_gaps",))}
    if "attempts" not in have:
        conn.execute("ALTER TABLE lyric_gaps ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if "last_error" not in have:
        conn.execute("ALTER TABLE lyric_gaps ADD COLUMN last_error TEXT")
    conn.commit()


def ensure_play_count_column(conn: Connection) -> None:
    """Add tracks.play_count and backfill it from play_events (idempotent).

    Play counts drive the "priority: least played" sort so undiscovered tracks
    surface first. The column is denormalised for cheap sorting; it is kept in
    step by record_event and seeded here from the historical play_events log.
    """
    have = {r["name"] for r in conn.execute("SELECT column_name as name FROM information_schema.columns WHERE table_name = %s", ("tracks",))}
    if not have:
        # No tracks table yet (a database predating it, or a minimal test
        # fixture). Nothing to migrate; the table's own CREATE carries the
        # column when it is finally made.
        return
    if "play_count" not in have:
        conn.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
        # Backfill only when the columns the seed query needs are present. A
        # legacy tracks table can be as minimal as (track_id); referencing
        # artist/title there would crash the migration on connect().
        if {"artist", "title"} <= have:
            conn.execute(
                """
                UPDATE tracks SET play_count = COALESCE((
                    SELECT COUNT(*) FROM play_events p
                    WHERE p.event IN ('play', 'discover')
                      AND LOWER(p.artist) = LOWER(tracks.artist)
                      AND LOWER(p.title) = LOWER(tracks.title)
                ), 0)
                """
            )
        conn.commit()


def normalize_gap_metadata(artist: str, title: str) -> Optional[tuple[str, str]]:
    """Normalize player metadata for the gap queue, or None if unusable.

    Player/YouTube metadata arrives decorated ("Artist - Topic",
    "Song | YouTube Music", "(Official Video)") or degenerate (empty artist,
    a whole-album page title). Such rows can never resolve against LRCLIB, so
    they are rejected here rather than queued and retried forever.
    """
    # Checked against the raw title: clean_page_title strips "(Full Album)" as a
    # decoration, so the reject has to run before it.
    if re.search(r"\b(full\s+album|full\s+set|album\s+completo|mixtape)\b",
                 title or "", re.I):
        return None
    a = clean_artist(clean_page_title(artist or ""))
    t = clean_page_title(title or "")
    if not a or not t:
        return None
    # YouTube titles repeat the artist ("Red Hot Chili Peppers - Suck My Kiss"),
    # which LRCLIB's exact endpoint cannot match. Drop the redundant prefix.
    prefix = re.match(rf"^{re.escape(a)}\s*[-–—:]\s*(?P<rest>.+)$", t, re.I)
    if prefix:
        t = prefix.group("rest").strip()
    if not t:
        return None
    return a, t


def log_lyric_gap(artist: str, title: str, conn: Connection) -> None:
    """Log a song that is missing lyrics.

    Metadata is normalized first; unusable rows are dropped instead of queued.
    """
    cleaned = normalize_gap_metadata(artist, title)
    if cleaned is None:
        log.debug("lyric gap skipped (unusable metadata): %r - %r", artist, title)
        return
    conn.execute(
        "INSERT INTO lyric_gaps (artist, title, created_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (*cleaned, time.time())
    )
    conn.commit()


# How many times a track may be looked up on Spotify before we stop asking. A
# transient failure deserves one retry; more than that and we are just spending
# quota on a song Spotify does not have.
SPOTIFY_LOOKUP_ATTEMPTS = 2


def ensure_recording_tables(conn: Connection) -> None:
    """Create the record-mode tables in databases predating them."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recordings (
            recording_id SERIAL PRIMARY KEY,
            started_at   REAL NOT NULL,
            ended_at     REAL,
            source       TEXT NOT NULL,
            dir          TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'recording',
            keep_audio   INTEGER NOT NULL DEFAULT 0,
            note         TEXT
        );
        CREATE TABLE IF NOT EXISTS recording_marks (
            mark_id      SERIAL PRIMARY KEY,
            recording_id INTEGER NOT NULL,
            at_wall      REAL NOT NULL,
            at_mono      REAL,
            at_offset    REAL,
            artist       TEXT NOT NULL DEFAULT '',
            title        TEXT NOT NULL DEFAULT '',
            ok           INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_recording_marks_rec
            ON recording_marks (recording_id, at_wall);
    """)
    conn.commit()


# The kinds of note that may be stored. Deliberately closed: a notes table with
# an open kind becomes a dumping ground, and text that does not classify is
# still better refused than filed as "other" and never read again.
NOTE_KINDS = ("biography", "transcription", "commentary", "credits")


def ensure_notes_table(conn: Connection) -> None:
    """Create the notes table in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_notes (
            note_id    SERIAL PRIMARY KEY,
            track_id   INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            text       TEXT NOT NULL,
            source     TEXT NOT NULL,
            confidence REAL,
            noted_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_track_notes_track
            ON track_notes (track_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_track_notes_dedup
            ON track_notes (track_id, kind, source);
    """)
    conn.commit()


def record_note(track_id: int, kind: str, text: str, source: str,
                conn: Connection, *,
                confidence: Optional[float] = None) -> bool:
    """Store text about a track that is not its lyrics.

    Returns whether anything was stored. An unknown ``kind`` is refused rather
    than coerced, so a caller that has not decided what it is holding cannot
    quietly file it as prose.

    Re-reading the same source updates in place: the panel is read on every
    play and the biography does not change between them, but it can be read
    truncated while the tab is still rendering, so a longer later read wins.
    """
    if kind not in NOTE_KINDS:
        log.debug("refusing note of unknown kind %r for track %s", kind, track_id)
        return False
    body = (text or "").strip()
    if not body:
        return False
    conn.execute(
        """
        INSERT INTO track_notes (track_id, kind, text, source, confidence, noted_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(track_id, kind, source) DO UPDATE SET
            text       = excluded.text,
            confidence = excluded.confidence,
            noted_at   = excluded.noted_at
        WHERE length(excluded.text) >= length(track_notes.text)
        """,
        (track_id, kind, body, source, confidence, time.time()),
    )
    conn.commit()
    return True


def notes_for_track(track_id: int, conn: Connection) -> list:
    """Every note held about one track, newest first."""
    return conn.execute(
        """
        SELECT note_id, track_id, kind, text, source, confidence, noted_at
        FROM track_notes WHERE track_id = %s
        ORDER BY noted_at DESC, note_id DESC
        """,
        (track_id,),
    ).fetchall()


def iter_note_rows(conn: Connection) -> Iterable[dict]:
    """Yield every note with its track's names, for indexing."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT n.note_id, n.track_id, n.kind, n.text, n.source,
               n.confidence, n.noted_at,
               t.artist, t.title, t.album
        FROM track_notes n
        JOIN tracks t ON t.track_id = n.track_id
        ORDER BY n.note_id
        """
    )
    yield from cur.fetchall()


# Anchored fraction at or above which an alignment's timings are trustworthy.
# Measured on 13 recorded tracks against their known LRC timings: every one of
# the nine at or above this scored 0.24-0.89s of jitter, without exception.
#
# Below it the outcome is *unpredictable*, not bad -- King Buffalo - Locusts
# managed 0.77s on 45% anchored, indistinguishable on every available metric
# from Ministry - Filth Pig at 2.63s. That asymmetry is why this marks rows for
# review and never rejects them: the evidence supports certifying a good
# alignment, not condemning a poor-looking one.
GOOD_ANCHOR_FRACTION = 0.83


def ensure_alignment_support_table(conn: Connection) -> None:
    """Create the alignment support table in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alignment_support (
            track_id     INTEGER PRIMARY KEY,
            lines        INTEGER NOT NULL,
            anchored     INTEGER NOT NULL,
            longest_gap_s REAL,
            unanchored_fraction REAL,
            source       TEXT NOT NULL DEFAULT '',
            noted_at     REAL NOT NULL
        );
    """)
    conn.commit()


def record_alignment_support(track_id: int, report: dict,
                             conn: Connection, *,
                             source: str = "") -> None:
    """Note how well a stored alignment's timings are actually supported.

    Timings that were interpolated between distant anchors are indistinguishable
    from timings taken from heard words once written to an LRC -- GAUPA -
    Febersvan stored lines up to 183 seconds out and looked no different from a
    good result. This keeps the difference.

    Written alongside the alignment rather than derived later, because the
    anchors are gone by then: reproducing them means transcribing the audio
    again, and Whisper is not deterministic, so a later reconstruction would
    not describe the row that is actually stored.
    """
    if not report:
        return
    conn.execute(
        """
        INSERT INTO alignment_support
            (track_id, lines, anchored, longest_gap_s, unanchored_fraction,
             source, noted_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            lines               = excluded.lines,
            anchored            = excluded.anchored,
            longest_gap_s       = excluded.longest_gap_s,
            unanchored_fraction = excluded.unanchored_fraction,
            source              = excluded.source,
            noted_at            = excluded.noted_at
        """,
        (track_id, report.get("lines", 0), report.get("anchored", 0),
         report.get("longest_gap_s"), report.get("unanchored_fraction"),
         source, time.time()),
    )
    conn.commit()


def anchored_fraction(row: Any) -> Optional[float]:
    """Share of a row's lines that were anchored, or None if unknowable."""
    if row is None:
        return None
    lines = row["lines"] or 0
    if lines <= 0:
        return None
    return (row["anchored"] or 0) / lines


def alignment_is_trustworthy(row: Any) -> Optional[bool]:
    """Whether an alignment's timings met the measured bar.

    None when there is nothing recorded — which is not the same as False, and
    covers every alignment stored before this was measured.
    """
    fraction = anchored_fraction(row)
    if fraction is None:
        return None
    return fraction >= GOOD_ANCHOR_FRACTION


def alignment_support(track_id: int, conn: Connection) -> Any:
    """The recorded support for one track's alignment, or None."""
    return conn.execute(
        "SELECT * FROM alignment_support WHERE track_id = %s",
        (track_id,)).fetchone()


def alignments_for_review(conn: Connection,
                          threshold: float = GOOD_ANCHOR_FRACTION) -> list:
    """Stored alignments whose timings are mostly interpolated, worst first.

    A worklist, not a delete list. These are the rows whose timings deserve a
    listen; the words themselves are unaffected either way.
    """
    return conn.execute(
        """
        SELECT s.*, t.artist, t.title,
               CAST(s.anchored AS REAL) / s.lines AS fraction
        FROM alignment_support s
        JOIN tracks t ON t.track_id = s.track_id
        WHERE s.lines > 0 AND CAST(s.anchored AS REAL) / s.lines < %s
        ORDER BY fraction
        """,
        (threshold,)).fetchall()


def ensure_genre_table(conn: Connection) -> None:
    """Create the genre table in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_genre (
            track_id        INTEGER PRIMARY KEY,
            genre           TEXT NOT NULL,
            score           REAL,
            -- Kept because it is often the more informative half: "heavy metal
            -- 0.708, punk rock 0.689" describes sludge better than either
            -- label alone, and a narrow margin is exactly when a reader should
            -- see both.
            runner_up       TEXT NOT NULL DEFAULT '',
            runner_up_score REAL,
            method          TEXT NOT NULL DEFAULT '',
            labelled_at     REAL NOT NULL,
            FOREIGN KEY(track_id) REFERENCES tracks(track_id)
        );
        CREATE INDEX IF NOT EXISTS idx_track_genre_genre ON track_genre (genre);
    """)
    conn.commit()


def record_genre(track_id: int, verdict: Any, conn: Connection, *,
                 method: str = "clap-zero-shot") -> bool:
    """Store a track's genre label. Returns whether anything was written.

    A None verdict writes nothing rather than storing an "unknown" row: the
    absence of a label and a label saying "unknown" read the same to a query
    but mean different things to a re-run, which should retry the first and
    leave the second alone.
    """
    if verdict is None or not getattr(verdict, "genre", ""):
        return False
    conn.execute(
        """
        INSERT INTO track_genre (track_id, genre, score, runner_up,
                                 runner_up_score, method, labelled_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            genre           = excluded.genre,
            score           = excluded.score,
            runner_up       = excluded.runner_up,
            runner_up_score = excluded.runner_up_score,
            method          = excluded.method,
            labelled_at     = excluded.labelled_at
        """,
        (track_id, verdict.genre, verdict.score, verdict.runner_up,
         verdict.runner_up_score, method, time.time()),
    )
    conn.commit()
    return True


def clear_genre(track_id: int, conn: Connection) -> bool:
    """Remove a track's genre label.

    Needed when a re-label decides the track matches nothing after all: leaving
    the old row would keep a stale answer that the current rules would refuse
    to make, which is worse than having none.
    """
    removed = conn.execute("DELETE FROM track_genre WHERE track_id = %s",
                           (track_id,)).rowcount
    conn.commit()
    return bool(removed)


def ensure_tone_table(conn: Connection) -> None:
    """Create the lyric-tone table in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_tone (
            track_id        INTEGER PRIMARY KEY,
            tone            TEXT NOT NULL,
            score           REAL,
            runner_up       TEXT NOT NULL DEFAULT '',
            runner_up_score REAL,
            method          TEXT NOT NULL DEFAULT '',
            labelled_at     REAL NOT NULL,
            FOREIGN KEY(track_id) REFERENCES tracks(track_id)
        );
        CREATE INDEX IF NOT EXISTS idx_track_tone_tone ON track_tone (tone);
    """)
    conn.commit()


def record_tone(track_id: int, verdict: Any, conn: Connection, *,
                method: str = "embed-zero-shot") -> bool:
    """Store a lyric's tone. Separate from genre because the two axes come
    from different material and either can exist without the other: an
    instrumental has a genre and no tone, and a track whose audio was never
    embedded can still have its words read."""
    if verdict is None or not getattr(verdict, "tone", ""):
        return False
    conn.execute(
        """
        INSERT INTO track_tone (track_id, tone, score, runner_up,
                                runner_up_score, method, labelled_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            tone            = excluded.tone,
            score           = excluded.score,
            runner_up       = excluded.runner_up,
            runner_up_score = excluded.runner_up_score,
            method          = excluded.method,
            labelled_at     = excluded.labelled_at
        """,
        (track_id, verdict.tone, verdict.score, verdict.runner_up,
         verdict.runner_up_score, method, time.time()),
    )
    conn.commit()
    return True


def clear_tone(track_id: int, conn: Connection) -> bool:
    """Remove a track's tone, when a re-read decides it cannot be judged."""
    removed = conn.execute("DELETE FROM track_tone WHERE track_id = %s",
                           (track_id,)).rowcount
    conn.commit()
    return bool(removed)


def tone_for(track_id: int, conn: Connection) -> Any:
    """One track's stored tone row, or None."""
    return conn.execute(
        "SELECT * FROM track_tone WHERE track_id = %s", (track_id,)).fetchone()


def tone_counts(conn: Connection) -> list:
    """How many tracks carry each tone, commonest first."""
    return conn.execute(
        "SELECT tone, count(*) AS n, avg(score) AS mean_score"
        "  FROM track_tone GROUP BY tone ORDER BY n DESC").fetchall()


def genre_for(track_id: int, conn: Connection) -> Any:
    """One track's stored genre row, or None."""
    return conn.execute(
        "SELECT * FROM track_genre WHERE track_id = %s", (track_id,)).fetchone()


def genre_counts(conn: Connection) -> list:
    """How many tracks carry each label, commonest first."""
    return conn.execute(
        "SELECT genre, count(*) AS n, avg(score) AS mean_score"
        "  FROM track_genre GROUP BY genre ORDER BY n DESC").fetchall()


def tracks_in_genre(genre: str, conn: Connection,
                    limit: int = 50) -> list:
    """Tracks carrying a label, most confident first."""
    return conn.execute(
        "SELECT g.track_id, t.artist, t.title, g.score, g.runner_up"
        "  FROM track_genre g JOIN tracks t ON t.track_id = g.track_id"
        " WHERE g.genre = %s ORDER BY g.score DESC LIMIT %s",
        (genre, limit)).fetchall()


def ensure_artist_genres_table(conn: Connection) -> None:
    """Create the artist genres consensus table in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS artist_genres (
            artist_normalized TEXT NOT NULL,
            genre             TEXT NOT NULL,
            broad_genre       TEXT NOT NULL,
            weight            REAL NOT NULL DEFAULT 1.0,
            source            TEXT NOT NULL,
            fetched_at        REAL NOT NULL,
            PRIMARY KEY (artist_normalized, genre, broad_genre)
        );
        CREATE INDEX IF NOT EXISTS idx_artist_genres_broad ON artist_genres (broad_genre);
        CREATE INDEX IF NOT EXISTS idx_artist_genres_artist ON artist_genres (artist_normalized);
    """)
    conn.commit()


def get_artist_genres(artist: str, conn: Connection) -> tuple[list[str], list[str]]:
    """Retrieve specific genres and canonical broad genres for an artist.

    Returns:
        (specific_genres, broad_genres)
    """
    from .artist_classifier import normalize_artist
    norm = normalize_artist(artist)
    if not norm:
        return ([], [])
    rows = conn.execute(
        """
        SELECT genre, broad_genre FROM artist_genres
        WHERE artist_normalized = %s
        ORDER BY weight DESC
        """,
        (norm,),
    ).fetchall()
    specific: list[str] = []
    broad: set[str] = set()
    for r in rows:
        g = str(r["genre"]).strip() if r["genre"] else ""
        bg = str(r["broad_genre"]).strip() if r["broad_genre"] else ""
        if g and g not in specific:
            specific.append(g)
        if bg:
            broad.add(bg)
    return (specific, sorted(broad))


def get_all_artist_genres_map(conn: Connection) -> dict[str, dict[str, list[str]]]:
    """Pre-fetch all artist genre mappings for fast memory lookup in TUI / indexing.

    Returns:
        {artist_normalized: {"specific": [genres...], "broad": [broad_genres...]}}
    """
    ensure_artist_genres_table(conn)
    rows = conn.execute(
        "SELECT artist_normalized, genre, broad_genre FROM artist_genres"
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for r in rows:
        an = str(r["artist_normalized"]) if r["artist_normalized"] else ""
        g = str(r["genre"]) if r["genre"] else ""
        bg = str(r["broad_genre"]) if r["broad_genre"] else ""
        if an not in result:
            result[an] = {"specific": [], "broad": set()}
        if g and g not in result[an]["specific"]:
            result[an]["specific"].append(g)
        if bg:
            result[an]["broad"].add(bg)

    return {
        an: {"specific": d["specific"], "broad": sorted(d["broad"])}
        for an, d in result.items()
    }


def ensure_silence_table(conn: Connection) -> None:
    """Create the recording silence map in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recording_silence (
            recording_id INTEGER NOT NULL,
            file         TEXT NOT NULL,
            start_s      REAL NOT NULL,
            end_s        REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recording_silence_rec
            ON recording_silence (recording_id, file, start_s);

        -- A fully audible segment produces no silence rows, so the rows alone
        -- cannot say whether a file has been scanned. This records the scan
        -- itself -- and caches the measured duration, which is the expensive
        -- part: the segment muxer writes no FLAC duration header, so length
        -- has to be obtained by decoding the file.
        CREATE TABLE IF NOT EXISTS recording_silence_scans (
            recording_id INTEGER NOT NULL,
            file         TEXT NOT NULL,
            duration_s   REAL,
            scanned_at   REAL NOT NULL,
            PRIMARY KEY (recording_id, file)
        );
    """)
    conn.commit()


def record_silence(recording_id: int, file: str,
                   spans: list[tuple[float, float]],
                   conn: Connection, *,
                   duration_s: Optional[float] = None) -> int:
    """Store the silent stretches found in one segment file.

    Replaces any previous map for this file rather than adding to it: the audio
    does not change, so a re-scan is a correction, not more evidence.

    Storing it is not about the cost of detection -- 36 minutes of audio scans
    in about a second. It is so the map can be read by anything that is not
    holding the audio: a TUI row, a query, the alignment scorer, and any of
    them after the week's retention has deleted the files.
    """
    conn.execute(
        "DELETE FROM recording_silence WHERE recording_id = %s AND file = %s",
        (recording_id, file),
    )
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO recording_silence (recording_id, file, start_s, end_s) "
        "VALUES (%s, %s, %s, %s)",
        [(recording_id, file, s, e) for s, e in spans],
    )
    cur.close()
    conn.execute(
        """
        INSERT INTO recording_silence_scans
            (recording_id, file, duration_s, scanned_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(recording_id, file) DO UPDATE SET
            duration_s = excluded.duration_s,
            scanned_at = excluded.scanned_at
        """,
        (recording_id, file, duration_s, time.time()),
    )
    conn.commit()
    return len(spans)


def silence_for_recording(recording_id: int,
                          conn: Connection) -> list:
    """The stored silence map for a recording, in file and time order."""
    return conn.execute(
        """
        SELECT file, start_s, end_s FROM recording_silence
        WHERE recording_id = %s
        ORDER BY file, start_s
        """,
        (recording_id,),
    ).fetchall()


def silence_scans(recording_id: int, conn: Connection) -> dict:
    """Which files have been scanned, mapped to their measured duration.

    A fully audible segment stores no silence rows, so the rows alone cannot
    answer "has this been scanned"; that is what this table is for.
    """
    return {r["file"]: r["duration_s"] for r in conn.execute(
        "SELECT file, duration_s FROM recording_silence_scans "
        "WHERE recording_id = %s", (recording_id,))}


def ensure_restricted_table(conn: Connection) -> None:
    """Create the age-restricted list in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS restricted_tracks (
            track_id   INTEGER PRIMARY KEY,
            query      TEXT NOT NULL DEFAULT '',
            reason     TEXT NOT NULL DEFAULT '',
            noted_at   REAL NOT NULL
        )
    """)
    conn.commit()


def record_restricted(track_id: int, query: str, reason: str,
                      conn: Connection) -> None:
    """Note that a track could not be fetched without being signed in.

    Age-restricted uploads need a logged-in session, which a headless fetch
    does not have. Rather than retry them on every run and fail identically,
    they are collected so one pass can go through them later in the browser
    window that *is* signed in.
    """
    conn.execute(
        """
        INSERT INTO restricted_tracks (track_id, query, reason, noted_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            query = excluded.query,
            reason = excluded.reason,
            noted_at = excluded.noted_at
        """,
        (track_id, query, reason[:200], time.time()),
    )
    conn.commit()


def restricted_tracks(conn: Connection) -> list:
    """Tracks waiting on a signed-in session, with their names."""
    return conn.execute(
        """
        SELECT r.track_id, r.query, r.reason, r.noted_at,
               t.artist, t.title
        FROM restricted_tracks r
        JOIN tracks t ON t.track_id = r.track_id
        ORDER BY t.artist, t.title
        """
    ).fetchall()


def ensure_spotify_lookup_table(conn: Connection) -> None:
    """Create the Spotify lookup cache in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spotify_lookups (
            track_id   INTEGER PRIMARY KEY,
            uri        TEXT,
            checked_at REAL NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def spotify_lookup_due(track_id: Optional[int], conn: Connection, *,
                       max_attempts: int = SPOTIFY_LOOKUP_ATTEMPTS) -> bool:
    """Whether this track may be looked up on Spotify.

    False once a URI is known (the answer is cached), and false once the track
    has been asked about ``max_attempts`` times without one — a miss is a
    result, not an invitation to keep spending search quota.
    """
    if track_id is None:
        return False
    row = conn.execute(
        "SELECT uri, attempts FROM spotify_lookups WHERE track_id = %s",
        (track_id,),
    ).fetchone()
    if row is None:
        return True
    if row["uri"]:
        return False
    return int(row["attempts"] or 0) < max_attempts


def record_spotify_lookup(track_id: int, uri: Optional[str],
                          conn: Connection) -> None:
    """Record the outcome of a Spotify lookup, hit or miss.

    Call this for a miss too. Never call it for a rate-limit error: a 429 says
    nothing about whether Spotify has the song, and recording it would cache a
    false negative that ``spotify_lookup_due`` would then honour forever.
    """
    conn.execute(
        """
        INSERT INTO spotify_lookups (track_id, uri, checked_at, attempts)
        VALUES (%s, %s, %s, 1)
        ON CONFLICT(track_id) DO UPDATE SET
            uri = excluded.uri,
            checked_at = excluded.checked_at,
            attempts = spotify_lookups.attempts + 1
        """,
        (track_id, uri or None, time.time()),
    )
    conn.commit()


# Above this, a "track" is a full-album upload rather than a song. Album sides
# run to about 25 minutes and the longest real songs here are well under it --
# Ren's "The Tale of Jenny & Screech" is 13.5 minutes and genuinely one track.
# Duration is the fact, so nothing is stored: a row stops being an album upload
# the moment a better duration lands, with no flag to go stale.
ALBUM_UPLOAD_SECONDS = 1500.0


def is_album_upload(duration) -> bool:
    """Whether a duration means this row is a whole album, not a song.

    Such rows cannot carry a meaningful key, tempo or album name -- an analysis
    over 70 minutes of different songs is a number about nothing -- so they are
    kept out of search results, playlists and the analysis queue.
    """
    try:
        return duration is not None and float(duration) > ALBUM_UPLOAD_SECONDS
    except (TypeError, ValueError):
        return False


def offset_mode(mode: str) -> str:
    """Collapse a detection mode to the clock its sync offset belongs to.

    Radio dead-reckons the playhead from songrec and adds ``DEFAULT_LEAD_S`` to
    cover the listening latency; every other mode reads an MPRIS position with
    no such lead. An offset tuned against one clock is wrong by roughly that
    lead on the other, so the two are stored and matched separately. Modes that
    share the MPRIS position (scan, spotify, listen) share an offset.
    """
    return "radio" if mode == "radio" else "player"


def ensure_sync_offset_columns(conn: Connection) -> None:
    """Add the ``mode`` column to databases created before it existed."""
    cols = {r["name"] for r in conn.execute("SELECT column_name as name FROM information_schema.columns WHERE table_name = %s", ("track_sync_offsets",))}
    if "mode" not in cols:
        conn.execute("ALTER TABLE track_sync_offsets ADD COLUMN "
                     "mode TEXT NOT NULL DEFAULT ''")
        conn.commit()


def get_sync_offset(track_id: Optional[int], conn: Connection,
                    mode: Optional[str] = None) -> Optional[float]:
    """Return the saved lyric sync offset (seconds) for a track, or None.

    When ``mode`` is given, an offset saved against a different clock is
    ignored rather than misapplied — the caller then falls back to its default.
    Rows predating the ``mode`` column store ``''`` and are honoured for any
    mode: they cannot be attributed, and the alternative is silently discarding
    tuning the user did by hand.
    """
    if track_id is None:
        return None
    row = conn.execute(
        "SELECT offset_s, mode FROM track_sync_offsets WHERE track_id = %s",
        (track_id,),
    ).fetchone()
    if row is None:
        return None
    saved = row["mode"] or ""
    if mode is not None and saved and saved != offset_mode(mode):
        return None
    return float(row["offset_s"])


def set_sync_offset(track_id: int, offset_s: float, conn: Connection,
                    mode: str = "") -> None:
    """Persist (upsert) the per-track lyric sync offset in seconds."""
    conn.execute(
        """
        INSERT INTO track_sync_offsets (track_id, offset_s, updated_at, mode)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT(track_id) DO UPDATE SET
            offset_s = excluded.offset_s,
            updated_at = excluded.updated_at,
            mode = excluded.mode
        """,
        (track_id, float(offset_s), time.time(),
         offset_mode(mode) if mode else ""),
    )
    conn.commit()


def _tune_connection(conn: Connection) -> None:
    """Apply performance PRAGMAs. Best-effort: never fail opening the DB.

    WAL is the important one for a large collection being scanned while the
    TUI browses it: readers no longer block behind the importer's writer.
    ``KARAOKE_SQLITE_JOURNAL`` can force a different mode (e.g. ``DELETE``) if
    the DB lives on a filesystem where WAL is unsupported (some network mounts).
    """
    import os

    journal = os.environ.get("KARAOKE_SQLITE_JOURNAL", "WAL")
    try:
        conn.execute(f"PRAGMA journal_mode = {journal}")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -16000")  # ~16 MB page cache
    except psycopg.Error as exc:
        log.debug("sqlite tuning skipped: %s", exc)


def ensure_performance_indexes(conn: Connection) -> None:
    """Index the foreign-key/join columns the library and search hit constantly.

    ``sources.track_id`` and ``lyrics.track_id`` back the JOINs behind every
    library load and ``*`` browse; without an index each becomes a full scan of
    a table that grows with the whole collection. Idempotent and cheap.
    """
    try:
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sources_track ON sources (track_id);
            CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources (kind);
            CREATE INDEX IF NOT EXISTS idx_lyrics_track ON lyrics (track_id);
            CREATE INDEX IF NOT EXISTS idx_lyrics_track_kind ON lyrics (track_id, kind);
            CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks (artist);
            CREATE INDEX IF NOT EXISTS idx_tracks_title ON tracks (title);
            """
        )
        conn.commit()
    except psycopg.Error as exc:
        log.debug("sqlite index creation skipped: %s", exc)


def ensure_queue_schema(conn: Connection) -> None:
    """Ensure tables exist for active queue state and playback queue events."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_queue_state (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                playlist_id   TEXT DEFAULT '',
                name          TEXT DEFAULT '',
                current_index INTEGER DEFAULT 0,
                updated_at    REAL NOT NULL,
                items_json    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS queue_events (
                id           SERIAL PRIMARY KEY,
                ts           REAL NOT NULL,
                event_type   TEXT NOT NULL,
                track_id     INTEGER,
                artist       TEXT DEFAULT '',
                title        TEXT DEFAULT '',
                queue_index  INTEGER DEFAULT 0,
                payload_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_queue_events_ts ON queue_events(ts);
            """
        )
        conn.commit()
        ensure_saved_searches_and_playlists_schema(conn)
    except psycopg.Error as exc:
        log.debug("queue schema setup skipped: %s", exc)


def ensure_saved_searches_and_playlists_schema(conn: Connection) -> None:
    """Ensure tables exist for saved searches and synced playlists."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_searches (
                query        TEXT PRIMARY KEY,
                result_count INTEGER DEFAULT 0,
                created_at   DOUBLE PRECISION NOT NULL,
                last_used_at DOUBLE PRECISION NOT NULL,
                use_count    INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_saved_searches_last_used ON saved_searches(last_used_at);

            CREATE TABLE IF NOT EXISTS saved_playlists (
                playlist_id  TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                search_query TEXT DEFAULT '',
                track_count  INTEGER DEFAULT 0,
                url          TEXT DEFAULT '',
                source_kind  TEXT DEFAULT 'youtube_music',
                created_at   DOUBLE PRECISION NOT NULL,
                updated_at   DOUBLE PRECISION NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_saved_playlists_updated ON saved_playlists(updated_at);

            CREATE TABLE IF NOT EXISTS saved_playlist_tracks (
                playlist_id TEXT NOT NULL,
                position    INTEGER NOT NULL,
                track_id    INTEGER,
                artist      TEXT NOT NULL,
                title       TEXT NOT NULL,
                video_id    TEXT DEFAULT '',
                url         TEXT DEFAULT '',
                PRIMARY KEY (playlist_id, position),
                FOREIGN KEY (playlist_id) REFERENCES saved_playlists(playlist_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_saved_playlist_tracks_pid ON saved_playlist_tracks(playlist_id);
            """
        )
        conn.commit()
    except psycopg.Error as exc:
        log.debug("saved searches and playlists schema setup skipped: %s", exc)


def ensure_radio_session_schema(conn: Connection) -> None:
    """Ensure tables exist for radio sessions and detected session tracks."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS radio_sessions (
                session_id    SERIAL PRIMARY KEY,
                started_at    REAL NOT NULL,
                ended_at      REAL,
                source        TEXT NOT NULL DEFAULT 'mic',
                is_recording  INTEGER NOT NULL DEFAULT 0,
                recording_id  INTEGER,
                track_count   INTEGER NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'active',
                notes         TEXT,
                FOREIGN KEY(recording_id) REFERENCES recordings(recording_id)
            );
            CREATE INDEX IF NOT EXISTS idx_radio_sessions_started ON radio_sessions(started_at);
            CREATE INDEX IF NOT EXISTS idx_radio_sessions_status ON radio_sessions(status);

            CREATE TABLE IF NOT EXISTS radio_session_tracks (
                id                 SERIAL PRIMARY KEY,
                session_id         INTEGER NOT NULL,
                artist             TEXT NOT NULL,
                title              TEXT NOT NULL,
                first_seen_at      REAL NOT NULL,
                last_seen_at       REAL NOT NULL,
                play_count         INTEGER NOT NULL DEFAULT 1,
                offset_s           REAL,
                has_synced_lyrics  INTEGER NOT NULL DEFAULT 0,
                lyric_source       TEXT DEFAULT '',
                imported           INTEGER NOT NULL DEFAULT 0,
                imported_track_id  INTEGER,
                FOREIGN KEY(session_id) REFERENCES radio_sessions(session_id) ON DELETE CASCADE,
                FOREIGN KEY(imported_track_id) REFERENCES tracks(track_id)
            );
            CREATE INDEX IF NOT EXISTS idx_radio_session_tracks_sid ON radio_session_tracks(session_id);
            CREATE INDEX IF NOT EXISTS idx_radio_session_tracks_track ON radio_session_tracks(artist, title);
            """
        )
        conn.commit()
    except psycopg.Error as exc:
        log.debug("radio session schema setup skipped: %s", exc)


import atexit
import os
from psycopg_pool import ConnectionPool
import psycopg.rows

_POOL = None
def get_pool() -> ConnectionPool:
    global _POOL
    if _POOL is None:
        pg_url = os.environ.get("KARAOKE_PG_URL", "postgresql://karaoke:karaoke@localhost:5432/karaoke")
        _POOL = ConnectionPool(conninfo=pg_url, open=True, min_size=1, max_size=500, kwargs={"autocommit": True, "row_factory": psycopg.rows.dict_row})
        # Close the pool while the interpreter is still alive. Left to
        # finalization, ConnectionPool.__del__ tries to join its worker threads
        # after shutdown has begun and raises PythonFinalizationError -- which
        # printed a traceback under every CLI and health-check run, reading
        # like a crash directly beneath a successful report.
        atexit.register(close_pool)
    return _POOL


def close_pool() -> None:
    """Close the connection pool if one was opened (idempotent)."""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass

def connect(db_path: Optional[Path] = None) -> Connection:
    pool = get_pool()
    conn = pool.getconn()
    
    class AutoCloseConnection(conn.__class__):
        def __exit__(self, exc_type, exc_val, exc_tb):
            super().__exit__(exc_type, exc_val, exc_tb)
            self.close()

    conn.__class__ = AutoCloseConnection

    _closed = False
    def pool_close():
        nonlocal _closed
        if _closed:
            return
        _closed = True
        try:
            conn.rollback()
        except Exception:
            pass
        pool.putconn(conn)
        
    conn.close = pool_close
    return conn

def _tune_connection(conn: Connection):
    pass


def table_exists(conn: Connection, table_name: str) -> bool:
    """Check if a table exists in either PostgreSQL or SQLite."""
    try:
        cur = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
            (table_name,),
        )
        return bool(cur.fetchone())
    except Exception:
        pass

    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table_name,),
        )
        return bool(cur.fetchone())
    except Exception:
        return False


def find_track_id(artist: str, title: str, conn: Connection) -> Optional[int]:
    """Find a track by artist and title, returning its ID.

    Player metadata can vary in case, so cache lookup is case-insensitive while
    preserving the originally-stored display spelling.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT track_id FROM tracks
        WHERE lower(artist) = lower(%s) AND lower(title) = lower(%s)
        ORDER BY track_id DESC
        LIMIT 1
        """,
        (artist, title),
    )
    row = cur.fetchone()
    return row["track_id"] if row else None


def _match_key(text: str) -> str:
    """Aggressively normalised form for comparing track/artist names.

    Lowercased, accents stripped, everything but letters and digits removed, so
    "(Sittin' on) The Dock of the Bay" and "(Sittin On) The Dock Of The Bay"
    collapse to the same key.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", folded.casefold())


def _artist_key(artist: str) -> str:
    """Match key for an artist, ignoring a leading "The".

    Sources disagree on it constantly — songrec stores "The Mothers of
    Invention" where a player reports "Mothers of Invention".
    """
    return _match_key(re.sub(r"^\s*the\s+", "", artist or "", flags=re.I))


def _title_keys(title: str) -> set[str]:
    """Match keys for a title, with and without trailing bracketed suffixes.

    Editions arrive in every bracket style — "[2020 Remaster]", "(Live)",
    "- Remastered 2011" — and only some are covered by clean_title's suffix
    list, so the bare stem is compared too.
    """
    from .lyrics import clean_title

    keys = {_match_key(title), _match_key(clean_title(title))}
    stem = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title or "").strip()
    while stem and stem != title:
        keys.add(_match_key(stem))
        title, stem = stem, re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", stem).strip()
    return {k for k in keys if k}


def find_track_id_relaxed(artist: str, title: str,
                          conn: Connection) -> Optional[int]:
    """Find a track when the exact spelling does not match.

    Sources name the same song differently: songrec stores the full credit
    ("James Brown & The Famous Flames") and a decorated title ("[2020
    Remaster]") where a browser reports "James Brown" and a plainer title. An
    exact lookup misses those, so a track the radio already cached looks absent
    to the TUI and its lyrics never appear.

    Tries the exact match, then the cleaned title, then a punctuation- and
    case-insensitive comparison in which the artist need only be a prefix of
    the stored credit, or the reverse.
    """
    exact = find_track_id(artist, title, conn)
    if exact is not None:
        return exact

    from .lyrics import clean_title

    cleaned = clean_title(title)
    if cleaned and cleaned != title:
        got = find_track_id(artist, cleaned, conn)
        if got is not None:
            return got

    want_titles = _title_keys(title) | _title_keys(cleaned or title)
    want_artist = _artist_key(artist)
    if not want_titles:
        return None

    # Prefilter in SQL on a word of the title so this stays cheap on a large
    # library rather than normalising every row on every 1.5s poll.
    words = re.sub(r"[^a-z0-9]+", " ", (cleaned or title).casefold()).split()
    lead = max(words, key=len) if words else ""
    rows = conn.execute(
        "SELECT track_id, artist, title FROM tracks WHERE lower(title) LIKE %s"
        " ORDER BY track_id DESC LIMIT 200",
        (f"%{lead}%" if lead else "%",),
    ).fetchall()

    for row in rows:
        if not (_title_keys(row["title"]) & want_titles):
            continue
        got_artist = _artist_key(row["artist"])
        if not want_artist or not got_artist:
            return int(row["track_id"])
        # "James Brown" should match "James Brown & The Famous Flames".
        if got_artist.startswith(want_artist) or want_artist.startswith(got_artist):
            return int(row["track_id"])
    return None


def extract_youtube_id(url: str) -> Optional[str]:
    """Extract the 11-character video ID from a YouTube or YouTube Music URL, ID or filename."""
    if not url:
        return None
    import re
    m = re.search(r"(?:v=|/v/|/embed/|youtu\.be/|/watch\?v=)([^&\s\?]+)", url)
    if m:
        val = m.group(1)
        if len(val) == 11:
            return val
    stem = Path(url).stem
    if len(stem) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", stem):
        return stem
    if len(url) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", url):
        return url
    return None


def extract_playlist_id(url: Optional[str]) -> Optional[str]:
    """Extract YouTube playlist ID from URL (e.g. from list= parameter) or return None."""
    if not url:
        return None
    import re
    # Check for list= parameter
    m = re.search(r"(?:[?&]list=)([^&\s#]+)", url)
    if m:
        val = m.group(1).strip()
        if val and re.match(r"^[a-zA-Z0-9_-]{10,}$", val):
            return val
    # Direct playlist ID string if passed directly (starts with standard prefix or is >= 12 chars)
    trimmed = url.strip()
    if re.match(r"^(?:PL|RD|OLAK5uy_|VL)[a-zA-Z0-9_-]{10,}$", trimmed):
        return trimmed
    return None


def find_track_by_url(url: str, conn: Connection) -> Optional[tuple[int, str, str]]:
    """Find a track by source URL, returning (track_id, artist, title)."""
    cur = conn.cursor()
    # Try exact match first
    cur.execute(
        """
        SELECT t.track_id, t.artist, t.title
        FROM tracks t JOIN sources s ON t.track_id = s.track_id
        WHERE s.url = %s
        """,
        (url,)
    )
    row = cur.fetchone()
    if row:
        return row["track_id"], row["artist"], row["title"]

    # Fallback to matching by YouTube video ID
    vid = extract_youtube_id(url)
    if vid:
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title
            FROM tracks t JOIN sources s ON t.track_id = s.track_id
            WHERE s.url LIKE %s AND s.kind IN ('youtube', 'youtube_music')
            LIMIT 1
            """,
            (f"%{vid}%",)
        )
        row = cur.fetchone()
        if row:
            return row["track_id"], row["artist"], row["title"]

    return None


def get_lyrics_by_track_id(track_id: int, conn: Connection) -> Optional[Lyrics]:
    """Get approved lyrics for a given track ID."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM lyrics WHERE track_id = %s AND kind = 'approved'",
        (track_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    
    synced = row["synced_lyrics"] or ""
    plain = row["plain_lyrics"] or ""
    if not synced and not plain:
        return None
        
    return Lyrics(
        plain=plain,
        synced_raw=synced,
        source=row["source"] or "lrclib",
        lines=parse_lrc(synced) if synced else [],
    )

def get_cached_lyrics(
    artist: str,
    title: str,
    *,
    conn: Optional[Connection] = None,
) -> Optional[Lyrics]:
    """Return approved cached lyrics for artist/title, or None on miss.

    Compatibility API kept for older code/tests while the database now stores
    tracks and lyrics in separate tables.
    """
    own = conn is None
    c = conn or connect()
    try:
        track_id = find_track_id(artist, title, c)
        if not track_id:
            return None
        return get_lyrics_by_track_id(track_id, c)
    finally:
        if own:
            c.close()


def put_cached_lyrics(
    artist: str,
    title: str,
    lyrics: Lyrics,
    *,
    album: str = "",
    duration: Optional[float] = None,
    conn: Optional[Connection] = None,
) -> None:
    """Upsert approved lyrics for artist/title.

    Empty lyrics are ignored. Existing approved lyrics for the track are replaced
    so a Whisper/synced result can upgrade an earlier plain LRCLIB/caption entry.
    """
    if not (lyrics.synced_raw or lyrics.plain):
        return
    own = conn is None
    c = conn or connect()
    try:
        cur = c.cursor()
        track_id = find_track_id(artist, title, c)
        if track_id is None:
            cur.execute(
                "INSERT INTO tracks (artist, title, album, duration) VALUES (%s, %s, %s, %s) RETURNING track_id",
                (artist, title, album, duration),
            )
            track_id = cur.fetchone()["track_id"]
            if track_id is None:
                return
        else:
            cur.execute(
                """
                UPDATE tracks
                SET album = COALESCE(NULLIF(%s, ''), album),
                    duration = COALESCE(%s, duration)
                WHERE track_id = %s
                """,
                (album, duration, track_id),
            )

        cur.execute(
            "DELETE FROM lyrics WHERE track_id = %s AND kind = 'approved'",
            (track_id,),
        )
        cur.execute(
            """
            INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics)
            VALUES (%s, 'approved', %s, %s, %s)
            """,
            (track_id, lyrics.source, lyrics.synced_raw, lyrics.plain),
        )
        c.commit()
    finally:
        if own:
            c.close()


def delete_empty_approved_lyrics(conn: Optional[Connection] = None) -> int:
    """Delete placeholder approved lyrics rows that contain no synced or plain text."""
    own = conn is None
    c = conn or connect()
    try:
        cur = c.cursor()
        cur.execute(
            """
            DELETE FROM lyrics
            WHERE kind = 'approved'
              AND COALESCE(synced_lyrics, '') = ''
              AND COALESCE(plain_lyrics, '') = ''
            """
        )
        deleted = cur.rowcount if cur.rowcount is not None else 0
        c.commit()
        return int(deleted)
    finally:
        if own:
            c.close()


def add_track_source(
    artist: str,
    title: str,
    *,
    album: str = "",
    duration: Optional[float] = None,
    url: Optional[str] = None,
    kind: str = "youtube",
    player_name: str = "",
    conn: Optional[Connection] = None,
) -> int:
    """Upsert a track and optional source URL without creating lyrics rows."""
    own = conn is None
    c = conn or connect()
    try:
        cur = c.cursor()
        # Prefer matching an existing track by its source URL / YouTube video ID
        # so callers that pass a URL (transcribe, backfill, write-through) update
        # the canonical track instead of spawning a duplicate when the parsed
        # artist/title differ (e.g. Whisper's filename-derived tags, remix edits).
        track_id: Optional[int] = None
        if url:
            found = find_track_by_url(url, c)
            if found:
                track_id = found[0]
        if track_id is None:
            track_id = find_track_id(artist, title, c)
        if track_id is None:
            cur.execute(
                "INSERT INTO tracks (artist, title, album, duration) VALUES (%s, %s, %s, %s) RETURNING track_id",
                (artist, title, album, duration),
            )
            track_id = cur.fetchone()["track_id"]
            if track_id is None:
                raise RuntimeError("failed to insert track")
        else:
            cur.execute(
                """
                UPDATE tracks
                SET album = COALESCE(NULLIF(%s, ''), album),
                    duration = COALESCE(%s, duration)
                WHERE track_id = %s
                """,
                (album, duration, track_id),
            )

        if url:
            cur.execute(
                """
                INSERT INTO sources (track_id, url, kind, player_name)
                VALUES (%s, %s, %s, NULLIF(%s, ''))
                ON CONFLICT(url) DO UPDATE SET
                    track_id = excluded.track_id,
                    kind = excluded.kind,
                    player_name = COALESCE(excluded.player_name, sources.player_name)
                """,
                (track_id, url, kind, player_name),
            )
        c.commit()
        return int(track_id)
    finally:
        if own:
            c.close()


def add_track_and_lyrics(
    artist: str,
    title: str,
    lyrics: Lyrics,
    album: str = "",
    duration: Optional[float] = None,
    url: Optional[str] = None,
    kind: str = "youtube",
    conn: Optional[Connection] = None,
) -> int:
    """Add or update a track, its optional source URL and approved lyrics."""
    if not (lyrics.synced_raw or lyrics.plain):
        return add_track_source(
            artist,
            title,
            album=album,
            duration=duration,
            url=url,
            kind=kind,
            conn=conn,
        )

    own = conn is None
    c = conn or connect()
    try:
        track_id = add_track_source(
            artist,
            title,
            album=album,
            duration=duration,
            url=url,
            kind=kind,
            conn=c,
        )
        cur = c.cursor()
        cur.execute(
            "DELETE FROM lyrics WHERE track_id = %s AND kind = 'approved'",
            (track_id,),
        )
        cur.execute(
            """
            INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics)
            VALUES (%s, 'approved', %s, %s, %s)
            """,
            (track_id, lyrics.source, lyrics.synced_raw, lyrics.plain),
        )
        c.commit()
        return int(track_id)
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------------------
# Play / discovery stats (Unchanged from previous implementation)
# ---------------------------------------------------------------------------

def log_event(
    mode: str,
    event: str,
    *,
    artist: str = "",
    title: str = "",
    source: str = "",
    has_synced: bool = False,
    conn: Optional[Connection] = None,
) -> None:
    """Record one play/discovery event (best-effort; never raises to caller)."""
    own = conn is None
    try:
        c = conn or connect()
    except Exception:
        return
    try:
        c.execute(
            "INSERT INTO play_events (ts, mode, artist, title, event, source, has_synced)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (time.time(), mode, artist, title, event, source, int(has_synced)),
        )
        if event in ("play", "discover") and (artist or title):
            c.execute(
                "UPDATE tracks SET play_count = play_count + 1"
                " WHERE LOWER(artist) = LOWER(%s) AND LOWER(title) = LOWER(%s)",
                (artist, title),
            )
        c.commit()
    except Exception:
        pass
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


@dataclass
class StatsSummary:
    """Aggregated play/discovery statistics."""

    total_events: int
    plays: int
    discoveries: int
    cache_hits: int
    cache_misses: int
    distinct_tracks: int
    distinct_artists: int
    top_tracks: list[tuple[str, str, int]]     # (artist, title, plays)
    top_artists: list[tuple[str, int]]         # (artist, plays)
    by_mode: list[tuple[str, int]]             # (mode, plays)

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of lyrics lookups served from the local cache (0..1)."""
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total) if total else 0.0


def summarize(
    *, limit: int = 10, since: Optional[float] = None,
    conn: Optional[Connection] = None,
) -> StatsSummary:
    """Compute a StatsSummary over play_events (optionally since a UNIX time)."""
    own = conn is None
    c = conn or connect()
    where = "WHERE ts >= %s" if since is not None else ""
    args: tuple = (since,) if since is not None else ()
    try:
        def scalar(sql: str, extra: tuple = ()) -> int:
            row = c.execute(sql, args + extra).fetchone()
            return int(list(row.values())[0]) if row and list(row.values())[0] is not None else 0

        total = scalar(f"SELECT COUNT(*) FROM play_events {where}")
        plays = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'play'"
        )
        discoveries = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'discover'"
        )
        hits = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'cache_hit'"
        )
        misses = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'cache_miss'"
        )
        play_where = (
            f"{where} {'AND' if where else 'WHERE'} event IN ('play','discover') "
            "AND title != ''"
        )
        distinct_tracks = scalar(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM play_events {play_where} "
            "GROUP BY artist, title)"
        )
        distinct_artists = scalar(
            f"SELECT COUNT(DISTINCT artist) FROM play_events {play_where} "
            "AND artist != ''"
        )
        top_tracks = [
            (r["artist"], r["title"], int(r["n"]))
            for r in c.execute(
                f"SELECT artist, title, COUNT(*) AS n FROM play_events {play_where} "
                "GROUP BY artist, title ORDER BY n DESC, title ASC LIMIT %s",
                args + (limit,),
            ).fetchall()
        ]
        top_artists = [
            (r["artist"], int(r["n"]))
            for r in c.execute(
                f"SELECT artist, COUNT(*) AS n FROM play_events {play_where} "
                "AND artist != '' GROUP BY artist ORDER BY n DESC, artist ASC LIMIT %s",
                args + (limit,),
            ).fetchall()
        ]
        by_mode = [
            (r["mode"], int(r["n"]))
            for r in c.execute(
                f"SELECT mode, COUNT(*) AS n FROM play_events {play_where} "
                "GROUP BY mode ORDER BY n DESC",
                args,
            ).fetchall()
        ]
    finally:
        if own:
            c.close()
    return StatsSummary(
        total_events=total, plays=plays, discoveries=discoveries,
        cache_hits=hits, cache_misses=misses,
        distinct_tracks=distinct_tracks, distinct_artists=distinct_artists,
        top_tracks=top_tracks, top_artists=top_artists, by_mode=by_mode,
    )


# -- active queue & rollback persistence --------------------------------------

def save_active_queue(
    playlist_id: str,
    name: str,
    rows: list[dict[str, Any]],
    current_index: int = 0,
    conn: Optional[Connection] = None,
) -> None:
    """Persist the currently loaded queue to SQLite for instant restoration on reload."""
    import json
    import time
    own = conn is None
    c = conn or connect()
    try:
        ensure_queue_schema(c)
        clean_rows = [
            {
                "track_id": r.get("track_id"),
                "artist": r.get("artist") or "",
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "kind": r.get("kind") or "",
                "genre": r.get("genre"),
                "energy": r.get("energy"),
                "bpm": r.get("bpm"),
                "key": r.get("key"),
            }
            for r in rows
        ]
        c.execute(
            """
            INSERT INTO active_queue_state (id, playlist_id, name, current_index, updated_at, items_json)
            VALUES (1, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                playlist_id = excluded.playlist_id,
                name = excluded.name,
                current_index = excluded.current_index,
                updated_at = excluded.updated_at,
                items_json = excluded.items_json
            """,
            (playlist_id, name, current_index, time.time(), json.dumps(clean_rows)),
        )
        c.commit()
    finally:
        if own:
            c.close()


def load_active_queue(conn: Optional[Connection] = None) -> Optional[dict[str, Any]]:
    """Load the persisted active queue from SQLite, or None if none saved."""
    import json
    own = conn is None
    c = conn or connect()
    try:
        ensure_queue_schema(c)
        row = c.execute(
            "SELECT playlist_id, name, current_index, updated_at, items_json FROM active_queue_state WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        try:
            items = json.loads(row["items_json"])
        except Exception:
            items = []
        return {
            "playlist_id": str(row["playlist_id"] or ""),
            "name": str(row["name"] or ""),
            "current_index": int(row["current_index"] or 0),
            "updated_at": float(row["updated_at"] or 0.0),
            "rows": items,
        }
    finally:
        if own:
            c.close()


def get_last_played_track(conn: Optional[Connection] = None) -> Optional[dict[str, Any]]:
    """Return the most recently played track from play_events, or None."""
    own = conn is None
    c = conn or connect()
    try:
        row = c.execute(
            """
            SELECT artist, title, mode, source, ts
            FROM play_events
            WHERE event = 'play' AND (artist != '' OR title != '')
            ORDER BY ts DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        return {
            "artist": str(row["artist"] or ""),
            "title": str(row["title"] or ""),
            "mode": str(row["mode"] or ""),
            "source": str(row["source"] or ""),
            "ts": float(row["ts"] or 0.0),
        }
    finally:
        if own:
            c.close()


def record_queue_event(
    event_type: str,
    track_id: Optional[int] = None,
    artist: str = "",
    title: str = "",
    queue_index: int = 0,
    metadata: Optional[dict[str, Any]] = None,
    conn: Optional[Connection] = None,
) -> int:
    """Record a queue navigation/playback event for audit and rollback."""
    import json
    import time
    own = conn is None
    c = conn or connect()
    try:
        ensure_queue_schema(c)
        cur = c.execute(
            """
            INSERT INTO queue_events (ts, event_type, track_id, artist, title, queue_index, payload_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (
                time.time(),
                event_type,
                track_id,
                artist or "",
                title or "",
                queue_index,
                json.dumps(metadata or {}),
            ),
        )
        ret_id = cur.fetchone()["id"]
        c.commit()
        return int(ret_id)
    finally:
        if own:
            c.close()


def get_recent_queue_events(
    limit: int = 20,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Return recent queue events in reverse chronological order."""
    import json
    own = conn is None
    c = conn or connect()
    try:
        ensure_queue_schema(c)
        rows = c.execute(
            """
            SELECT id, ts, event_type, track_id, artist, title, queue_index, payload_json
            FROM queue_events
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"])
            except Exception:
                payload = {}
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "event_type": r["event_type"],
                "track_id": r["track_id"],
                "artist": r["artist"],
                "title": r["title"],
                "queue_index": r["queue_index"],
                "payload": payload,
            })
        return out
    finally:
        if own:
            c.close()


def record_search_query(
    query: str,
    result_count: int = 0,
    conn: Optional[Connection] = None,
) -> None:
    """Record or update a search query in SQLite/Postgres."""
    q = (query or "").strip().lower()
    if not q or q == "*":
        return
    now = time.time()
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        c.execute(
            """
            INSERT INTO saved_searches (query, result_count, created_at, last_used_at, use_count)
            VALUES (%s, %s, %s, %s, 1)
            ON CONFLICT(query) DO UPDATE SET
                query = excluded.query,
                result_count = excluded.result_count,
                last_used_at = excluded.last_used_at,
                use_count = saved_searches.use_count + 1
            """,
            (q, result_count, now, now),
        )
        c.commit()
    finally:
        if own:
            c.close()


def get_saved_searches(
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Return recently used saved search queries."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        rows = c.execute(
            """
            SELECT query, result_count, created_at, last_used_at, use_count
            FROM saved_searches
            ORDER BY last_used_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "query": r["query"],
                "result_count": r["result_count"],
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
                "use_count": r["use_count"],
            }
            for r in rows
        ]
    finally:
        if own:
            c.close()


def save_playlist(
    playlist_id: str,
    name: str,
    search_query: str = "",
    tracks: Optional[list[dict[str, Any]]] = None,
    url: str = "",
    source_kind: str = "youtube_music",
    conn: Optional[Connection] = None,
) -> None:
    """Save or update a synced playlist and its tracks in SQLite."""
    pid = (playlist_id or "").strip()
    if not pid:
        return
    now = time.time()
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        track_count = len(tracks) if tracks is not None else 0
        c.execute(
            """
            INSERT INTO saved_playlists (playlist_id, name, search_query, track_count, url, source_kind, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(playlist_id) DO UPDATE SET
                name = excluded.name,
                search_query = CASE WHEN excluded.search_query != '' THEN excluded.search_query ELSE saved_playlists.search_query END,
                track_count = CASE WHEN excluded.track_count > 0 THEN excluded.track_count ELSE saved_playlists.track_count END,
                url = CASE WHEN excluded.url != '' THEN excluded.url ELSE saved_playlists.url END,
                source_kind = excluded.source_kind,
                updated_at = excluded.updated_at
            """,
            (pid, name, search_query or "", track_count, url or "", source_kind, now, now),
        )
        if tracks is not None:
            c.execute("DELETE FROM saved_playlist_tracks WHERE playlist_id = %s", (pid,))
            for pos, t in enumerate(tracks, 1):
                c.execute(
                    """
                    INSERT INTO saved_playlist_tracks (playlist_id, position, track_id, artist, title, video_id, url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        pid,
                        t.get("position", pos),
                        t.get("track_id"),
                        t.get("artist", "") or "",
                        t.get("title", "") or "",
                        t.get("video_id", "") or "",
                        t.get("url", "") or "",
                    ),
                )
        c.commit()
    finally:
        if own:
            c.close()


def get_saved_playlists(
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Return saved playlists ordered by update time."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        rows = c.execute(
            """
            SELECT playlist_id, name, search_query, track_count, url, source_kind, created_at, updated_at
            FROM saved_playlists
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "playlist_id": r["playlist_id"],
                "name": r["name"],
                "search_query": r["search_query"],
                "track_count": r["track_count"],
                "url": r["url"],
                "source_kind": r["source_kind"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
    finally:
        if own:
            c.close()


def get_saved_playlist_tracks(
    playlist_id: str,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Return tracks for a saved playlist ordered by position."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        rows = c.execute(
            """
            SELECT position, track_id, artist, title, video_id, url
            FROM saved_playlist_tracks
            WHERE playlist_id = %s
            ORDER BY position ASC
            """,
            (playlist_id,),
        ).fetchall()
        return [
            {
                "position": r["position"],
                "track_id": r["track_id"],
                "artist": r["artist"],
                "title": r["title"],
                "video_id": r["video_id"],
                "url": r["url"],
            }
            for r in rows
        ]
    finally:
        if own:
            c.close()


def find_saved_playlist_by_id(
    playlist_id: str,
    conn: Optional[Connection] = None,
) -> Optional[dict[str, Any]]:
    """Retrieve a saved playlist by its playlist ID, or None if not found."""
    if not playlist_id:
        return None
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        r = c.execute(
            """
            SELECT playlist_id, name, search_query, track_count, url, source_kind, created_at, updated_at
            FROM saved_playlists
            WHERE playlist_id = %s
            """,
            (playlist_id,),
        ).fetchone()
        if not r:
            return None
        return {
            "playlist_id": r["playlist_id"],
            "name": r["name"],
            "search_query": r["search_query"],
            "track_count": r["track_count"],
            "url": r["url"],
            "source_kind": r["source_kind"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
    finally:
        if own:
            c.close()


def find_playlist_by_video_id(
    video_id: str,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Find saved playlists containing a given YouTube video ID."""
    if not video_id:
        return []
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        rows = c.execute(
            """
            SELECT p.playlist_id, p.name, p.search_query, p.track_count, p.url, p.source_kind,
                   p.created_at, p.updated_at, pt.position
            FROM saved_playlists p
            JOIN saved_playlist_tracks pt ON pt.playlist_id = p.playlist_id
            WHERE pt.video_id = %s
            ORDER BY p.updated_at DESC
            """,
            (video_id,),
        ).fetchall()
        return [
            {
                "playlist_id": r["playlist_id"],
                "name": r["name"],
                "search_query": r["search_query"],
                "track_count": r["track_count"],
                "url": r["url"],
                "source_kind": r["source_kind"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "position": r["position"],
            }
            for r in rows
        ]
    finally:
        if own:
            c.close()


def append_playlist_track(
    playlist_id: str,
    track: dict[str, Any],
    conn: Optional[Connection] = None,
) -> int:
    """Append a track to a saved playlist, incrementing track_count and returning new position."""
    if not playlist_id:
        return 0
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        row = c.execute(
            "SELECT COALESCE(MAX(position), 0) AS max_pos FROM saved_playlist_tracks WHERE playlist_id = %s",
            (playlist_id,),
        ).fetchone()
        new_pos = int(row["max_pos"] or 0) + 1
        now = time.time()
        c.execute(
            """
            INSERT INTO saved_playlist_tracks (playlist_id, position, track_id, artist, title, video_id, url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                playlist_id,
                new_pos,
                track.get("track_id"),
                str(track.get("artist") or ""),
                str(track.get("title") or ""),
                str(track.get("video_id") or ""),
                str(track.get("url") or ""),
            ),
        )
        c.execute(
            """
            UPDATE saved_playlists
            SET track_count = track_count + 1, updated_at = %s
            WHERE playlist_id = %s
            """,
            (now, playlist_id),
        )
        c.commit()
        return new_pos
    finally:
        if own:
            c.close()


def delete_saved_playlist(
    playlist_id: str,
    conn: Optional[Connection] = None,
) -> None:
    """Delete a saved playlist and its tracks from SQLite."""
    if not playlist_id:
        return
    own = conn is None
    c = conn or connect()
    try:
        ensure_saved_searches_and_playlists_schema(c)
        c.execute("DELETE FROM saved_playlists WHERE playlist_id = %s", (playlist_id,))
        c.execute("DELETE FROM saved_playlist_tracks WHERE playlist_id = %s", (playlist_id,))
        c.commit()
    finally:
        if own:
            c.close()


def start_radio_session(
    source: str = "mic",
    recording_id: Optional[int] = None,
    is_recording: bool = False,
    notes: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> int:
    """Create and start a new radio listening session in SQLite."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        started = time.time()
        cur = c.execute(
            """
            INSERT INTO radio_sessions (started_at, source, is_recording, recording_id, status, notes)
            VALUES (%s, %s, %s, %s, 'active', %s) RETURNING session_id
            """,
            (started, source, 1 if is_recording else 0, recording_id, notes),
        )
        ret_id = cur.fetchone()["session_id"]
        c.commit()
        return int(ret_id)
    finally:
        if own:
            c.close()


def record_radio_track(
    session_id: int,
    artist: str,
    title: str,
    *,
    offset_s: Optional[float] = None,
    has_synced: bool = False,
    lyric_source: str = "",
    conn: Optional[Connection] = None,
) -> int:
    """Record an identified track discovery/event in a radio session."""
    if not artist and not title:
        return 0
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        now = time.time()
        row = c.execute(
            """
            SELECT id, play_count FROM radio_session_tracks
            WHERE session_id = %s AND LOWER(artist) = LOWER(%s) AND LOWER(title) = LOWER(%s)
            """,
            (session_id, artist, title),
        ).fetchone()
        if row:
            track_entry_id = int(row["id"])
            c.execute(
                """
                UPDATE radio_session_tracks
                SET last_seen_at = %s, play_count = play_count + 1,
                    offset_s = COALESCE(%s, offset_s),
                    has_synced_lyrics = GREATEST(has_synced_lyrics, %s),
                    lyric_source = CASE WHEN lyric_source != '' THEN lyric_source ELSE %s END
                WHERE id = %s
                """,
                (now, offset_s, 1 if has_synced else 0, lyric_source, track_entry_id),
            )
        else:
            cur = c.execute(
                """
                INSERT INTO radio_session_tracks
                    (session_id, artist, title, first_seen_at, last_seen_at, play_count,
                     offset_s, has_synced_lyrics, lyric_source)
                VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s) RETURNING id
                """,
                (session_id, artist, title, now, now, offset_s, 1 if has_synced else 0, lyric_source),
            )
            track_entry_id = int(cur.fetchone()["id"])
            c.execute(
                "UPDATE radio_sessions SET track_count = track_count + 1 WHERE session_id = %s",
                (session_id,),
            )
        c.commit()
        return track_entry_id
    finally:
        if own:
            c.close()


def update_radio_session_recording(
    session_id: int,
    recording_id: Optional[int],
    is_recording: bool = True,
    conn: Optional[Connection] = None,
) -> None:
    """Update active recording linkage for a radio session."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        c.execute(
            """
            UPDATE radio_sessions
            SET is_recording = %s, recording_id = COALESCE(%s, recording_id)
            WHERE session_id = %s
            """,
            (1 if is_recording else 0, recording_id, session_id),
        )
        c.commit()
    finally:
        if own:
            c.close()


def finish_radio_session(
    session_id: int,
    status: str = "completed",
    notes: Optional[str] = None,
    conn: Optional[Connection] = None,
) -> None:
    """Mark a radio session as ended/completed."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        ended = time.time()
        cur = c.execute(
            "SELECT COUNT(DISTINCT id) AS cnt FROM radio_session_tracks WHERE session_id = %s",
            (session_id,),
        )
        r = cur.fetchone()
        cnt = r["cnt"] if r else 0
        c.execute(
            """
            UPDATE radio_sessions
            SET ended_at = %s, status = %s, track_count = %s,
                notes = COALESCE(%s, notes)
            WHERE session_id = %s
            """,
            (ended, status, cnt, notes, session_id),
        )
        c.commit()
    finally:
        if own:
            c.close()


def get_radio_sessions(
    limit: int = 50,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Return radio sessions ordered by start time descending."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        rows = c.execute(
            """
            SELECT s.session_id, s.started_at, s.ended_at, s.source, s.is_recording,
                   s.recording_id, s.track_count, s.status, s.notes,
                   r.dir AS recording_dir, r.status AS recording_status
            FROM radio_sessions s
            LEFT JOIN recordings r ON s.recording_id = r.recording_id
            ORDER BY s.started_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


def get_radio_session(
    session_id: int,
    conn: Optional[Connection] = None,
) -> Optional[dict[str, Any]]:
    """Return metadata for a single radio session."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        row = c.execute(
            """
            SELECT s.session_id, s.started_at, s.ended_at, s.source, s.is_recording,
                   s.recording_id, s.track_count, s.status, s.notes,
                   r.dir AS recording_dir, r.status AS recording_status
            FROM radio_sessions s
            LEFT JOIN recordings r ON s.recording_id = r.recording_id
            WHERE s.session_id = %s
            """,
            (session_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            c.close()


def get_radio_session_tracks(
    session_id: int,
    conn: Optional[Connection] = None,
) -> list[dict[str, Any]]:
    """Return tracks identified in a radio session."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        rows = c.execute(
            """
            SELECT id, session_id, artist, title, first_seen_at, last_seen_at,
                   play_count, offset_s, has_synced_lyrics, lyric_source,
                   imported, imported_track_id
            FROM radio_session_tracks
            WHERE session_id = %s
            ORDER BY first_seen_at ASC
            """,
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


def mark_radio_track_imported(
    session_track_id: int,
    imported_track_id: int,
    conn: Optional[Connection] = None,
) -> None:
    """Mark a track in a radio session as imported to library."""
    own = conn is None
    c = conn or connect()
    try:
        ensure_radio_session_schema(c)
        c.execute(
            """
            UPDATE radio_session_tracks
            SET imported = 1, imported_track_id = %s
            WHERE id = %s
            """,
            (imported_track_id, session_track_id),
        )
        c.commit()
    finally:
        if own:
            c.close()



