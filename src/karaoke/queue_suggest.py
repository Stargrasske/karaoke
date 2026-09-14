"""Suggest tracks that keep the vibe of a whole queue going.

Seeds on **every** track already in the queue rather than one, so the result
reflects the set's shared character instead of lurching toward whatever the last
song was. Each seed contributes its nearest neighbours (by sound), the neighbours
are pooled, and a track that is close to *several* queue members outranks one that
merely resembles a single outlier.

Similarity comes from the audio vectors, preferring CLAP (a learned timbre/genre
embedding) and falling back per-seed to the spectral vector when a track has no
CLAP embedding -- the same precedence :func:`karaoke.search.similar_main` uses.
This is query-by-example: it needs no lyrics, so it works for instrumentals too.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import search

# How many neighbours to pull per seed before pooling. Wider than the final
# list so a track can be reinforced by several seeds and still leave room after
# the already-queued members and the per-artist cap are removed.
PER_SEED = 20

# Cap on how often one artist may appear in the result. A queue heavy on one
# band otherwise returns more of that band -- technically similar, useless as
# "something new that fits".
PER_ARTIST = 2


@dataclass(frozen=True)
class Suggestion:
    """A suggested track and why it scored where it did."""

    track_id: int
    artist: str
    title: str
    score: float          # summed similarity across the seeds it matched
    seeds_matched: int    # how many queue members it was a neighbour of
    space: str            # "clap" or "spectral" -- which vector answered

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


def _seed_neighbours(track_id: int, k: int,
                     os_client: Any = None) -> tuple[list[search.SoundHit], str]:
    """Nearest neighbours for one seed, CLAP first then spectral, then acoustic CLAP genre.

    Returns the hits and which space answered, so the caller can report a
    result that mixed both rather than presenting two incomparable cosines as
    one scale.
    """
    hits = search.sounds_like_track(track_id, k=k, os_client=os_client)
    if hits:
        return hits, "clap"
    hits = search.similar_sounding(track_id, k=k, os_client=os_client)
    if hits:
        return hits, "spectral"
    # Fallback to acoustic CLAP classification in SQLite when vector search is unavailable
    try:
        from . import localcache
        with localcache.connect() as conn:
            g_row = localcache.genre_for(track_id, conn)
            if g_row and g_row["genre"]:
                seed_genre = str(g_row["genre"]).strip().casefold()
                rows = conn.execute(
                    """
                    SELECT t.track_id, t.artist, t.title, COALESCE(g.score, 0.8) as score
                    FROM tracks t
                    JOIN track_genre g ON g.track_id = t.track_id
                    WHERE lower(trim(g.genre)) = ? AND t.track_id != ?
                    ORDER BY g.score DESC
                    LIMIT ?
                    """,
                    (seed_genre, track_id, k),
                ).fetchall()
                if rows:
                    return [
                        search.SoundHit(
                            track_id=int(r["track_id"]),
                            artist=r["artist"] or "",
                            title=r["title"] or "",
                            similarity=float(r["score"] or 0.8),
                            source="clap_acoustic",
                        )
                        for r in rows
                    ], "clap_acoustic"
    except Exception:
        pass
    return [], "none"


def suggest_for_track(track_id: int, *, limit: int = 10,
                      per_artist: int = PER_ARTIST,
                      os_client: Any = None) -> list[Suggestion]:
    """Find tracks that acoustically sound like a single target track."""
    return suggest_for_queue([track_id], limit=limit, per_artist=per_artist, os_client=os_client)


def suggest_for_queue(track_ids: list[int], *, limit: int = 10,
                      per_artist: int = PER_ARTIST,
                      per_seed: int = PER_SEED,
                      os_client: Any = None) -> list[Suggestion]:
    """Tracks that fit a whole queue, best first.

    ``track_ids`` are the queue members to seed on. A candidate already in the
    queue is never suggested. Scores are summed across the seeds a candidate
    matched, so breadth of fit (close to many members) beats a single strong
    outlier.
    """
    if not track_ids:
        return []
    queued = set(track_ids)

    # track_id -> accumulated state while pooling.
    pooled: dict[int, dict[str, Any]] = {}
    for seed in track_ids:
        hits, space = _seed_neighbours(seed, per_seed, os_client=os_client)
        for hit in hits:
            if hit.track_id in queued:
                continue
            slot = pooled.get(hit.track_id)
            if slot is None:
                pooled[hit.track_id] = {
                    "artist": hit.artist, "title": hit.title,
                    "score": hit.similarity, "seeds": 1, "space": space,
                }
            else:
                slot["score"] += hit.similarity
                slot["seeds"] += 1

    ordered = sorted(
        pooled.items(),
        # Breadth of fit first (matched more seeds), then total closeness.
        key=lambda kv: (kv[1]["seeds"], kv[1]["score"]),
        reverse=True,
    )

    seen_artist: dict[str, int] = {}
    out: list[Suggestion] = []
    for tid, slot in ordered:
        artist_key = (slot["artist"] or "").casefold()
        if per_artist and seen_artist.get(artist_key, 0) >= per_artist:
            continue
        seen_artist[artist_key] = seen_artist.get(artist_key, 0) + 1
        out.append(Suggestion(
            track_id=tid,
            artist=slot["artist"],
            title=slot["title"],
            score=round(slot["score"], 4),
            seeds_matched=slot["seeds"],
            space=slot["space"],
        ))
        if len(out) >= limit:
            break
    return out


def playable_url(track_id: int, conn: sqlite3.Connection) -> Optional[str]:
    """A URL to play a suggested track from, preferring YouTube like browse."""
    row = conn.execute(
        "SELECT url FROM sources WHERE track_id = ?"
        " ORDER BY CASE WHEN url LIKE '%youtu%' THEN 0 ELSE 1 END, source_id"
        " LIMIT 1", (track_id,)).fetchone()
    return row["url"] if row and row["url"] else None
