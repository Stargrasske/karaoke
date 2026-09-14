"""Semantic + keyword search over the tracks index."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .config import settings
from .librarysearch import TRANSCRIBED_SOURCE, is_transcribed

# How much a match is worth when the "lyrics" are Whisper's own guess at the
# words. Shared rule with librarysearch so the two search paths cannot drift
# into disagreeing about which tracks are trustworthy.
TRANSCRIBED_BOOST = 0.25


@dataclass
class SearchHit:
    """One normalized search result from OpenSearch."""

    score: float
    artist: str
    title: str
    album: str
    source: str
    has_synced: bool
    path: Optional[str] = None
    # Whether the words are Whisper's guess rather than a real lyric. Carried
    # on the hit so a caller can say so, instead of every caller having to know
    # which source strings mean "transcribed".
    transcribed: bool = False
    dominant_mood: str = "neutral"
    broad_genres: list[str] = field(default_factory=list)
    artist_genres: list[str] = field(default_factory=list)
    audio_genre: str = ""


def _hit(raw: dict[str, Any]) -> SearchHit:
    s = raw["_source"]
    return SearchHit(
        score=raw.get("_score", 0.0),
        artist=s.get("artist", ""),
        title=s.get("title", ""),
        album=s.get("album", ""),
        source=s.get("source", ""),
        has_synced=bool(s.get("has_synced")),
        path=s.get("path"),
        transcribed=is_transcribed(s.get("lyrics_source", "")),
        dominant_mood=s.get("dominant_mood") or "neutral",
        broad_genres=list(s.get("broad_genres") or []),
        artist_genres=list(s.get("artist_genres") or []),
        audio_genre=str(s.get("audio_genre") or ""),
    )


def _mood_filter(mood: str) -> dict[str, Any]:
    val = mood.strip().lower()
    return {
        "bool": {
            "should": [
                {"term": {"dominant_mood": val}},
                {"term": {"dominant_mood.keyword": val}},
            ],
            "minimum_should_match": 1,
        }
    }


def _genre_filter(genre: str) -> dict[str, Any]:
    val = genre.strip().lower()
    return {
        "bool": {
            "should": [
                {"term": {"broad_genres": val}},
                {"term": {"artist_genres": val}},
                {"term": {"audio_genre": val}},
            ],
            "minimum_should_match": 1,
        }
    }


def semantic_search(
    query: str,
    k: int = 5,
    mood: Optional[str] = None,
    genre: Optional[str] = None,
    os_client: Any = None,
) -> list[SearchHit]:
    """kNN search over lyric vectors: 'find the song that goes like ...'."""
    from .embed import embed_text
    from .osclient import client

    c = os_client or client()
    vec = embed_text(query)
    knn_clause: dict[str, Any] = {"vector": vec, "k": k}
    filters: list[dict[str, Any]] = []
    if mood:
        filters.append(_mood_filter(mood))
    if genre and genre.strip().lower() != "all":
        filters.append(_genre_filter(genre))
    if len(filters) == 1:
        knn_clause["filter"] = filters[0]
    elif len(filters) > 1:
        knn_clause["filter"] = {"bool": {"filter": filters}}

    body = {"size": k, "query": {"knn": {"lyrics_vector": knn_clause}}}
    res = c.search(index=settings.index_name, body=body)
    return [_hit(h) for h in res["hits"]["hits"]]


def keyword_search(
    query: str,
    k: int = 5,
    mood: Optional[str] = None,
    genre: Optional[str] = None,
    os_client: Any = None,
) -> list[SearchHit]:
    """Full-text search across title/artist/broad_genres/artist_genres/audio_genre/album/plain lyrics."""
    from .osclient import client

    c = os_client or client()
    positive: dict[str, Any] = {
        "multi_match": {
            "query": query,
            "fields": [
                "title^2.5",
                "artist^2.0",
                "broad_genres^2.0",
                "artist_genres^1.5",
                "audio_genre^1.0",
                "album",
                "plain_lyrics^0.5",
            ],
        }
    }
    filters: list[dict[str, Any]] = []
    if mood:
        filters.append(_mood_filter(mood))
    if genre and genre.strip().lower() != "all":
        filters.append(_genre_filter(genre))

    if filters:
        positive = {
            "bool": {
                "must": positive,
                "filter": filters,
            }
        }
    body = {
        "size": k,
        "query": {
            "boosting": {
                "positive": positive,
                # Demoted, not filtered. A transcription is the only text 69
                # tracks have, so removing them would make those songs
                # unfindable by their words; they simply must not outrank a
                # track whose lyrics are real. `whisper_aligned` is untouched:
                # there the words came from a real source and only the timings
                # are Whisper's.
                "negative": {"term": {"lyrics_source": TRANSCRIBED_SOURCE}},
                "negative_boost": TRANSCRIBED_BOOST,
            }
        },
    }
    res = c.search(index=settings.index_name, body=body)
    return [_hit(h) for h in res["hits"]["hits"]]


def hybrid_search(
    query: str,
    k: int = 5,
    mood: Optional[str] = None,
    genre: Optional[str] = None,
    os_client: Any = None,
) -> list[SearchHit]:
    """Hybrid semantic + keyword search with cached boolean mood and genre filter."""
    from .embed import embed_text
    from .osclient import client

    c = os_client or client()
    vec = embed_text(query)

    filter_clauses: list[dict[str, Any]] = []
    if mood:
        filter_clauses.append(_mood_filter(mood))
    if genre and genre.strip().lower() != "all":
        filter_clauses.append(_genre_filter(genre))

    knn_clause: dict[str, Any] = {"vector": vec, "k": k}
    if filter_clauses:
        knn_clause["filter"] = filter_clauses[0] if len(filter_clauses) == 1 else {"bool": {"filter": filter_clauses}}

    body = {
        "size": k,
        "query": {
            "bool": {
                "should": [
                    {
                        "boosting": {
                            "positive": {
                                "multi_match": {
                                    "query": query,
                                    "fields": [
                                        "title^2.5",
                                        "artist^2.0",
                                        "broad_genres^2.0",
                                        "artist_genres^1.5",
                                        "audio_genre^1.0",
                                        "album",
                                        "plain_lyrics^0.5",
                                    ],
                                }
                            },
                            "negative": {"term": {"lyrics_source": TRANSCRIBED_SOURCE}},
                            "negative_boost": TRANSCRIBED_BOOST,
                        }
                    },
                    {"knn": {"lyrics_vector": knn_clause}},
                ],
                "filter": filter_clauses,
            }
        },
    }
    res = c.search(index=settings.index_name, body=body)
    return [_hit(h) for h in res["hits"]["hits"]]


# What cosine similarity between *unrelated* tracks actually looks like,
# measured over 3000 random pairs of library vectors:
#
#     min 0.777   p5 0.885   median 0.965   p95 0.986   max 0.999
#
# Every vector sits in a narrow cone, so a raw score reads far more confident
# than it is: 0.99 is not "nearly identical", it is the top few percent. These
# thresholds exist so a caller can say that rather than print a number that
# invites the wrong conclusion. Re-measure if the vector composition changes.
SIMILARITY_TYPICAL = 0.965      # median between unrelated tracks
SIMILARITY_NOTABLE = 0.986      # p95: closer than 19 of 20 random pairs
SIMILARITY_STRIKING = 0.993     # rarer still


def describe_similarity(score: float) -> str:
    """Plain words for a cosine, given how compressed the range is."""
    if score >= SIMILARITY_STRIKING:
        return "very close"
    if score >= SIMILARITY_NOTABLE:
        return "close"
    if score >= SIMILARITY_TYPICAL:
        return "somewhat"
    return "unremarkable"


@dataclass
class SoundHit:
    """One "sounds like" result: a track, and how close it sounded."""

    track_id: int
    artist: str
    title: str
    similarity: float          # cosine, 0..1 -- the vectors are unit length
    source: str                # library (the release) or recording (a capture)
    detected_key: str = ""
    bpm: Optional[float] = None


def audio_vector_for(track_id: int, os_client: Any = None) -> Optional[list[float]]:
    """A track's stored sound vector, preferring the release over a capture.

    A track can have both: a recording describes one performance heard through
    a speaker and taken off a monitor, while the library document is the
    release itself. The release is the cleaner query -- a capture carries the
    room, the speaker and the codec of that particular evening.
    """
    from . import audio_vector
    from .osclient import client

    c = os_client or client()
    try:
        res = c.search(index=audio_vector.AUDIO_INDEX, body={
            "size": 1,
            "query": {"bool": {
                "must": [{"term": {"track_id": track_id}}],
                "should": [{"term": {"source": "library"}}],
            }},
            "sort": [{"_score": "desc"}],
        })
    except Exception:
        return None
    hits = res["hits"]["hits"]
    return hits[0]["_source"].get("audio_vector") if hits else None


def similar_sounding(track_id: int, k: int = 10,
                     os_client: Any = None) -> list[SoundHit]:
    """Tracks whose audio resembles this one, nearest first.

    This is query-by-example rather than by text, because a 62-dimension timbre
    and harmony vector shares no space with a sentence embedding -- there is no
    sensible way to type a query into it. What it answers is the question the
    lyric index cannot: *what does this sound like*, which for an instrumental
    is the only question there is.

    Results are deduplicated by track: a song present as both a release and a
    capture would otherwise occupy two of the k slots with itself.
    """
    from . import audio_vector
    from .osclient import client

    vector = audio_vector_for(track_id, os_client)
    if vector is None:
        return []

    c = os_client or client()
    try:
        # Over-fetch: the query track's own documents and any duplicate
        # observations come back too, and are dropped below.
        res = c.search(index=audio_vector.AUDIO_INDEX, body={
            "size": max(k * 3, k + 5),
            "query": {"knn": {"audio_vector": {"vector": vector,
                                               "k": max(k * 3, k + 5)}}},
        })
    except Exception:
        return []

    seen: set[int] = {track_id}
    out: list[SoundHit] = []
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        tid = int(src.get("track_id", 0))
        if tid in seen:
            continue
        seen.add(tid)
        out.append(SoundHit(
            track_id=tid,
            artist=src.get("artist", ""),
            title=src.get("title", ""),
            # OpenSearch returns 1/(1+distance) for cosine; the stored vectors
            # are unit length, so reporting the raw score would be a different
            # number from the cosine everything else in this project uses.
            similarity=audio_vector.similarity(vector, src["audio_vector"])
            if src.get("audio_vector") else 0.0,
            source=src.get("source", ""),
            detected_key=src.get("detected_key", "") or "",
            bpm=src.get("bpm"),
        ))
        if len(out) >= k:
            break
    return out


def find_track(artist: str, title: str, os_client: Any = None) -> Optional[dict[str, Any]]:
    """Look up a specific indexed track by EXACT artist+title (cache lookup).

    Uses case-insensitive exact matching on both fields so a fuzzy title
    collision can't return the wrong track's cached lyrics. Falls back to a
    title-only exact match when no artist is supplied.
    """
    from .osclient import client

    c = os_client or client()
    must: list[dict[str, Any]] = [
        {"match_phrase": {"title": title}},
    ]
    if artist:
        must.append({"match_phrase": {"artist": artist}})
    body = {"size": 1, "query": {"bool": {"must": must}}}
    res = c.search(index=settings.index_name, body=body)
    hits = res["hits"]["hits"]
    if not hits:
        return None
    src = hits[0]["_source"]
    # Guard: confirm the returned title actually matches (case-insensitive).
    if src.get("title", "").strip().lower() != title.strip().lower():
        return None
    if artist and src.get("artist", "").strip().lower() != artist.strip().lower():
        return None
    return src


def similar_main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    """CLI: tracks that sound like a given one.

    Query by example, because a timbre vector shares no space with text --
    there is nothing sensible to type into it. The reported closeness is
    described in words as well as numbers, since the raw cosine is compressed
    into a narrow band and a bare "0.99" invites the wrong conclusion.
    """
    import argparse

    from . import localcache

    ap = argparse.ArgumentParser(
        prog="karaoke-similar",
        description="Find tracks whose audio resembles a given track")
    ap.add_argument("query", help="artist and title, or a track id")
    ap.add_argument("-k", type=int, default=8, help="how many to show")
    args = ap.parse_args(argv)

    conn = localcache.connect()
    try:
        if args.query.isdigit():
            track_id = int(args.query)
            row = conn.execute("SELECT artist, title FROM tracks WHERE track_id = ?",
                               (track_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT track_id, artist, title FROM tracks"
                " WHERE (artist || ' ' || title) LIKE ? COLLATE NOCASE"
                " ORDER BY length(artist || title) LIMIT 1",
                (f"%{args.query}%",)).fetchone()
            track_id = int(row["track_id"]) if row else 0
        if row is None:
            print(f"no track matching {args.query!r}")
            return 1
    finally:
        conn.close()

    print(f"sounds like: {row['artist']} - {row['title']}\n")
    # CLAP where the track has an embedding; the spectral vector otherwise.
    # They are different spaces, so the CLI says which one answered rather
    # than presenting two incomparable scores as though they were one scale.
    hits = sounds_like_track(track_id, k=args.k)
    space = "clap"
    if not hits:
        hits = similar_sounding(track_id, k=args.k)
        space = "spectral"
    if not hits:
        print("No sound vector for that track.")
        print("Vectors come from record-mode analysis or scripts/vectorize_cached.py,")
        print("so a track with neither has nothing to compare.")
        return 0

    for hit in hits:
        key = hit.detected_key or ""
        bpm = f"{hit.bpm:5.0f}" if hit.bpm else "     "
        print(f"  {hit.similarity:.3f} {describe_similarity(hit.similarity):<13} "
              f"{hit.artist[:22]:24} {hit.title[:26]:28} {key:9} {bpm} "
              f"{hit.source}")

    if space == "clap":
        from .clap_vector import SIMILARITY_NOTABLE as CN
        from .clap_vector import SIMILARITY_TYPICAL as CT

        print(f"\n[clap] unrelated tracks sit near {CT:.2f}; {CN:.2f} is the "
              "95th percentile.")
    else:
        print(f"\n[spectral] between unrelated tracks the median is "
              f"{SIMILARITY_TYPICAL:.3f} and the 95th percentile "
              f"{SIMILARITY_NOTABLE:.3f},")
        print("so treat anything below 'close' as ordinary rather than a match.")
    return 0


def clap_vector_for(track_id: int, os_client: Any = None) -> Optional[list[float]]:
    """A track's CLAP embedding, or None if it has not been embedded."""
    from . import clap_vector
    from .osclient import client

    c = os_client or client()
    try:
        res = c.search(index=clap_vector.CLAP_INDEX, body={
            "size": 1, "query": {"term": {"track_id": track_id}}})
    except Exception:
        return None
    hits = res["hits"]["hits"]
    return hits[0]["_source"].get("clap_vector") if hits else None


