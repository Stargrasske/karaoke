"""Build a playlist from tracks that feel alike.

The library already knows what each song is *about*: ``visuals.analyze_sentiment``
turns a lyric block into a mood breakdown, and the analysis row carries tempo and
energy. This puts the two together to answer "more like this one" — seeded by a
track you are playing, or by a mood you are in.

Matching is on the **shape** of the sentiment, not the dominant label. Two songs
can both come out "sad" while one is purely mournful and the other is half
furious; comparing the whole vector keeps those apart, where comparing labels
would file them together.

Sentiment is recomputed on demand rather than stored. It costs about 0.2ms per
track — the whole library profiles in well under a second — and a cache would
only create a way for it to disagree with the lyrics it came from.
"""
from __future__ import annotations

import math
import psycopg
from psycopg import Connection, Cursor
from dataclasses import dataclass
from typing import Optional

from . import visuals
from .localcache import ALBUM_UPLOAD_SECONDS

# Moods that carry direction. "neutral" is the absence of signal rather than a
# feeling to match on, so including it would make every unremarkable song look
# similar to every other one.
MATCH_MOODS = ("happy", "sad", "angry", "tender")

# Below this many mood words, the profile is noise. One hit gives a vector
# pointing entirely at one mood, which then matches anything in that direction
# perfectly — sparse lyrics would dominate every result.
MIN_HITS = 6

# How much tempo agreement counts next to sentiment. Small on purpose: this is
# a sentiment playlist, and tempo is there to stop it lurching between a dirge
# and a stomp that happen to share a mood.
TEMPO_WEIGHT = 0.25

# BPM difference at which the tempo term reaches zero.
TEMPO_SPAN = 60.0


@dataclass(frozen=True)
class Candidate:
    """A track that can take part in matching."""

    track_id: int
    artist: str
    title: str
    vector: tuple[float, ...]
    hits: int
    dominant: str
    bpm: Optional[float] = None
    energy: Optional[float] = None

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass(frozen=True)
class Match:
    """A candidate and how well it matched the seed."""

    candidate: Candidate
    score: float
    sentiment: float
    tempo: Optional[float]


def mood_vector(profile) -> tuple[float, ...]:
    """The sentiment as a vector over :data:`MATCH_MOODS`."""
    shares = profile.shares
    return tuple(float(shares.get(mood, 0.0)) for mood in MATCH_MOODS)


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Similarity of two mood vectors, 0..1.

    Cosine rather than distance because it compares the *balance* of moods and
    ignores how strongly worded a song is: a quietly sad lyric and a relentlessly
    sad one point the same way, and should match.
    """
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


# What an unknown tempo is worth. Neither a reward nor a penalty: a known
# close tempo should beat "we do not know", and "we do not know" should beat a
# known clash.
UNKNOWN_TEMPO = 0.5


def tempo_affinity(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """How close two tempos are, 0..1, or None when either is unknown."""
    if not a or not b:
        return None
    return max(0.0, 1.0 - abs(a - b) / TEMPO_SPAN)


def load_candidates(conn: Connection, *,
                    min_hits: int = MIN_HITS) -> list[Candidate]:
    """Every track with enough lyric signal to match on."""
    # track_analysis is created on demand, not by connect(), so a database that
    # has never had an analysis stored has no such table and the join below
    # would raise rather than simply finding no tempos.
    from . import track_analysis
    track_analysis.ensure_schema(conn)

    rows = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title,
               COALESCE(l.plain_lyrics, l.synced_lyrics, '') AS words,
               a.bpm, a.energy
        FROM tracks t
        JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'
        LEFT JOIN track_analysis a ON a.track_id = t.track_id
        WHERE length(COALESCE(l.plain_lyrics, l.synced_lyrics, '')) > 40
          -- A whole album is not a playlist entry, and its sentiment is the
          -- average of a dozen different songs.
          AND (t.duration IS NULL OR t.duration <= %s)
        """, (ALBUM_UPLOAD_SECONDS,)
    ).fetchall()

    out: list[Candidate] = []
    for row in rows:
        profile = visuals.analyze_sentiment(row["words"])
        if profile.total_hits < min_hits:
            continue
        out.append(Candidate(
            track_id=int(row["track_id"]),
            artist=row["artist"] or "",
            title=row["title"] or "",
            vector=mood_vector(profile),
            hits=profile.total_hits,
            dominant=profile.dominant,
            bpm=row["bpm"],
            energy=row["energy"],
        ))
    return out


def score(seed: Candidate, other: Candidate, *,
          tempo_weight: float = TEMPO_WEIGHT) -> Match:
    """Score one candidate against the seed."""
    sentiment = cosine(seed.vector, other.vector)
    tempo = tempo_affinity(seed.bpm, other.bpm)
    if tempo_weight <= 0:
        return Match(candidate=other, score=sentiment, sentiment=sentiment,
                     tempo=tempo)
    # An unknown tempo scores UNKNOWN_TEMPO rather than dropping the term.
    # Dropping it leaves the sentiment score untouched while every *analysed*
    # track gets blended toward a tempo term below 1.0 -- so tracks with no
    # analysis float to the top purely for having less known about them, which
    # is the opposite of what the tempo term is for.
    effective = UNKNOWN_TEMPO if tempo is None else tempo
    total = sentiment * (1.0 - tempo_weight) + effective * tempo_weight
    return Match(candidate=other, score=total, sentiment=sentiment, tempo=tempo)


