"""Chord detection, and the root motion that separates jazz from pop.

Key detection cannot see what makes jazz jazz. A ii-V-I does not change the
key -- that is what makes it a cadence rather than a modulation -- so a
fifteen-second key window averages straight over the most characteristic
gesture in the idiom. Measured against real records that showed up starkly:
Ella Fitzgerald singing Cole Porter scored identically to the Red Hot Chili
Peppers on every key-level statistic, because at the level of *key* they are
the same.

Chords change every bar or two, so this works at roughly one frame per
quarter second and asks a different question: not "what key is this" but
"where do the roots move". Descending-fifth motion -- the engine of ii-V-I
chains and the circle of fifths -- is what a pop song built on I-IV-V does
not do repeatedly, and what a standard does constantly.

Templates rather than a trained model: a triad has a known pitch-class
profile, the library has no chord labels to train on, and a template match is
inspectable when it gets something wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .logger import log

SAMPLE_RATE = 22050

#: One frame per quarter second. Fast enough to place a chord inside a bar at
#: most tempos, slow enough that a passing tone does not become a chord.
FRAME_SECONDS = 0.25

#: A chord must hold this long to count. Below it the detector is tracking
#: melody, not harmony.
MIN_CHORD_SECONDS = 0.5

#: Semitone offsets from the root for major and minor triads.
_TRIADS = {"maj": (0, 4, 7), "min": (0, 3, 7)}

_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

#: Root motion down a perfect fifth, in semitones, as a positive interval
#: modulo 12. C -> F is +5; that is the same motion as descending a fifth.
DESCENDING_FIFTH = 5


@dataclass
class Chord:
    """One detected chord and where it sits."""

    at_s: float
    root: int                  # pitch class 0-11
    quality: str               # maj | min
    strength: float

    @property
    def name(self) -> str:
        return f"{_NOTE[self.root]}{'m' if self.quality == 'min' else ''}"


@dataclass
class ChordAnalysis:
    """A track's chord sequence and what its root motion looks like."""

    chords: list[Chord]
    duration_s: float
    #: Histogram of root motion intervals between consecutive chords, 0-11,
    #: normalised. Key-invariant: the same progression in any key is identical.
    motion: list[float]

    @property
    def fifth_ratio(self) -> float:
        """Share of chord changes that move down a fifth.

        The single most diagnostic number here. A I-IV-V song produces some by
        accident; a ii-V-I chain produces almost nothing else.
        """
        return self.motion[DESCENDING_FIFTH] if self.motion else 0.0

    @property
    def changes_per_minute(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return len(self.chords) / (self.duration_s / 60.0)

    @property
    def distinct_chords(self) -> int:
        return len({(c.root, c.quality) for c in self.chords})


def _templates():
    """24 unit-norm triad templates, indexed (root, quality)."""
    import numpy as np

    out = {}
    for quality, intervals in _TRIADS.items():
        for root in range(12):
            vec = np.zeros(12, dtype=float)
            for i in intervals:
                vec[(root + i) % 12] = 1.0
            out[(root, quality)] = vec / np.linalg.norm(vec)
    return out


def detect(audio_path: str, *, frame_seconds: float = FRAME_SECONDS,
           sample_rate: int = SAMPLE_RATE,
           harmonic: bool = False) -> Optional[ChordAnalysis]:
    """Detect the chord sequence of a track.

    ``harmonic`` runs percussive/harmonic separation first, which cleans the
    chroma but costs 22x the total runtime -- measured at 37.0s against 1.7s
    for the CQT itself, so it is 95% of the work. Off by default: over a
    library that is the difference between an hour and a day, and the chord
    labels it changes are mostly ones already too short to be kept.

    Returns None when the audio cannot be read or librosa is unavailable.
    """
    try:
        import librosa
        import numpy as np

        from .analyze import _as_wav
    except Exception:
        log.debug("librosa unavailable; no chord detection")
        return None

    try:
        with _as_wav(audio_path, sample_rate) as wav:
            if not wav:
                return None
            y, sr = librosa.load(wav, sr=sample_rate, mono=True)
    except Exception:
        log.debug("could not decode %s for chords", audio_path, exc_info=True)
        return None

    if y is None or len(y) < sr * 5:
        return None

    try:
        hop = max(512, int(sr * frame_seconds))
        # CQT-based chroma: pitch-aligned bins, which template matching needs.
        # chroma_stft is 13x faster again but its bins are not pitch-aligned,
        # so a triad template matches poorly.
        source = librosa.effects.harmonic(y, margin=3.0) if harmonic else y
        chroma = librosa.feature.chroma_cqt(y=source, sr=sr, hop_length=hop)
    except Exception:
        log.debug("chroma failed for %s", audio_path, exc_info=True)
        return None

    templates = _templates()
    keys = list(templates)
    matrix = np.stack([templates[k] for k in keys])          # 24 x 12

    # Normalise each frame so loudness does not decide the chord.
    norms = np.linalg.norm(chroma, axis=0, keepdims=True)
    norms[norms == 0] = 1.0
    frames = (chroma / norms).T                               # frames x 12
    scores = frames @ matrix.T                                # frames x 24
    best = scores.argmax(axis=1)
    strength = scores.max(axis=1)

    frame_dur = hop / sr
    min_frames = max(1, int(MIN_CHORD_SECONDS / frame_dur))

    # Collapse runs, dropping any too short to be harmony.
    chords: list[Chord] = []
    run_start = 0
    for i in range(1, len(best) + 1):
        if i < len(best) and best[i] == best[run_start]:
            continue
        length = i - run_start
        if length >= min_frames:
            root, quality = keys[int(best[run_start])]
            chords.append(Chord(
                at_s=run_start * frame_dur,
                root=root,
                quality=quality,
                strength=float(strength[run_start:i].mean()),
            ))
        run_start = i

    duration = len(y) / sr
    motion = [0.0] * 12
    for prev, nxt in zip(chords, chords[1:]):
        step = (nxt.root - prev.root) % 12
        if step:                       # a repeat is not motion
            motion[step] += 1.0
    total = sum(motion) or 1.0
    motion = [m / total for m in motion]

    return ChordAnalysis(chords=chords, duration_s=duration, motion=motion)


def describe(analysis: ChordAnalysis, limit: int = 8) -> str:
    """A readable summary, for a TUI or a log."""
    seq = " ".join(c.name for c in analysis.chords[:limit])
    return (f"{analysis.distinct_chords} chords, "
            f"{analysis.changes_per_minute:.0f}/min, "
            f"fifths {analysis.fifth_ratio:.0%}  |  {seq}")
