"""Weighted search across the library.

One box, ranked results. A query is matched against several fields and the
matches are weighted, so typing a song name finds the song rather than every
track by an artist whose name happens to contain the same letters.

Field priority, highest first:

1. **title** — what you almost always mean
2. **album** — narrower than an artist, so a hit is more deliberate
3. **artist** — broad; matches everything they recorded
4. **lyrics** — the fallback for "I only remember a line of it"

Position counts as well as presence: an exact title beats a title that starts
with the query, which beats one that merely contains it. Without that,
searching "river" ranks *Riverside* and *The River* identically, and the one
you meant is wherever the database happened to put it.

Note that album is sparsely populated in practice — most rows arrive from
player metadata that omits it — so that weight rarely fires today. It costs
nothing to keep, and it starts working the moment albums are filled in.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import localcache

# Relative worth of a hit in each field.
W_TITLE = 1.0
W_ALBUM = 0.6
W_ARTIST = 0.4
W_GENRE = 0.35
W_LYRICS = 0.2

# Lyrics that are Whisper's own guess at the words, with nothing corroborating
# them. They are worth having -- for an album no source carries they are the
# only text there is -- but they are not evidence in the way a real lyric is,
# and matching one should not outrank matching a real one.
#
# The failure is not subtle when it happens. Einstuerzende Neubauten's
# "Installation N deg 1" is stored as
# "HerausforderDisobeylarationDisobeyedzyIt's the lawczywiscieDisobey not":
# German and Polish fragments fused without spaces, from a track that is mostly
# industrial noise.
TRANSCRIBED_SOURCE = "whisper"

# Sources whose *words* are Whisper's guess, however the timings were obtained.
#
# `whisper_synced` is a transcription that has since been given timings by
# aligning it against its own audio. That is a real improvement -- it becomes
# singable -- but the words are no better corroborated than before, and storing
# it as `whisper_aligned` would have hidden that: `whisper_aligned` means real
# words with Whisper timings, and it is deliberately not demoted. Timing a
# guess must not launder it into evidence.
TRANSCRIBED_SOURCES = (TRANSCRIBED_SOURCE, "whisper_synced")

# What a lyric match is worth when the words are a transcription. Not zero:
# 69 tracks in the library have no other text, and excluding them outright
# would make them unfindable by their words at all. Low enough that any real
# lyric match wins.
TRANSCRIBED_PENALTY = 0.25


def is_transcribed(source: str) -> bool:
    """Whether a lyric source is Whisper guessing at the words.

    Deliberately not "does the source mention whisper". ``whisper_aligned``
    means the *words* came from a real source and only the timings are
    Whisper's, so those 94 tracks carry text as trustworthy as any other and
    must not be penalised. Only the ones with no corroborating source are --
    whether or not they have since been given timings, which improves how they
    play without making the words any more likely to be right.
    """
    return (source or "").strip().casefold() in TRANSCRIBED_SOURCES

# How a hit within a field scores, by how deliberate the match looks.
EXACT = 1.0
PREFIX = 0.75     # starts with the query, at a word boundary
WORD = 0.6        # the query appears as a whole word
PARTIAL = 0.45    # starts with the query mid-word ("Riverside" for "river")
CONTAINS = 0.35   # appears somewhere, inside a word

# Lyrics are long, so a substring hit there says less than one in a title.
# Only whole-word matches count, or "love" matches "glove" in every song.
_WORD_CACHE_LIMIT = 400


@dataclass(frozen=True)
class Hit:
    """One search result and why it matched."""

    track_id: int
    artist: str
    title: str
    album: str
    score: float
    fields: tuple[str, ...]      # which fields contributed, best first
    genre: str = ""
    broad_genres: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


def _normalise(text: str) -> str:
    return " ".join((text or "").casefold().split())


def field_score(value: str, query: str) -> float:
    """How well one field matches, 0..1."""
    haystack = _normalise(value)
    needle = _normalise(query)
    if not haystack or not needle:
        return 0.0
    if haystack == needle:
        return EXACT
    # Word boundaries are checked before a bare prefix, or "Riverside" scores
    # the same as "River Deep" for "river" -- both merely start with it, but
    # only one of them is actually the word you typed.
    if re.match(rf"{re.escape(needle)}\b", haystack):
        return PREFIX
    if re.search(rf"\b{re.escape(needle)}\b", haystack):
        return WORD
    if haystack.startswith(needle):
        return PARTIAL
    if needle in haystack:
        return CONTAINS

    # Fuzzy matching for single words and multi-word phrases
    import difflib
    haystack_words = haystack.split()
    words = [w for w in needle.split() if w]
    if len(words) == 1:
        w = words[0]
        best_ratio = 0.0
        for hw in haystack_words:
            if len(hw) >= 3 and len(w) >= 3:
                r = difflib.SequenceMatcher(None, w, hw).ratio()
                if r > best_ratio:
                    best_ratio = r
        if best_ratio >= 0.72:
            return WORD * best_ratio * 0.9
        r_all = difflib.SequenceMatcher(None, needle, haystack).ratio()
        if r_all >= 0.72:
            return PARTIAL * r_all * 0.85
    else:
        meaningful_words = [w for w in words if len(w) > 2 or w not in ("of", "the", "a", "in", "and", "or", "to", "on", "at", "by", "for", "with", "is", "it")]
        if not meaningful_words:
            meaningful_words = words
        matched_count = 0
        for w in meaningful_words:
            if any(w in hw or (len(w) >= 3 and len(hw) >= 3 and difflib.SequenceMatcher(None, w, hw).ratio() >= 0.72) for hw in haystack_words):
                matched_count += 1
        if matched_count > 0:
            return WORD * (matched_count / len(meaningful_words)) * 0.9

    return 0.0


def lyrics_score(text: str, query: str) -> float:
    """Whether the query appears in the lyrics as a whole word.

    Substring matching is deliberately not used here: in a body of text
    "love" would match "glove" and "clover", and the lyric field would then
    match nearly everything at its low weight rather than nothing.
    """
    haystack = _normalise(text)
    needle = _normalise(query)
    if not haystack or not needle:
        return 0.0
    if re.search(rf"\b{re.escape(needle)}\b", haystack):
        return WORD

    # Fallback for multi-word search in lyrics
    words = [w for w in needle.split() if w]
    if len(words) > 1:
        meaningful_words = [w for w in words if len(w) > 2 or w not in ("of", "the", "a", "in", "and", "or", "to", "on", "at", "by", "for", "with", "is", "it")]
        if not meaningful_words:
            meaningful_words = words
        matched_count = sum(1 for w in meaningful_words if re.search(rf"\b{re.escape(w)}\b", haystack))
        if matched_count > 0:
            return WORD * (matched_count / len(meaningful_words)) * 0.8

    return 0.0


def score_row(row, query: str, *, search_lyrics: bool = True) -> tuple[float, tuple[str, ...]]:
    """Total weighted score for one track, and the fields that contributed."""
    parts: list[tuple[float, str]] = []

    title = field_score(row["title"] or "", query) * W_TITLE
    if title:
        parts.append((title, "title"))
    album = field_score(row["album"] or "", query) * W_ALBUM
    if album:
        parts.append((album, "album"))
    artist = field_score(row["artist"] or "", query) * W_ARTIST
    if artist:
        parts.append((artist, "artist"))

    # Genre matching: check broad genres, artist genres, and audio genre
    keys = row.keys() if hasattr(row, "keys") else ()
    g_score = 0.0
    if "broad_genres" in keys and row["broad_genres"]:
        bgs = row["broad_genres"]
        if isinstance(bgs, str):
            bgs = [bgs]
        for bg in bgs:
            s = field_score(bg, query)
            if s > g_score:
                g_score = s
    if "artist_genres" in keys and row["artist_genres"]:
        ags = row["artist_genres"]
        if isinstance(ags, str):
            ags = [ags]
        for ag in ags:
            s = field_score(ag, query) * 0.9
            if s > g_score:
                g_score = s
    if "genre" in keys and row["genre"]:
        s = field_score(str(row["genre"]), query) * 0.8
        if s > g_score:
            g_score = s
    if g_score:
        parts.append((g_score * W_GENRE, "genre"))

    if search_lyrics:
        words = lyrics_score(row["words"] if "words" in row.keys() else "", query)
        if words:
            guessed = is_transcribed(row["lyrics_source"]
                                     if "lyrics_source" in keys else "")
            weight = W_LYRICS * (TRANSCRIBED_PENALTY if guessed else 1.0)
            parts.append((words * weight,
                          "lyrics?" if guessed else "lyrics"))

    if not parts:
        # Cross-field matching fallback for multi-word queries (e.g. matching "Led Zeppelin Houses Holy")
        combined_meta = f"{row['artist'] or ''} {row['title'] or ''} {row['album'] or ''}"
        combined_normalized = _normalise(combined_meta)
        normalized_query = _normalise(query)
        words = [w for w in normalized_query.split() if w]
        if len(words) > 1:
            meaningful_words = [w for w in words if len(w) > 2 or w not in ("of", "the", "a", "in", "and", "or", "to", "on", "at", "by", "for", "with", "is", "it")]
            if not meaningful_words:
                meaningful_words = words
            matched_count = sum(1 for w in meaningful_words if re.search(rf"\b{re.escape(w)}\b", combined_normalized))
            if matched_count > 0:
                contributed_fields = []
                if any(re.search(rf"\b{re.escape(w)}\b", _normalise(row["title"] or "")) for w in meaningful_words):
                    contributed_fields.append("title")
                if any(re.search(rf"\b{re.escape(w)}\b", _normalise(row["artist"] or "")) for w in meaningful_words):
                    contributed_fields.append("artist")
                if any(re.search(rf"\b{re.escape(w)}\b", _normalise(row["album"] or "")) for w in meaningful_words):
                    contributed_fields.append("album")
                ratio = matched_count / len(meaningful_words)
                cross_score = WORD * ratio * W_TITLE * 0.85
                return (cross_score, tuple(contributed_fields))

    if not parts:
        return (0.0, ())
    parts.sort(reverse=True)
    # Summed, not maxed: a track matching on both title and artist is a better
    # answer than one matching on either alone, and should outrank it.
    return (sum(score for score, _ in parts), tuple(name for _, name in parts))


def search(query: str, conn: sqlite3.Connection, *, limit: int = 25,
           genre: Optional[str] = None,
           search_lyrics: bool = True) -> list[Hit]:
    """Ranked matches for a query, best first."""
    if not _normalise(query):
        return []

    where_clauses = ["(t.duration IS NULL OR t.duration <= :album_seconds)"]
    params: dict[str, object] = {"album_seconds": localcache.ALBUM_UPLOAD_SECONDS}

    has_track_genre = True
    try:
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_genre'")
        if not cur.fetchone():
            has_track_genre = False
    except Exception:
        has_track_genre = False

    has_artist_genres = True
    try:
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artist_genres'")
        if not cur.fetchone():
            has_artist_genres = False
    except Exception:
        has_artist_genres = False

    artist_genres_map = localcache.get_all_artist_genres_map(conn) if has_artist_genres else {}

    if genre and str(genre).strip().casefold() != "all":
        selected_genre = str(genre).strip().casefold()
        params["genre"] = selected_genre
        conditions: list[str] = []
        if has_track_genre and has_artist_genres:
            conditions.append(
                """(
                    EXISTS (
                        SELECT 1 FROM artist_genres ag
                        WHERE (lower(trim(ag.broad_genre)) = :genre OR lower(trim(ag.genre)) = :genre)
                          AND ag.artist_normalized = lower(trim(t.artist))
                    )
                    OR (
                        NOT EXISTS (
                            SELECT 1 FROM artist_genres ag
                            WHERE ag.artist_normalized = lower(trim(t.artist))
                        )
                        AND EXISTS (
                            SELECT 1 FROM track_genre g
                            WHERE g.track_id = t.track_id AND lower(trim(g.genre)) = :genre
                        )
                    )
                )"""
            )
        elif has_artist_genres:
            conditions.append(
                """EXISTS (
                    SELECT 1 FROM artist_genres ag
                    WHERE (lower(trim(ag.broad_genre)) = :genre OR lower(trim(ag.genre)) = :genre)
                      AND ag.artist_normalized = lower(trim(t.artist))
                )"""
            )
        elif has_track_genre:
            conditions.append(
                "EXISTS (SELECT 1 FROM track_genre g WHERE g.track_id = t.track_id AND lower(trim(g.genre)) = :genre)"
            )
        if conditions:
            where_clauses.append(f"({' OR '.join(conditions)})")
        else:
            return []

    genre_col = "COALESCE(g.genre, '') AS genre" if has_track_genre else "'' AS genre"
    join_genre = "LEFT JOIN track_genre g ON g.track_id = t.track_id" if has_track_genre else ""

    rows = conn.execute(
        f"""
        SELECT t.track_id, t.artist, t.title, COALESCE(t.album, '') AS album,
               COALESCE(l.plain_lyrics, l.synced_lyrics, '') AS words,
               COALESCE(l.source, '') AS lyrics_source,
               {genre_col}
        FROM tracks t
        {join_genre}
        LEFT JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'
        -- A full-album upload is not something you can pick and play.
        WHERE {' AND '.join(where_clauses)}
        """, params
    ).fetchall()

    cols = ["track_id", "artist", "title", "album", "words", "lyrics_source"]
    if has_track_genre:
        cols.append("genre")

    hits: list[Hit] = []
    for row in rows:
        if hasattr(row, "keys"):
            row_dict = {k: row[k] for k in row.keys()}
        else:
            row_dict = dict(zip(cols, row))
        norm_artist = _normalise(row_dict.get("artist") or "")
        a_info = artist_genres_map.get(norm_artist, {})
        row_dict["broad_genres"] = a_info.get("broad", [])
        row_dict["artist_genres"] = a_info.get("specific", [])

        score, fields = score_row(row_dict, query, search_lyrics=search_lyrics)
        if score <= 0:
            continue
        g_val = row_dict.get("genre") or ""
        hits.append(Hit(track_id=int(row_dict["track_id"]), artist=row_dict["artist"] or "",
                        title=row_dict["title"] or "", album=row_dict["album"] or "",
                        score=score, fields=fields, genre=g_val,
                        broad_genres=tuple(row_dict["broad_genres"])))
    hits.sort(key=lambda h: (-h.score, h.artist.casefold(), h.title.casefold()))
    return hits[:limit]


def playable_url(track_id: int, conn: sqlite3.Connection) -> Optional[str]:
    """Somewhere to play a track from, preferring a browser-openable source."""
    row = conn.execute(
        "SELECT url FROM sources WHERE track_id = ?"
        " ORDER BY CASE WHEN url LIKE '%youtu%' THEN 0"
        "               WHEN url LIKE 'http%' THEN 1 ELSE 2 END, source_id"
        " LIMIT 1", (track_id,)).fetchone()
    return row["url"] if row and row["url"] else None
