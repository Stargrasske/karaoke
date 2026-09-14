"""Lay real lyrics onto Whisper's rhythm.

Whisper on sung audio is a poor transcriber and a good metronome. It mishears
words ("up to do" -> "up to doom", "daddy done" -> "daddy John") and emits
"🎵" / "// Music //" artifacts, but the *times* it attaches to what it heard are
close to right.

So Whisper supplies timing and a known-good source (LRCLIB, Genius) supplies
words. The two cannot simply be zipped: Whisper's line breaks fall in different
places from the real lyric's, and it drops or invents whole phrases.

    whisper: [00:21.24] mister, when you're gone 🎵 🎵They bring you
    real:    Where, mister, when you're young / They bring you up to do

Instead both are reduced to normalized word streams and aligned with
``difflib.SequenceMatcher``, which anchors on the words Whisper *did* get right
("valley", "high school", "seventeen") and tolerates the rest as edits. Each
real line takes the timestamp of its earliest anchored word; unanchored lines
are interpolated between their neighbours so no line is left without a time.

stdlib only — no alignment dependency is pulled in for this.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

# Whisper marks instrumental passages with these; they anchor nothing.
_ARTIFACT = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]|//\s*music\s*//",
                       re.IGNORECASE)

_WORD = re.compile(r"[a-z0-9']+")

# Plausible span for one sung word. Outside this range the timing is not a word
# being sung: below the floor it is a decoding glitch, and above the ceiling
# Whisper has stretched one token across an instrumental passage. Anchoring a
# lyric line on either puts it seconds away from the singing.
MIN_WORD_S = 0.04
MAX_WORD_S = 4.0

# Fastest credible singing, in seconds per unit of line_weight. Measured
# against real anchors on a rock track: comfortable delivery runs 0.4-0.9, so
# anything under this is not a person singing those words -- it is an anchor
# landing on the wrong repeat of a chorus, which is the usual way a
# well-anchored alignment still comes out wrong.
MIN_SECONDS_PER_WEIGHT = 0.12

# Beats in a bar. Assumed rather than detected: 4/4 covers nearly everything
# here, and getting it wrong only loosens the bounds below rather than
# inverting them.
BEATS_PER_BAR = 4

# Confidence below which a word is only trusted if the real lyrics corroborate
# it. Measured, not guessed: on a track whose vocal onset is known, "neighal"
# (a pure hallucination before the singing starts) scored 0.001 while "no,",
# "don\u2019t" and "ya" -- all genuinely sung -- scored 0.000, 0.229 and 0.048.
# Sung words are inherently low-confidence, so a plain floor discards real ones;
# what separates them is whether the word appears in the lyrics at all.
LOW_CONFIDENCE = 0.15

# Only the ceiling scales with tempo. A word cannot credibly span two bars --
# that is Whisper holding one token across an instrumental -- and two bars is a
# very different length at 76bpm than at 152.
#
# The floor stays absolute. Deriving it from tempo looked principled and was
# wrong: a sixteenth note at 76bpm is 0.197s, but singers pack syllables far
# tighter than that ("ya" measured 0.12s on a 76bpm track), so a tempo-scaled
# floor threw away real words and dragged the first line eight seconds late.
# Near-zero spans are decoding glitches at any tempo.
MAX_WORD_BARS = 2.0

# A stretch this long with barely any words in it is an instrumental break, not
# slow singing. Eight bars rather than four: four is a normal gap between
# verses on a mid-tempo track, and treating it as a break pushed a whole chorus
# minutes late.
BREAK_BARS = 8.0
BREAK_MAX_WORDS = 3


def beat_seconds(bpm: Optional[float]) -> Optional[float]:
    """Seconds per beat, or None when the tempo is unknown."""
    try:
        value = float(bpm or 0.0)
    except (TypeError, ValueError):
        return None
    return 60.0 / value if 20.0 < value < 400.0 else None


def bar_seconds(bpm: Optional[float]) -> Optional[float]:
    beat = beat_seconds(bpm)
    return beat * BEATS_PER_BAR if beat else None


def word_bounds(bpm: Optional[float]) -> tuple[float, float]:
    """Plausible (min, max) seconds for one sung word at this tempo.

    Falls back to the fixed pair when the tempo is unknown. A word at 152bpm
    can be much shorter than one at 70bpm, and judging both by the same
    absolute threshold throws away good anchors on fast material and keeps bad
    ones on slow.
    """
    beat = beat_seconds(bpm)
    if beat is None:
        return (MIN_WORD_S, MAX_WORD_S)
    return (MIN_WORD_S, beat * BEATS_PER_BAR * MAX_WORD_BARS)

# Numbers appear as digits in one source and words in the other often enough
# ("17" vs "seventeen") to be worth anchoring on.
_NUMBERS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "11": "eleven", "12": "twelve", "13": "thirteen", "14": "fourteen",
    "15": "fifteen", "16": "sixteen", "17": "seventeen", "18": "eighteen",
    "19": "nineteen", "20": "twenty",
}


def normalize_word(word: str) -> str:
    """Reduce a word to a comparable token, or "" if it carries no signal."""
    w = _ARTIFACT.sub(" ", word or "").casefold()
    # Contractions differ between sources ("we'd" vs "wed"); drop apostrophes.
    w = w.replace("’", "'").replace("'", "")
    m = _WORD.search(w)
    if not m:
        return ""
    token = m.group(0)
    return _NUMBERS.get(token, token)


def _lyric_tokens(lines: list[str]) -> tuple[list[str], list[int]]:
    """Flatten lyric lines to (tokens, line index per token)."""
    tokens: list[str] = []
    owners: list[int] = []
    for i, line in enumerate(lines):
        for raw in line.split():
            tok = normalize_word(raw)
            if tok:
                tokens.append(tok)
                owners.append(i)
    return tokens, owners


def word_span_ok(start: float, end: float,
                 bpm: Optional[float] = None) -> bool:
    """Whether a Whisper word's duration is plausible for sung speech.

    Judged against the track's own tempo when it is known: see
    :func:`word_bounds`.
    """
    low, high = word_bounds(bpm)
    span = end - start
    return low <= span <= high


def trust_word(token: str, probability: Optional[float],
               lyric_tokens: Optional[set[str]]) -> bool:
    """Whether a transcribed word is worth anchoring a lyric line to.

    Confidence alone is not enough: singing is indistinct, and the three real
    words at the start of the measured track scored 0.000, 0.229 and 0.048 --
    a floor that rejected those would cost the whole opening line.

    So a low-confidence word is kept when the *real lyrics* contain it, and
    dropped when they do not. That combination is what identifies a
    hallucination: "neighal" at 0.001 appears nowhere in the song, while "no,"
    at 0.000 is its first word. The lyrics are information Whisper does not
    have, and this is where it pays.

    Without lyrics to compare against -- a track no source has words for --
    only the confidence is available, and it is applied loosely: the goal is
    to drop obvious junk, not to second-guess the transcription that is the
    only text there is.
    """
    if probability is None:
        return True
    if probability >= LOW_CONFIDENCE:
        return True
    if lyric_tokens is None:
        # No corroboration available; reject only near-zero confidence.
        return probability > 0.005
    return token in lyric_tokens


def _whisper_tokens(words: Iterable, *, bpm: Optional[float] = None,
                    lyric_tokens: Optional[set[str]] = None
                    ) -> tuple[list[str], list[float], float]:
    """Flatten timestamped Whisper words to (tokens, starts, last end).

    Words with an implausible span are dropped rather than kept as anchors:
    Whisper attaches one token to a whole instrumental break often enough that
    trusting it drags a lyric line badly off the singing. The end of the last
    usable word is returned too -- it is a real bound on the song's sung
    content, and better than inventing a tail.
    """
    tokens: list[str] = []
    starts: list[float] = []
    last_end = 0.0
    for w in words:
        start = float(getattr(w, "start", 0.0))
        end = float(getattr(w, "end", start))
        if end > start and not word_span_ok(start, end, bpm):
            continue
        pieces = [normalize_word(raw) for raw in str(getattr(w, "text", "")).split()]
        pieces = [tok for tok in pieces if tok]
        if not pieces:
            continue
        probability = getattr(w, "probability", None)
        pieces = [tok for tok in pieces
                  if trust_word(tok, probability, lyric_tokens)]
        if not pieces:
            continue
        # A multi-word token spans its own duration, so spread the pieces
        # across it rather than stacking them all on the start.
        step = (end - start) / len(pieces) if end > start else 0.0
        for i, tok in enumerate(pieces):
            tokens.append(tok)
            starts.append(start + step * i)
        last_end = max(last_end, end)
    return tokens, starts, last_end


def line_weight(line: str) -> float:
    """Roughly how long a line takes to sing, in arbitrary units.

    Syllables would be better than words, and words are better than nothing:
    what matters is that "I been wondering where you are" is given more of a
    gap than "what's it for". Counting characters rather than words keeps a
    line of long words from being rushed. Commas and punctuation add pause weight
    for natural breathing breaks.
    """
    text = (line or "").strip()
    if not text:
        return 0.0
    # Add pause weight for commas, dashes, semicolons, periods, and exclamation marks.
    pause_bonus = (
        text.count(",") * 0.4
        + text.count("—") * 0.6
        + text.count(";") * 0.5
        + text.count(".") * 0.7
        + text.count("!") * 0.8
    )
    return max(1.0, len(_WORD.findall(text.lower())) + len(text) / 12.0 + pause_bonus)


def _interpolate(times: list[Optional[float]], total: Optional[float],
                 weights: Optional[list[float]] = None,
                 breaks: Optional[list[tuple[float, float]]] = None) -> list[float]:
    """Fill gaps in a partially-timed line list, keeping it non-decreasing.

    A line Whisper never anchored still has to appear at a sensible moment. It
    is placed in proportion to how much singing precedes it, not at an even
    interval: spacing lines evenly makes a long line and a two-word line take
    the same time, which is what makes an otherwise well-anchored alignment
    feel off the beat.
    """
    n = len(times)
    out: list[float] = [0.0] * n
    known = [i for i, t in enumerate(times) if t is not None]
    if weights is None or len(weights) != n:
        weights = [1.0] * n
    gaps = breaks or []

    def share(lo: int, hi: int) -> list[float]:
        """Cumulative weight fraction for lines lo+1..hi-1 within (lo, hi)."""
        span = sum(weights[k] for k in range(lo, hi)) or 1.0
        run, out_fracs = 0.0, []
        for k in range(lo, hi):
            run += weights[k]
            out_fracs.append(run / span)
        return out_fracs

    if not known:
        # Nothing anchored at all: fall back to weight-proportional spacing
        # across whatever duration is known.
        span = total or float(n)
        fracs = share(0, n)
        return [0.0] + [span * f for f in fracs[:-1]]

    for i, t in enumerate(times):
        if t is not None:
            out[i] = t
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:                      # leading untimed lines
            fracs = share(0, nxt)             # type: ignore[arg-type]
            head = times[nxt]                 # type: ignore[index]
            out[i] = max(0.0, head * (fracs[i - 1] if i else 0.0))
        elif nxt is None:                     # trailing untimed lines
            tail = total if total and total > times[prev] else times[prev] + (n - prev)  # type: ignore[operator]
            fracs = share(prev, n)
            out[i] = times[prev] + (tail - times[prev]) * fracs[i - prev - 1]  # type: ignore[operator]
        else:
            fracs = share(prev, nxt)
            # Distributed across the *sung* seconds between the anchors, then
            # mapped back through any instrumental break, so lines cluster
            # where there is singing instead of drifting into a solo.
            sung = _sung_span(times[prev], times[nxt], gaps)   # type: ignore[arg-type]
            out[i] = _advance_through_breaks(
                times[prev], sung * fracs[i - prev - 1], gaps)  # type: ignore[arg-type]

    # Enforce monotonicity; a lyric line may never move backwards in time.
    for i in range(1, n):
        if out[i] < out[i - 1]:
            out[i] = out[i - 1]
    return out


def find_breaks(starts: list[float],
                bpm: Optional[float]) -> list[tuple[float, float]]:
    """Stretches with too few words to be singing: instrumental breaks.

    Scanned in bars rather than seconds, because "quiet for four bars" means
    the same thing at any tempo while "quiet for six seconds" does not. A solo
    or an intro leaves a gap no lyric belongs in, and spreading lines evenly
    across it is what puts them far from the vocal.
    """
    bar = bar_seconds(bpm)
    if bar is None or len(starts) < 2:
        return []
    window = bar * BREAK_BARS
    breaks: list[tuple[float, float]] = []
    for a, b in zip(starts, starts[1:]):
        if (b - a) >= window:
            breaks.append((a, b))
    return breaks


def _sung_span(lo: float, hi: float,
               breaks: list[tuple[float, float]]) -> float:
    """Seconds between lo and hi that are not inside an instrumental break."""
    total = max(0.0, hi - lo)
    for start, end in breaks:
        overlap = min(hi, end) - max(lo, start)
        if overlap > 0:
            total -= overlap
    return max(0.0, total)


def _advance_through_breaks(start: float, sung: float,
                            breaks: list[tuple[float, float]]) -> float:
    """Move ``sung`` seconds of singing forward from ``start``, skipping breaks.

    The inverse of :func:`_sung_span`: a line a third of the way through the
    *vocal* has to be placed past whatever silence sits in between, not a third
    of the way through the clock.
    """
    remaining, position = sung, start
    for gap_start, gap_end in sorted(breaks):
        if gap_end <= position:
            continue
        usable = max(0.0, gap_start - position)
        if remaining <= usable:
            return position + remaining
        remaining -= usable
        position = gap_end
    return position + remaining


def grid_phase(starts: list[float], beat: float) -> float:
    """Where the beat grid sits, inferred from the words themselves.

    A tempo alone does not say *when* the beats fall. Singing lands on or just
    after a beat far more often than between, so the residual most anchors
    share is the grid's offset. Snapping to a grid whose phase is wrong makes
    the timing worse rather than better.
    """
    if beat <= 0 or not starts:
        return 0.0
    residuals = sorted((t % beat) for t in starts)
    return residuals[len(residuals) // 2]


def snap_to_grid(t: float, beat: Optional[float], phase: float,
                 *, tolerance: float = 0.25) -> float:
    """Pull a time onto the nearest beat, when it is already close.

    ``tolerance`` is a fraction of a beat: a time further than that from the
    grid is left alone rather than dragged, since it is a genuine off-beat
    entry and not jitter to correct.
    """
    if not beat or beat <= 0:
        return t
    offset = t - phase
    nearest = round(offset / beat) * beat + phase
    if abs(nearest - t) <= beat * tolerance:
        return max(0.0, nearest)
    return t


def _earliest_match(line_tokens: set[str], whisper_toks: list[str],
                    starts: list[float]) -> Optional[float]:
    """When any of a line's words is first heard."""
    for tok, start in zip(whisper_toks, starts):
        if tok in line_tokens:
            return start
    return None