def similar_to(seed: Candidate, pool: list[Candidate], *, limit: int = 20,
               tempo_weight: float = TEMPO_WEIGHT,
               per_artist: int = 2) -> list[Match]:
    """The closest matches to a seed track, best first.

    ``per_artist`` caps how often one artist may appear. Without it a playlist
    seeded on one album returns that album: the lyrics of a record are written
    in one voice, so they score alike, and the result is technically correct and
    useless as a playlist.
    """
    matches = [score(seed, other, tempo_weight=tempo_weight)
               for other in pool if other.track_id != seed.track_id]
    matches.sort(key=lambda m: m.score, reverse=True)

    seen: dict[str, int] = {}
    picked: list[Match] = []
    for match in matches:
        artist = match.candidate.artist.casefold()
        if per_artist and seen.get(artist, 0) >= per_artist:
            continue
        seen[artist] = seen.get(artist, 0) + 1
        picked.append(match)
        if len(picked) >= limit:
            break
    return picked


def mood_seed(mood: str) -> Candidate:
    """A synthetic seed pointing purely at one mood."""
    vector = tuple(1.0 if m == mood else 0.0 for m in MATCH_MOODS)
    return Candidate(track_id=-1, artist="", title=f"({mood})",
                     vector=vector, hits=MIN_HITS, dominant=mood)


def find_seed(pool: list[Candidate], artist: str, title: str) -> Optional[Candidate]:
    """Locate a seed track in the pool by name, loosely."""
    from .localcache import _artist_key, _title_keys

    want_artist, want_titles = _artist_key(artist), _title_keys(title)
    for candidate in pool:
        if (_artist_key(candidate.artist) == want_artist
                and _title_keys(candidate.title) & want_titles):
            return candidate
    return None


def playlist_lines(matches: list[Match]) -> list[str]:
    """Human-readable rows for the CLI."""
    out = []
    for i, match in enumerate(matches, 1):
        cand = match.candidate
        tempo = f"{cand.bpm:.0f}bpm" if cand.bpm else "  ?   "
        out.append(f"{i:>3}. {match.score:.3f}  {cand.dominant:<7} {tempo:>7}  "
                   f"{cand.label[:52]}")
    return out


def urls_for(matches: list[Match], conn: Connection) -> list[tuple[str, str]]:
    """(label, url) for each match that has somewhere to play from."""
    out = []
    for match in matches:
        row = conn.execute(
            "SELECT url FROM sources WHERE track_id = %s"
            " ORDER BY CASE WHEN url LIKE '%%youtu%%' THEN 0 ELSE 1 END, source_id"
            " LIMIT 1", (match.candidate.track_id,)).fetchone()
        if row and row["url"]:
            out.append((match.candidate.label, row["url"]))
    return out


def smartlist_main(argv: Optional[list[str]] = None) -> int:
    """Run the ``karaoke-smartlist`` CLI."""
    import argparse

    from . import localcache

    ap = argparse.ArgumentParser(
        prog="karaoke-smartlist",
        description="Build a playlist of tracks that feel like a seed track "
                    "or a mood")
    ap.add_argument("--artist", default="", help="seed track artist")
    ap.add_argument("--title", default="", help="seed track title")
    ap.add_argument("--mood", default="",
                    help=f"seed on a mood instead: {', '.join(MATCH_MOODS)}")
    ap.add_argument("--playing", action="store_true",
                    help="seed on whatever is playing right now")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--per-artist", type=int, default=2,
                    help="cap tracks per artist (0 = no cap)")
    ap.add_argument("--tempo-weight", type=float, default=TEMPO_WEIGHT,
                    help="how much tempo agreement counts (0 = sentiment only)")
    ap.add_argument("--m3u", default="", help="also write a playlist file here")
    args = ap.parse_args(argv)

    artist, title = args.artist, args.title
    if args.playing:
        from . import detect
        det = detect.detect_active()
        if not (det.artist and det.title):
            print("nothing is playing", file=__import__("sys").stderr)
            return 1
        artist, title = det.artist, det.title
        print(f"seeding on what is playing: {artist} - {title}\n")

    with localcache.connect() as conn:
        pool = load_candidates(conn)
        if not pool:
            print("no tracks have enough lyric signal to match on")
            return 1

        if args.mood:
            if args.mood not in MATCH_MOODS:
                ap.error(f"--mood must be one of {', '.join(MATCH_MOODS)}")
            seed = mood_seed(args.mood)
        elif artist and title:
            seed = find_seed(pool, artist, title)
            if seed is None:
                print(f"seed not found (or too few mood words): {artist} - {title}",
                      file=__import__("sys").stderr)
                return 1
        else:
            ap.error("give --artist/--title, --mood, or --playing")

        matches = similar_to(seed, pool, limit=args.limit,
                             tempo_weight=args.tempo_weight,
                             per_artist=args.per_artist)
        print(f"{len(pool)} tracks in the pool; closest to {seed.label or seed.title}:\n")
        print("\n".join(playlist_lines(matches)))

        if args.m3u:
            entries = urls_for(matches, conn)
            with open(args.m3u, "w", encoding="utf-8") as handle:
                handle.write("#EXTM3U\n")
                for label, url in entries:
                    handle.write(f"#EXTINF:-1,{label}\n{url}\n")
            print(f"\nwrote {len(entries)} playable entries to {args.m3u}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(smartlist_main())
