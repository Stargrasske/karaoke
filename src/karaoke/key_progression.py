"""How a track's key moves over time, and what that says about its form.

A single detected key describes a song the way an average describes a melody.
Most pop sits in one key for three minutes; a blues sits in one key and moves
its *chords*; modal jazz moves the key centre itself. Those are different
pieces of music that currently all reduce to one label like "A major".

What this cannot see is chord-level harmony. A ii-V-I does not change the key,
so a standard sounds tonally identical to a pop song here -- measured, Ella
Fitzgerald scored 76% stability against 77% for the Red Hot Chili Peppers.
:mod:`karaoke.chords` answers that question instead.

This samples the key on a sliding window, smooths the result into sustained
regions, and classifies the moves between them. The classification matters
more than the count: a detector flipping between A major and A minor is
reporting modal mixture or its own ambiguity, not a modulation, and the
relative pair (A minor / C major) shares a key signature entirely. Counting
those as key changes would make every track look like jazz.

The output includes a key-invariant vector -- transitions recorded as
intervals rather than destinations -- so two songs that move the same way in
different keys compare as similar. A truck-driver key change up a semitone in
the last chorus looks the same whether the song started in C or F#.

Cost is negligible: key detection runs at roughly 0.002x realtime, so the
decode dominates. Computed in the same pass as a CLAP embedding it is
effectively free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import musictheory
from .logger import log

SAMPLE_RATE = 44100

#: Window and hop for sampling the key. Fifteen seconds is long enough for the
#: profile vote to settle and short enough to place a modulation within a
#: phrase; the half-window hop means a change is seen by two windows before it
#: is believed.
WINDOW_SECONDS = 15.0
HOP_SECONDS = 7.5

#: Consecutive windows that must agree before a new key is accepted. One
#: window disagreeing is the detector wobbling on a transitional bar; two in a
#: row is the music.
CONFIRM_WINDOWS = 2

#: Below this the window's own vote is too weak to reason about.
MIN_CONFIDENCE = 0.55

#: Intervals, in semitones, between key centres a fifth apart. Used to
#: classify a transition's kind; it no longer drives a form label, because a
#: ii-V-I does not change the key and so fifth motion is invisible at this
#: level by construction. See :mod:`karaoke.chords`.
FIFTHS = {5, 7}

#: Degrees of the home key whose chords a window can mistake for a key centre:
#: the tonic itself (a move *back* home is a return, not a modulation) plus
#: IV, V, vi and ii -- the chords a I-IV-V song or a blues actually sits on.
DIATONIC_DEGREES = {0, 2, 5, 7, 9}


@dataclass
class KeyRegion:
    """A stretch of the track sitting in one key."""

    key: Any                  # musictheory.Key
    start_s: float
    end_s: float
    confidence: float

    @property
    def seconds(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass
class Transition:
    """One key change, described by how it moves rather than where it lands."""

    at_s: float
    from_key: Any
    to_key: Any
    #: Semitones from the old tonic to the new one, 0-11.
    interval: int
    #: parallel | relative | fifth | step | distant
    kind: str


@dataclass
class Progression:
    """A track's harmonic shape."""

    regions: list[KeyRegion] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    duration_s: float = 0.0
    home_key: Any = None
    #: Fraction of the track spent in the most-occupied key, 0..1.
    stability: float = 1.0
    form: str = "unknown"

    @property
    def changes_per_minute(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return len(self.transitions) / (self.duration_s / 60.0)


def _tonic_index(key: Any) -> Optional[int]:
    """Pitch class of a key's tonic, 0-11, or None."""
    tonic = getattr(key, "tonic", None)
    return tonic if isinstance(tonic, int) else None


def _classify(from_key: Any, to_key: Any, home: Any = None) -> tuple[int, str]:
    """Describe a move between two keys as (interval, kind).

    Kind carries far more information than the interval. A parallel or
    relative move is one tonal centre seen two ways. More importantly, a move
    to the home key's IV or V is usually not a modulation at all: a blues or a
    I-IV-V pop song spends whole bars on those chords, and a fifteen-second
    window centred on them reads as that key. Measured on rock tracks this
    made six of ten look like circle-of-fifths jazz. Those are marked
    ``diatonic`` and excluded from the modulation count.
    """
    a, b = _tonic_index(from_key), _tonic_index(to_key)
    interval = ((b - a) % 12) if (a is not None and b is not None) else 0

    try:
        if musictheory.keys_equivalent(musictheory.parallel_key(from_key), to_key):
            return interval, "parallel"
        if musictheory.keys_equivalent(musictheory.relative_key(from_key), to_key):
            return interval, "relative"
    except Exception:
        pass

    # Within the home key's own harmony? Then the window found a chord, not a
    # new key centre.
    h = _tonic_index(home) if home is not None else None
    if h is not None and b is not None:
        from_home = (b - h) % 12
        if from_home in DIATONIC_DEGREES:
            return interval, "diatonic"

    if interval in FIFTHS:
        return interval, "fifth"
    if interval in (1, 2, 10, 11):
        return interval, "step"
    return interval, "distant"


def _oscillating(transitions: list["Transition"]) -> bool:
    """True when the track keeps returning rather than travelling.

    A vamp alternates between two centres; a modulating piece leaves one
    behind. Returning to a key already visited, repeatedly, is the tell.
    """
    tonics = [_tonic_index(t.to_key) for t in transitions]
    tonics = [t for t in tonics if t is not None]
    if len(tonics) < 3:
        return False
    return len(set(tonics)) <= 2


def timeline(audio_path: str, *, sample_rate: int = SAMPLE_RATE) -> list[tuple[float, Any, float]]:
    """Sample the key across the track: [(start_seconds, Key, confidence)]."""
    try:
        import essentia
        import essentia.standard as es

        from .analyze import _as_wav, _essentia_key
    except Exception:
        log.debug("essentia unavailable; no key timeline")
        return []

    essentia.log.infoActive = False
    essentia.log.warningActive = False

    try:
        with _as_wav(audio_path, sample_rate) as wav:
            if not wav:
                return []
            audio = es.MonoLoader(filename=wav, sampleRate=sample_rate)()
    except Exception:
        log.debug("could not decode %s for key timeline", audio_path, exc_info=True)
        return []

    win = int(sample_rate * WINDOW_SECONDS)
    hop = int(sample_rate * HOP_SECONDS)
    if len(audio) < win:
        return []

    out: list[tuple[float, Any, float]] = []
    for start in range(0, len(audio) - win + 1, hop):
        res = _essentia_key(audio[start:start + win], es, sample_rate)
        if res is not None:
            out.append((start / sample_rate, res[0], res[1]))
    return out


def regions_from(samples: list[tuple[float, Any, float]]) -> list[KeyRegion]:
    """Collapse per-window keys into sustained regions.

    A key is only accepted once CONFIRM_WINDOWS consecutive windows agree, so
    a single wobbling window extends the region it interrupts rather than
    splitting it in three.
    """
    if not samples:
        return []

    regions: list[KeyRegion] = []
    current_key = samples[0][1]
    start = samples[0][0]
    confs = [samples[0][2]]
    pending: list[tuple[float, Any, float]] = []

    for t, key, conf in samples[1:]:
        if conf < MIN_CONFIDENCE:
            continue
        if musictheory.keys_equivalent(key, current_key):
            pending.clear()
            confs.append(conf)
            continue

        pending.append((t, key, conf))
        if len(pending) < CONFIRM_WINDOWS:
            continue
        if not all(musictheory.keys_equivalent(p[1], pending[0][1]) for p in pending):
            pending = pending[-1:]
            continue

        change_at = pending[0][0]
        regions.append(KeyRegion(current_key, start, change_at,
                                 sum(confs) / len(confs) if confs else 0.0))
        current_key = pending[0][1]
        start = change_at
        confs = [p[2] for p in pending]
        pending = []

    end = samples[-1][0] + WINDOW_SECONDS
    regions.append(KeyRegion(current_key, start, end,
                             sum(confs) / len(confs) if confs else 0.0))
    return regions


#: Transition kinds that are not modulations: the same centre seen another
#: way, or a chord of the home key that a window mistook for one.
NON_MODULATION = ("parallel", "relative", "diatonic")


def _form_of(prog: "Progression") -> str:
    """A coarse label for the harmonic shape."""
    real = [t for t in prog.transitions if t.kind not in NON_MODULATION]

    if not real:
        if any(t.kind == "diatonic" for t in prog.transitions):
            # One key, but the window kept finding its IV and V: a I-IV-V
            # song or a blues, which is exactly the predictable shape.
            return "diatonic-vamp"
        if any(t.kind == "parallel" for t in prog.transitions):
            return "static-with-mixture"   # one key, borrowing its parallel mode
        return "static"

    if len(real) == 1 and real[0].kind == "step" and real[0].at_s > 0.6 * prog.duration_s:
        return "truck-driver"              # the late lift, up a semitone or tone

    # Travelling means leaving keys behind. A track that keeps returning to the
    # same two centres is vamping, however many fifths it appears to contain.
    if _oscillating(real):
        return "oscillating"

    # There used to be a "fifth-related" label here, for key centres a fifth
    # apart. Measured across 5292 tracks it fired 48 times, and the artists
    # with the most actual fifth motion at chord level -- Ween at 28.5%, John
    # Cale and Lou Reed at 30.0% -- came out among the harmonically *simplest*
    # by vocabulary. The two measures contradicted each other, so the label
    # was describing noise. Fifth motion is a chord-level property and lives
    # in karaoke.chords, where it separates Bill Evans from The Strokes.
    if prog.changes_per_minute >= 1.5:
        return "restless"
    return "modulating"


def analyse(audio_path: str) -> Optional[Progression]:
    """Full harmonic-shape analysis of one file."""
    samples = timeline(audio_path)
    if not samples:
        return None

    regions = regions_from(samples)
    duration = samples[-1][0] + WINDOW_SECONDS

    # Home first: whether a move is a modulation or just the song's own IV
    # chord can only be judged against the key it keeps coming back to.
    held: dict[str, float] = {}
    for r in regions:
        held[getattr(r.key, "name", "?")] = held.get(getattr(r.key, "name", "?"), 0.0) + r.seconds
    home_name = max(held, key=held.get) if held else None
    home = next((r.key for r in regions if getattr(r.key, "name", None) == home_name), None)

    transitions: list[Transition] = []
    for prev, nxt in zip(regions, regions[1:]):
        interval, kind = _classify(prev.key, nxt.key, home)
        transitions.append(Transition(nxt.start_s, prev.key, nxt.key, interval, kind))

    prog = Progression(
        regions=regions,
        transitions=transitions,
        duration_s=duration,
        home_key=home,
        stability=(held.get(home_name, 0.0) / duration) if (home_name and duration) else 1.0,
    )
    prog.form = _form_of(prog)
    return prog


def progression_vector(prog: Progression) -> list[float]:
    """A key-invariant description of how the track moves.

    Twelve slots for transition intervals -- recorded as intervals, not
    destinations, so the same modulation in any key lands in the same slot --
    followed by five shape scalars. Comparable across tracks by cosine.
    """
    hist = [0.0] * 12
    for t in prog.transitions:
        if t.kind not in NON_MODULATION:
            hist[t.interval % 12] += 1.0
    total = sum(hist) or 1.0
    hist = [h / total for h in hist]

    kinds = [t.kind for t in prog.transitions]
    scalars = [
        min(1.0, prog.changes_per_minute / 4.0),
        prog.stability,
        min(1.0, len({getattr(r.key, "name", "?") for r in prog.regions}) / 6.0),
        (kinds.count("fifth") / len(kinds)) if kinds else 0.0,
        (kinds.count("parallel") / len(kinds)) if kinds else 0.0,
    ]
    return hist + scalars


def describe(prog: Progression) -> str:
    """One readable line, for a TUI or a log."""
    home = getattr(prog.home_key, "name", "?")
    real = [t for t in prog.transitions if t.kind not in NON_MODULATION]
    bits = [f"{prog.form}", f"home {home}", f"{prog.stability:.0%} stable"]
    if real:
        moves = ", ".join(
            f"{getattr(t.from_key,'name','?')}->{getattr(t.to_key,'name','?')} ({t.kind})"
            for t in real[:3]
        )
        bits.append(moves)
    return " · ".join(bits)


PROGRESSION_INDEX = "karaoke-progression"

#: Twelve interval slots plus five shape scalars. Changing this invalidates
#: every stored vector, exactly like the CLAP dimension.
PROGRESSION_DIM = 17

#: Chord root-motion histogram: twelve intervals, key-invariant.
CHORD_MOTION_DIM = 12


def doc_id(track_id: int) -> str:
    """One progression per track: re-analysing replaces rather than appends."""
    return f"prog:{track_id}"


def ensure_index(os_client: Any, index_name: str = PROGRESSION_INDEX) -> bool:
    """Create the progression index if absent. True if it was created."""
    if os_client.indices.exists(index=index_name):
        return False
    os_client.indices.create(index=index_name, body={
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "track_id": {"type": "integer"},
                "artist": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                # Declared rather than left to dynamic mapping so aggregating
                # by form works without a .keyword suffix -- the same trap the
                # CLAP index documents for `genre`.
                "form": {"type": "keyword"},
                "home_key": {"type": "keyword"},
                "stability": {"type": "float"},
                "changes_per_minute": {"type": "float"},
                "n_transitions": {"type": "integer"},
                "duration_s": {"type": "float"},
                "analysed_at": {"type": "date"},
                "progression_vector": {
                    "type": "knn_vector",
                    "dimension": PROGRESSION_DIM,
                    "method": {"name": "hnsw", "space_type": "cosinesimil",
                               "engine": "lucene"},
                },
                # Chord-level harmony, which the key-level fields cannot see:
                # a ii-V-I does not change the key, so a jazz standard and a
                # pop song are tonally identical above but differ sharply here
                # (19.8 distinct chords against 13.2, measured).
                "distinct_chords": {"type": "integer"},
                "fifth_ratio": {"type": "float"},
                "chord_changes_per_minute": {"type": "float"},
                "chord_motion": {
                    "type": "knn_vector",
                    "dimension": CHORD_MOTION_DIM,
                    "method": {"name": "hnsw", "space_type": "cosinesimil",
                               "engine": "lucene"},
                },
            }
        },
    })
    return True