def _correct_opening_anchor(per_line: list[Optional[float]],
                            lines: list[str], whisper_toks: list[str],
                            starts: list[float],
                            bpm: Optional[float]) -> list[Optional[float]]:
    """Pull the first lyric back if it was matched to a later repeat.

    SequenceMatcher optimises globally, so with a repeated chorus it will
    happily pin the *opening* line to a later occurrence of the same words --
    measured at 21.16s on a track whose vocal starts at 14.0s. Every other line
    is constrained by the anchors around it; the first one has nothing before
    it, which is exactly why it drifts.

    The correction is the earliest moment any of that line's own words is
    heard. That is a weaker signal than an aligned block -- it can catch a
    mishearing -- so it is only applied when it disagrees by more than a bar,
    and never moved later.
    """
    if not per_line or per_line[0] is None or not starts:
        return per_line
    bar = bar_seconds(bpm) or 2.0
    own_tokens = {t for t in (normalize_word(w) for w in lines[0].split()) if t}
    first = _earliest_match(own_tokens, whisper_toks, starts)
    if first is None:
        return per_line
    if per_line[0] - first > bar:
        out = list(per_line)
        out[0] = first
        return out
    return per_line


def _drop_impossible_anchors(per_line: list[Optional[float]],
                             weights: list[float]) -> list[Optional[float]]:
    """Discard anchors that would require singing faster than anyone can.

    Repeated lyrics are where alignment fails: "no one's ready for your war"
    appears eight times, and the matcher is free to anchor a late line to an
    early occurrence. The giveaway is not the anchor itself but the interval it
    leaves -- on this track twenty lines were pinned into four seconds.

    An anchor that leaves too little room for the lines before it is dropped,
    and those lines are interpolated instead. Losing a suspect anchor costs a
    little precision; keeping it compresses whole verses into a blur.
    """
    kept = list(per_line)
    last_index: Optional[int] = None
    for i, t in enumerate(kept):
        if t is None:
            continue
        if last_index is None:
            last_index = i
            continue
        needed = sum(weights[last_index:i]) * MIN_SECONDS_PER_WEIGHT
        if (t - kept[last_index]) < needed:      # type: ignore[operator]
            kept[i] = None                       # implausible; interpolate it
            continue
        last_index = i
    return kept


