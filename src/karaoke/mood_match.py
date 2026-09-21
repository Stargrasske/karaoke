"""Turn "how I feel" into a setlist.

Two different questions arrive through the same field. *"upbeat happy
electronic"* describes how music should **sound**; *"I feel wrecked"*
describes how someone **is**. Neither alone retrieves well: CLAP text search
nails the first and takes the second far too literally, while the mood plane
handles the second and knows nothing about genre.

So both run, and their scores are blended. CLAP supplies the candidates --
it is the only search here that reaches instrumentals, since it embeds audio
and text in one space -- and proximity on the energy/brightness plane
reorders them toward the requested feeling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import localcache, musictheory, sentiment
from .logger import log

# Where each mood sits on the plane, as (valence, arousal) in 0..1.
# Valence reads off `brightness`, arousal off `energy` -- the same two axes
# tui.feeling_glyph uses, so a mood target and the glyph shown next to a track
# cannot disagree.
MOOD_TARGETS: dict[str, tuple[float, float]] = {
    "happy": (0.80, 0.70),
    "sad": (0.20, 0.25),
    "angry": (0.25, 0.85),
    "tender": (0.65, 0.25),
}

# Words people actually type at a DJ that the lyric lexicon does not cover.
# Values are (valence, arousal) nudges in the same space.
_DESCRIPTIVE: dict[str, tuple[float, float]] = {
    "upbeat": (0.80, 0.75),
    "energetic": (0.70, 0.90),
    "hyped": (0.75, 0.95),
    "party": (0.85, 0.85),
    "dancey": (0.80, 0.80),
    "chill": (0.60, 0.20),
    "mellow": (0.55, 0.20),
    "calm": (0.60, 0.15),
    "sleepy": (0.45, 0.10),
    "dark": (0.15, 0.55),
    "moody": (0.25, 0.40),
    "melancholy": (0.20, 0.25),
    "bleak": (0.10, 0.25),
    "cynical": (0.25, 0.50),
    "bitter": (0.20, 0.55),
    "aggressive": (0.20, 0.90),
    "heavy": (0.25, 0.85),
    "romantic": (0.70, 0.30),
    "dreamy": (0.65, 0.25),
    "wrecked": (0.15, 0.20),
    "exhausted": (0.35, 0.10),
    "anxious": (0.25, 0.65),
    "triumphant": (0.85, 0.80),
    "nostalgic": (0.45, 0.30),
}

# Neutral centre: used when a phrase carries no mood signal at all, which is
# the "purely descriptive" case (e.g. "electronic"). A centred target makes the
# plane term flat, so CLAP decides the ordering on its own.
_NEUTRAL = (0.5, 0.5)


@dataclass
class MoodTarget:
    """Where on the plane a request points, and how strongly."""

    valence: float
    arousal: float
    #: 0.0 when the phrase carried no mood words, rising with how many it did.
    #: Weights the plane term, so descriptive-only queries stay CLAP-led.
    confidence: float
    #: Mood words that drove the target, for showing back to the user.
    matched: list[str]
    lifted: bool = False

    @property
    def glyph(self) -> str:
        from .tui import feeling_glyph

        return feeling_glyph(self.arousal, self.valence)


def resolve_target(phrase: str, *, lift: bool = False) -> MoodTarget:
    """Read a free-text phrase as a point on the energy/brightness plane.

    Combines the lyric mood lexicon (happy/sad/angry/tender) with the
    descriptive words above, averaging every hit. ``lift`` reflects the result
    through the centre, turning "I feel awful" into a set that pulls upward
    instead of matching.
    """
    words = [w.strip(".,!?;:'\"").lower() for w in (phrase or "").split()]
    points: list[tuple[float, float]] = []
    matched: list[str] = []

    for w in words:
        if w in _DESCRIPTIVE:
            points.append(_DESCRIPTIVE[w])
            matched.append(w)

    # The lyric lexicon is much broader than the list above ("heartbroken",
    # "lonely", "rage"), so run the same scorer the lyric analysis uses.
    scores = sentiment.score_line(phrase or "")
    for mood, hits in scores.items():
        if hits > 0 and mood in MOOD_TARGETS:
            points.extend([MOOD_TARGETS[mood]] * hits)
            matched.append(mood)

    if not points:
        return MoodTarget(*_NEUTRAL, confidence=0.0, matched=[], lifted=lift)

    valence = sum(p[0] for p in points) / len(points)
    arousal = sum(p[1] for p in points) / len(points)

    if lift:
        # Reflect through the centre, then pull arousal up rather than simply
        # inverting it: the point of lifting a flat mood is more energy, not
        # less, and a straight reflection of "angry" would land on "sleepy".
        valence = 1.0 - valence
        arousal = min(1.0, max(0.55, 1.0 - arousal))

    # One deliberate mood word ("wrecked") is already a strong signal, so
    # confidence starts high rather than ramping linearly. Tuned against the
    # real library: at 0.5, "I feel wrecked" still ranked a high-energy track
    # first because the sonic term outvoted the plane.
    confidence = 0.75 if len(points) == 1 else 1.0
    return MoodTarget(valence, arousal, confidence, sorted(set(matched)), lift)


#: Library-wide brightness statistics, fetched once. Raw brightness is a
#: spectral centroid measure, not a 0..1 valence: across 17k tracks it spans
#: 0.0-0.55 with mean 0.19 and sd 0.05, so comparing it directly against a
#: target of 0.8 adds a near-constant to every candidate and the valence axis
#: does nothing. Rescaling against the observed spread makes it a real axis.
_BRIGHTNESS_STATS: Optional[tuple[float, float]] = None


def _brightness_stats() -> tuple[float, float]:
    global _BRIGHTNESS_STATS
    if _BRIGHTNESS_STATS is None:
        try:
            with localcache.connect() as conn:
                row = conn.execute(
                    "SELECT avg(brightness) av, stddev(brightness) sd"
                    " FROM track_analysis WHERE brightness IS NOT NULL"
                ).fetchone()
            _BRIGHTNESS_STATS = (float(row["av"] or 0.19), float(row["sd"] or 0.05) or 0.05)
        except Exception:
            _BRIGHTNESS_STATS = (0.19, 0.05)
    return _BRIGHTNESS_STATS


def _valence(brightness: Optional[float]) -> float:
    """Rescale raw brightness onto a usable 0..1 valence axis."""
    if brightness is None:
        return 0.5
    mean, sd = _brightness_stats()
    # +/- 2 sd covers the bulk of the library and maps to the full range.
    return min(1.0, max(0.0, 0.5 + (brightness - mean) / (4.0 * sd)))


def _plane_score(energy: Optional[float], brightness: Optional[float],
                 target: MoodTarget) -> float:
    """1.0 when a track sits exactly on the target, falling to 0.0 opposite."""
    if energy is None:
        return 0.5  # unanalysed: neither rewarded nor punished
    b = _valence(brightness)
    # Arousal counts for more than valence. Energy is measured directly and
    # spreads across the whole range (sd 0.26); valence is inferred from a
    # spectral centroid that barely moves (sd 0.05) and only becomes an axis
    # at all after rescaling, so it is the softer signal. Weighting them
    # equally let it drag a "wrecked" request back up to mid-energy.
    dist = (0.7 * (energy - target.arousal) ** 2
            + 0.3 * (b - target.valence) ** 2) ** 0.5
    return max(0.0, 1.0 - dist)


def _lyric_score(track_id: int, target: MoodTarget, conn: Any) -> Optional[float]:
    """How well a track's lyrics match the requested mood, or None if unknown.

    Computed on demand: there is no stored per-track sentiment, so this only
    ever runs over the shortlist CLAP already produced.
    """
    try:
        lyrics = localcache.get_lyrics_by_track_id(track_id, conn)
    except Exception:
        return None
    if not lyrics:
        return None
    text = lyrics.plain or ""
    if not text and lyrics.lines:
        text = "\n".join(t for _, t in lyrics.lines)
    if not text.strip():
        return None

    from . import visuals

    profile = visuals.analyze_sentiment(text)
    if profile.total_hits <= 0:
        return None

    # Score the lyric's dominant mood by where it sits on the plane relative
    # to the target, so "sad lyrics" score well for a sad request without
    # needing the request to name the mood exactly.
    point = MOOD_TARGETS.get(profile.dominant)
    if point is None:
        return None
    dist = ((point[0] - target.valence) ** 2 + (point[1] - target.arousal) ** 2) ** 0.5
    return max(0.0, 1.0 - dist / (2 ** 0.5))


def match(phrase: str, *, limit: int = 8, lift: bool = False,
          exclude: Optional[set[int]] = None,
          os_client: Any = None) -> tuple[MoodTarget, list[dict[str, Any]]]:
    """Rank library tracks against a free-text mood or description.

    Returns the resolved target alongside the ranked tracks, so a caller can
    show what it understood -- a mood match that cannot explain itself reads
    as random.
    """
    from . import search as search_mod

    target = resolve_target(phrase, lift=lift)
    excluded = exclude or set()

    # Over-fetch hard. The plane can only reorder what CLAP hands it, so a
    # tight pool pins the result to whatever the sonic search liked -- asking
    # for a low-energy set from forty mostly-loud candidates just returns the
    # quietest loud tracks. A wider pool gives the mood term something to find.
    pool_size = max(limit * 40, 300)
    try:
        hits = search_mod.sounds_like_text(phrase, k=pool_size, os_client=os_client)
    except Exception as exc:
        log.warning("CLAP text search unavailable for %r: %s", phrase, exc)
        hits = []

    clap_by_id = {h.track_id: h for h in hits if h.track_id not in excluded}

    # Falling back to the plane alone keeps /mood working when OpenSearch or
    # the CLAP index is down -- degraded, but not broken.
    if not clap_by_id:
        return target, _plane_only(target, limit, excluded)

    scores = [h.similarity for h in clap_by_id.values()]
    lo, hi = min(scores), max(scores)
    span = (hi - lo) or 1.0

    # A descriptive phrase carries no mood signal, so confidence is 0 and this
    # collapses to pure CLAP ordering. A felt phrase pulls the plane in.
    plane_weight = 0.70 * target.confidence
    clap_weight = 1.0 - plane_weight

    ranked: list[dict[str, Any]] = []
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title, t.play_count,
                   a.detected_key, a.bpm, a.energy, a.brightness, g.genre,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            WHERE t.track_id = ANY(%s)
            """,
            (list(clap_by_id),),
        )
        for row in cur.fetchall():
            hit = clap_by_id[row["track_id"]]
            clap_norm = (hit.similarity - lo) / span
            plane = _plane_score(row["energy"], row["brightness"], target)

            score = clap_weight * clap_norm + plane_weight * plane

            # Lyrics only adjust an already-ranked track, and only when the
            # request was emotional. Scanning them for a genre query would be
            # both slow and beside the point.
            lyric = None
            if target.confidence > 0:
                lyric = _lyric_score(row["track_id"], target, conn)
                if lyric is not None:
                    score = 0.85 * score + 0.15 * lyric

            key_obj = musictheory.parse_key(row["detected_key"]) if row["detected_key"] else None
            ranked.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "key": row["detected_key"] or "",
                "camelot": key_obj.camelot if key_obj else "?",
                "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                "energy": row["energy"],
                "brightness": row["brightness"],
                "genre": row["genre"] or "",
                "has_synced_lyrics": bool(row["has_synced"]),
                "score": round(score, 4),
                "sound_match": round(hit.similarity, 4),
                "mood_match": round(plane, 3),
                "lyric_match": round(lyric, 3) if lyric is not None else None,
            })

    ranked.sort(key=lambda r: r["score"], reverse=True)
    return target, ranked[:limit]


def _plane_only(target: MoodTarget, limit: int,
                excluded: set[int]) -> list[dict[str, Any]]:
    """Rank on energy/brightness alone, for when CLAP is unavailable."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title,
                   a.detected_key, a.bpm, a.energy, a.brightness, g.genre,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            WHERE a.energy IS NOT NULL
            ORDER BY ABS(a.energy - %s) ASC, t.play_count DESC
            LIMIT %s
            """,
            (target.arousal, limit + len(excluded)),
        )
        out = []
        for row in cur.fetchall():
            if row["track_id"] in excluded:
                continue
            key_obj = musictheory.parse_key(row["detected_key"]) if row["detected_key"] else None
            out.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "key": row["detected_key"] or "",
                "camelot": key_obj.camelot if key_obj else "?",
                "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                "energy": row["energy"],
                "brightness": row["brightness"],
                "genre": row["genre"] or "",
                "has_synced_lyrics": bool(row["has_synced"]),
                "score": round(_plane_score(row["energy"], row["brightness"], target), 4),
                "sound_match": None,
                "mood_match": round(_plane_score(row["energy"], row["brightness"], target), 3),
                "lyric_match": None,
            })
        return out[:limit]