def _clap_hits(res, exclude: set[int], k: int,
               query_vector: list[float]) -> list[SoundHit]:
    """Turn a CLAP kNN response into hits, one per track."""
    from . import clap_vector

    seen = set(exclude)
    out: list[SoundHit] = []
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        tid = int(src.get("track_id", 0))
        if tid in seen:
            continue
        seen.add(tid)
        vector = src.get("clap_vector")
        out.append(SoundHit(
            track_id=tid,
            artist=src.get("artist", ""),
            title=src.get("title", ""),
            similarity=(sum(a * b for a, b in zip(query_vector, vector))
                        if vector else 0.0),
            source=src.get("source", "") or clap_vector.CLAP_INDEX,
            detected_key=src.get("detected_key", "") or "",
            bpm=src.get("bpm"),
        ))
        if len(out) >= k:
            break
    return out


def sounds_like_text(query: str, k: int = 10,
                     os_client: Any = None) -> list[SoundHit]:
    """Tracks matching a *description* of how they sound.

    The thing no other search here can do. A lyric query needs a track to have
    words, and the spectral vector has no text side at all, so an instrumental
    was unreachable by any query. CLAP puts audio and text in one space, so
    "heavy distorted guitar rock" returns Mastodon and Dinosaur Jr. from a
    library the model has never seen.
    """
    from . import clap_vector
    from .osclient import client

    vector = clap_vector.embed_text(query)
    if vector is None:
        return []
    c = os_client or client()
    try:
        res = c.search(index=clap_vector.CLAP_INDEX, body={
            "size": k, "query": {"knn": {"clap_vector": {"vector": vector,
                                                         "k": k}}}})
    except Exception:
        return []
    return _clap_hits(res, exclude=set(), k=k, query_vector=vector)