def ensure_chord_fields(os_client: Any, index_name: str = PROGRESSION_INDEX) -> bool:
    """Add the chord fields to an index created before they existed.

    ensure_index only creates a missing index, so an index already holding
    progressions would otherwise never gain a knn mapping for chord_motion --
    and a knn_vector cannot be added by dynamic mapping. Adding a field to an
    existing mapping is allowed and idempotent.
    """
    try:
        current = os_client.indices.get_mapping(index=index_name)
        props = list(current.values())[0]["mappings"].get("properties", {})
        if "chord_motion" in props:
            return False
        os_client.indices.put_mapping(index=index_name, body={
            "properties": {
                "distinct_chords": {"type": "integer"},
                "fifth_ratio": {"type": "float"},
                "chord_changes_per_minute": {"type": "float"},
                "chord_motion": {
                    "type": "knn_vector",
                    "dimension": CHORD_MOTION_DIM,
                    "method": {"name": "hnsw", "space_type": "cosinesimil",
                               "engine": "lucene"},
                },
            }
        })
        return True
    except Exception:
        log.debug("could not add chord fields to %s", index_name, exc_info=True)
        return False


def build_doc(*, track_id: int, prog: Progression, artist: str = "",
              title: str = "", analysed_at: str, chords: Any = None) -> dict:
    """Assemble the document for one analysed track."""
    real = [t for t in prog.transitions if t.kind not in NON_MODULATION]
    doc_chords = {}
    if chords is not None:
        doc_chords = {
            "distinct_chords": chords.distinct_chords,
            "fifth_ratio": round(chords.fifth_ratio, 4),
            "chord_changes_per_minute": round(chords.changes_per_minute, 2),
            "chord_motion": chords.motion,
        }
    return {
        **doc_chords,
        "track_id": track_id,
        "artist": artist,
        "title": title,
        "form": prog.form,
        "home_key": getattr(prog.home_key, "name", "") or "",
        "stability": round(prog.stability, 4),
        "changes_per_minute": round(prog.changes_per_minute, 4),
        "n_transitions": len(real),
        "duration_s": round(prog.duration_s, 1),
        "analysed_at": analysed_at,
        "progression_vector": progression_vector(prog),
    }


def store(track_id: int, prog: Progression, *, artist: str = "", title: str = "",
          chords: Any = None, os_client: Any = None) -> bool:
    """Index a progression so it becomes searchable. Returns success.

    Best-effort for the same reason as the CLAP store: callers are part-way
    through a pipeline whose other results must survive OpenSearch being
    unavailable.
    """
    from datetime import datetime, timezone

    if prog is None:
        return False
    try:
        client = os_client
        if client is None:
            from .osclient import client as get_os_client

            client = get_os_client()
        if client is None:
            return False
        ensure_index(client)
        if chords is not None:
            ensure_chord_fields(client)
        client.index(
            index=PROGRESSION_INDEX,
            id=doc_id(track_id),
            body=build_doc(track_id=track_id, prog=prog, artist=artist, title=title,
                           analysed_at=datetime.now(timezone.utc).isoformat(),
                           chords=chords),
        )
        return True
    except Exception:
        log.debug("could not index progression for track %s", track_id, exc_info=True)
        return False