def review_main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    """List stored alignments whose timings are mostly interpolated.

    A worklist, not a delete list. The words are unaffected either way; what is
    in question is only whether the *timings* were taken from heard words or
    guessed between distant anchors, and the latter is worth a listen.
    """
    import argparse

    from . import localcache

    ap = argparse.ArgumentParser(
        prog="karaoke-align-review",
        description="Alignments whose timings rest on few anchors")
    ap.add_argument("--threshold", type=float,
                    default=localcache.GOOD_ANCHOR_FRACTION,
                    help="anchored fraction below which to list (default "
                         f"{localcache.GOOD_ANCHOR_FRACTION})")
    args = ap.parse_args(argv)

    conn = localcache.connect()
    try:
        rows = localcache.alignments_for_review(conn, args.threshold)
        total = conn.execute(
            "SELECT count(*) n FROM alignment_support").fetchone()["n"]
    finally:
        conn.close()

    if not total:
        print("No alignment support recorded yet. It is written when a track "
              "is synced,\nso this fills in as tracks are processed rather "
              "than being backfillable:\nthe anchors are gone once the "
              "timings are written, and Whisper does not\nreproduce them.")
        return 0
    if not rows:
        print(f"{total} alignment(s) recorded, none below "
              f"{args.threshold:.0%} anchored.")
        return 0

    print(f"{len(rows)} of {total} alignment(s) rest on few anchors "
          f"(under {args.threshold:.0%}):\n")
    for row in rows:
        gap = row["longest_gap_s"]
        print(f"  {row['fraction']:5.0%} anchored  "
              f"{row['anchored']:3}/{row['lines']:<3} lines  "
              f"{'' if gap is None else f'worst gap {gap:4.0f}s  '}"
              f"{row['artist']} - {row['title']}")
    print("\nTimings only. The lyrics themselves are unaffected, and a low "
          "score means\nuncertain rather than wrong: a 45%-anchored track "
          "measured 0.77s of error\nwhile a 54%-anchored one measured 2.63s.")
    return 0