def sounds_like_track(track_id: int, k: int = 10,
                      os_client: Any = None) -> list[SoundHit]:
    """Tracks whose CLAP embedding resembles this one's.

    Preferred over :func:`similar_sounding` where an embedding exists. The
    spectral vector was measured putting The Cranberries next to Macy Gray,
    and centring the space widened the scores without reordering them -- the
    features, not the normalisation, were the limit.
    """
    from . import clap_vector
    from .osclient import client

    vector = clap_vector_for(track_id, os_client)
    if vector is None:
        return []
    c = os_client or client()
    try:
        res = c.search(index=clap_vector.CLAP_INDEX, body={
            "size": max(k * 2, k + 3),
            "query": {"knn": {"clap_vector": {"vector": vector,
                                              "k": max(k * 2, k + 3)}}}})
    except Exception:
        return []
    return _clap_hits(res, exclude={track_id}, k=k, query_vector=vector)


def describe_main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    """CLI: find tracks by describing how they sound.

    The query nothing else here can answer. Lyric search needs a track to have
    words; an instrumental has none, so before this it could not be found by
    any query at all.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="karaoke-sounds-like",
        description='Find tracks by description, e.g. "heavy distorted guitar rock"')
    ap.add_argument("query", help="a description of the sound")
    ap.add_argument("-k", type=int, default=8)
    args = ap.parse_args(argv)

    hits = sounds_like_text(args.query, k=args.k)
    if not hits:
        print(f"nothing for {args.query!r}")
        print("Tracks need a CLAP embedding: scripts/clap_index.py")
        return 0
    print(f"sounds like: {args.query!r}\n")
    for hit in hits:
        key = hit.detected_key or ""
        bpm = f"{hit.bpm:5.0f}" if hit.bpm else "     "
        print(f"  {hit.similarity:+.3f}  {hit.artist[:24]:26} "
              f"{hit.title[:28]:30} {key:9} {bpm}")
    print("\nScores are cosine against the text; a description that matches "
          "nothing\nstill returns its nearest tracks, so read low scores as "
          "'no real match'.")
    return 0