def anchor_coverage(per_line: list[Optional[float]],
                    horizon: Optional[float]) -> tuple[float, float]:
    """How much of a track the anchors actually cover.

    Returns ``(longest_unanchored_seconds, unanchored_fraction)``.

    Interpolation is only as good as the anchors it runs between. Across a
    short gap it is reliable; across a long one it is a guess dressed as a
    measurement -- on GAUPA - Febersvan, anchors existed in the first 58s and
    after 337s of a 448-second track, and the eleven lines interpolated across
    the 280-second middle came out up to 183 seconds wrong.

    That gap cannot be closed by matching harder. Whisper heard the vocal at
    the right moment and got the words wrong ("She'll turn" for "Our
    shelter"), and no character or phonetic similarity admits that pairing
    while rejecting "feel"/"fell" and "feel"/"feet", which sit at the same
    ratio and would manufacture false anchors in a song whose lyric is mostly
    the word "feel". So the honest move is to notice the drought and say so.
    """
    anchored = sorted(t for t in per_line if t is not None)
    if not anchored:
        return (horizon or 0.0, 1.0)
    span = horizon if horizon and horizon > 0 else anchored[-1]
    if span <= 0:
        return (0.0, 0.0)

    # Gaps between consecutive anchors, plus the run-in before the first and
    # the run-out after the last: an alignment anchored only in its opening
    # thirty seconds is as poorly covered as one anchored only in the middle.
    edges = [anchored[0]] + [b - a for a, b in zip(anchored, anchored[1:])] \
        + [max(0.0, span - anchored[-1])]
    longest = max(edges)
    return (longest, min(1.0, longest / span))


def align_lines(
    lyric_lines: list[str],
    words: Iterable,
    *,
    total_duration: Optional[float] = None,
    bpm: Optional[float] = None,
    report: Optional[dict] = None,
) -> list[tuple[float, str]]:
    """Assign a timestamp to each real lyric line from Whisper word timings.

    Returns ``(seconds, line_text)`` with the ORIGINAL line text — punctuation,
    capitalisation and all. Only the timings come from Whisper.

    ``report``, if given, is filled with diagnostics about how well the
    timings are supported: how many lines were anchored rather than
    interpolated, and how long the worst unanchored stretch was. A caller can
    use those to decide whether to trust the result, which matters because a
    poorly anchored alignment looks exactly like a good one from the outside.
    """
    lines = [ln for ln in (lyric_lines or [])]
    if not lines:
        return []

    lyric_toks, owners = _lyric_tokens(lines)
    weights = [line_weight(ln) for ln in lines]
    whisper_toks, starts, last_end = _whisper_tokens(
        words, bpm=bpm, lyric_tokens=set(lyric_toks))
    if not lyric_toks or not whisper_toks:
        # Nothing was heard, so every line below is spaced by weight alone.
        # The report is filled here as well as on the main path: leaving it
        # empty let this case slip past both the support flag and the caller's
        # zero-anchor refusal, and "Jimi Hendrix - Sweet Angel" was stored as
        # 980 characters of whisper_aligned timings with no anchors behind any
        # of them and nothing recorded to say so.
        if report is not None:
            report.update(lines=len(lines), anchored=0,
                          longest_gap_s=(total_duration or 0.0),
                          unanchored_fraction=1.0,
                          horizon_s=total_duration,
                          total_duration_s=total_duration)
        return list(zip(_interpolate([None] * len(lines), total_duration, weights),
                        lines))
    # The last sung word bounds the lyrics better than the file's length, which
    # includes outros and silence the words do not cover.
    horizon = total_duration
    if last_end > 0 and (horizon is None or last_end < horizon):
        horizon = last_end

    # Earliest anchored time per line.
    per_line: list[Optional[float]] = [None] * len(lines)
    matcher = SequenceMatcher(None, lyric_toks, whisper_toks, autojunk=False)
    for li, wi, size in matcher.get_matching_blocks():
        for k in range(size):
            line_no = owners[li + k]
            t = starts[wi + k]
            if per_line[line_no] is None or t < per_line[line_no]:  # type: ignore[operator]
                per_line[line_no] = t

    per_line = _correct_opening_anchor(per_line, lines, whisper_toks, starts, bpm)
    per_line = _drop_impossible_anchors(per_line, weights)
    if report is not None:
        longest, fraction = anchor_coverage(per_line, horizon)
        report.update(
            lines=len(lines),
            anchored=sum(1 for t in per_line if t is not None),
            longest_gap_s=longest,
            unanchored_fraction=fraction,
            # Coverage is measured against the *sung* span, not the file: the
            # horizon is pulled back to the last heard word so that outros and
            # trailing silence do not read as a drought. That makes the two
            # worth reporting separately -- a sung span far shorter than the
            # track is its own pathology, where every line is crammed into
            # whatever fragment Whisper happened to hear.
            horizon_s=horizon,
            total_duration_s=total_duration,
        )
    breaks = find_breaks(starts, bpm)
    placed = _interpolate(per_line, horizon, weights, breaks)

    # Finally pull each line onto the beat grid. The tempo says how far apart
    # beats are; the anchors say where they fall. Interpolated lines are the
    # ones that drift, and a line that enters just off the beat reads as early.
    beat = beat_seconds(bpm)
    if beat:
        phase = grid_phase([t for t in per_line if t is not None] or starts, beat)
        placed = [snap_to_grid(t, beat, phase) for t in placed]
        for i in range(1, len(placed)):
            if placed[i] < placed[i - 1]:
                placed[i] = placed[i - 1]
    return list(zip(placed, lines))


def align_lyrics_to_lrc(
    plain_lyrics: str,
    words: Iterable,
    *,
    total_duration: Optional[float] = None,
    bpm: Optional[float] = None,
) -> str:
    """Align plain lyrics onto Whisper word timings and render LRC.

    Blank lines are dropped: they are stanza separators in the source text and
    have nothing to sing, so they should not consume a timestamp.
    """
    from .whisper_sync import lines_to_lrc

    lines = [ln.strip() for ln in (plain_lyrics or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return lines_to_lrc(align_lines(lines, words, total_duration=total_duration,
                                    bpm=bpm))
