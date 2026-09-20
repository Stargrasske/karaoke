"""Aggregate sentiment + rhythm visuals for the karaoke free-space panel.

Pure and dependency-free: turns a block of lyric text into a coarse mood profile
and a small ASCII "sentiment arc", and turns a BPM/energy into a simple rhythm
bar. This is a creative vibe cue, not real affect analysis — it builds on the
lexicon in ``sentiment``.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from .sentiment import MOODS, mood_of, score_line

# Glyph per mood, used by the arc only. These are East-Asian *ambiguous* width,
# so never place them where something after them has to stay aligned — see
# sentiment_bars.
_MOOD_MARK = {
    "happy": "▲",
    "sad": "▽",
    "angry": "✷",
    "tender": "♥",
    "neutral": "·",
}

# Width of the bar-chart label column: the longest label is "tender". ASCII
# only, so len() and display width agree.
_BAR_LABEL_W = 6


def cell_width(text: str, *, ambiguous: int = 1) -> int:
    """Terminal cell width of ``text``.

    Agrees with ``rich.cells.cell_len`` under the default ``ambiguous=1``, but
    keeps the policy an explicit argument rather than an inherited guess: East-
    Asian "Ambiguous" characters are genuinely terminal-dependent, and callers
    that know their terminal draws them wide can say so.
    """
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue                      # combining marks add no width
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("W", "F"):
            total += 2
        elif eaw == "A":
            total += ambiguous
        else:
            total += 1
    return total


def pad_cells(text: str, width: int, *, align: str = "center") -> str:
    """Pad ``text`` with spaces to ``width`` terminal cells.

    Never truncates: a string already wider than ``width`` is returned as-is,
    since silently cutting a glyph is worse than overflowing by one cell.
    """
    pad = max(0, width - cell_width(text))
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    if align == "right":
        return " " * pad + text
    return text + " " * pad


@dataclass(frozen=True)
class SentimentProfile:
    """Coarse mood breakdown of a lyric block."""

    counts: dict[str, int]
    dominant: str
    total_hits: int
    line_moods: list[str] = field(default_factory=list)

    @property
    def shares(self) -> dict[str, float]:
        """Fraction of mood hits per mood (0..1)."""
        if self.total_hits <= 0:
            return {m: 0.0 for m in MOODS if m != "neutral"}
        return {m: c / self.total_hits for m, c in self.counts.items()}


def analyze_sentiment(text: str) -> SentimentProfile:
    """Aggregate per-line moods across a lyric block into a profile."""
    counts = {"happy": 0, "sad": 0, "angry": 0, "tender": 0}
    line_moods: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        s = score_line(line)
        for mood in counts:
            counts[mood] += s.get(mood, 0)
        line_moods.append(mood_of(line))
    total = sum(counts.values())
    dominant = max(counts, key=lambda m: counts[m]) if total else "neutral"
    return SentimentProfile(counts, dominant if total else "neutral",
                            total, line_moods)


def sentiment_arc(profile: SentimentProfile, width: int = 24) -> str:
    """A one-line ASCII arc of the per-line moods (down-sampled to `width`)."""
    moods = profile.line_moods
    if not moods:
        return "·" * width
    out = []
    for i in range(width):
        idx = int(i * len(moods) / width)
        out.append(_MOOD_MARK.get(moods[idx], "·"))
    return "".join(out)


def sentiment_bars(profile: SentimentProfile, width: int = 12) -> str:
    """Small horizontal bar chart of mood shares.

    Deliberately label-then-bar with NO leading glyph. The marks in
    ``_MOOD_MARK`` are East-Asian *ambiguous* width: terminals disagree about
    whether they occupy one cell or two, and font fallback can differ per glyph
    — one report had "▽" drawn wide while "▲ ♥ ✷" stayed narrow, so only the
    "sad" bar was pushed a column right. Nothing can measure that reliably
    (``rich.cells.cell_len`` reports 1 for all four), so the fix is to put
    nothing mis-measurable before the bar. Everything left of it is ASCII, and
    every bar therefore starts in the same column on every terminal.

    The marks still appear in :func:`sentiment_arc`, where a shift is harmless.
    """
    shares = profile.shares
    lines = []
    for mood in ("happy", "tender", "sad", "angry"):
        filled = int(round(shares.get(mood, 0.0) * width))
        # NB "█"/"░" are themselves ambiguous-width. Terminals seen so far draw
        # them narrow; if one ever doesn't, swap them for "#"/"." here.
        bar = "█" * filled + "░" * (width - filled)
        lines.append(f"{mood:<{_BAR_LABEL_W}s} {bar}")
    return "\n".join(lines)


# How much of each beat the pulse spends in the air. Short: the hop should read
# as a strike on the beat, not as hovering between them.
HOP_FRACTION = 0.35

# Beats per traverse of the bar. Two gives a visible sweep at ordinary tempos
# without the pulse becoming a blur at fast ones.
BOUNCE_BEATS = 2.0

# Beats for the cartwheel to cross the panel. Slower than the bar's sweep: the
# figure is seven cells wide and a fast traverse turns it into a smear.
CARTWHEEL_BEATS = 4.0


def rhythm_bar(bpm: float | None, elapsed: float = 0.0, width: int = 16) -> str:
    """An animated rhythm indicator driven by BPM and elapsed time.

    Two rows. The pulse travels left and right and **reverses at each end**
    rather than wrapping: a sawtooth that teleports back to the start reads as
    drift, and the eye follows the jump rather than the beat.

    On each beat the pulse is drawn on the upper row and lands on the lower one
    between beats. The horizontal travel alone is what a metronome does not
    have; the hop is what actually marks time.

    Depends only on ``elapsed``, so the same instant always renders identically
    and the caller's timer interval cannot introduce jitter.

    With no BPM there is no beat to keep, so it stays a single static bar.
    """
    if not bpm or bpm <= 0:
        return "‑" * width + "  (bpm ?)"
    if width < 1:
        return f"  {bpm:.0f} bpm"

    beat = 60.0 / bpm
    beats = elapsed / beat

    # Triangle wave over BOUNCE_BEATS: 0 -> 1 -> 0, so the pulse turns around at
    # the ends instead of jumping back to the start.
    phase = (beats / BOUNCE_BEATS) % 2.0
    travel = phase if phase <= 1.0 else 2.0 - phase
    pos = min(width - 1, int(travel * (width - 1) + 0.5))

    airborne = (beats % 1.0) < HOP_FRACTION
    upper = ["·"] * width
    lower = ["·"] * width
    (upper if airborne else lower)[pos] = "●"
    return ("".join(upper) + "\n"
            + "".join(lower) + f"  {bpm:.0f} bpm")


def tempo_word(bpm: float | None) -> str:
    """Rough Italian tempo marking for a BPM (a fun, human label)."""
    if not bpm or bpm <= 0:
        return "unknown"
    if bpm < 60:
        return "largo (very slow)"
    if bpm < 76:
        return "adagio (slow)"
    if bpm < 108:
        return "andante (walking)"
    if bpm < 120:
        return "moderato"
    if bpm < 156:
        return "allegro (fast)"
    if bpm < 176:
        return "vivace (lively)"
    return "presto (very fast)"


_CARTWHEEL_FRAMES = [
    [
        "   o   ",
        "  /|\\  ",
        "  / \\  "
    ],
    [
        " \\ o / ",
        "   |   ",
        "  / \\  "
    ],
    [
        "  _ o  ",
        "   /\\  ",
        "  | \\  "
    ],
    [
        "   __\\ ",
        " ___\\o ",
        " /)  | "
    ],
    [
        "  __|  ",
        "  \\o   ",
        "  ( \\  "
    ],
    [
        "  \\ /  ",
        "   |   ",
        "  /o\\  "
    ],
    [
        "  |__  ",
        "   o/  ",
        "  / )  "
    ],
    [
        "  o _  ",
        "  /\\   ",
        "  / |  "
    ],
    [
        "   o   ",
        "  /|\\  ",
        "  / \\  "
    ]
]


def cartwheel_frame(bpm: float | None, elapsed: float, max_width: int = 24) -> str:
    """An ASCII cartwheel that rolls with the beat.

    Like :func:`rhythm_bar`, the figure **turns back** at each end rather than
    teleporting to the start, and it **hops on the beat**: it sits a row higher
    for the first part of each beat and lands for the rest. A figure that only
    slides across reads as drift; the landing is what marks time.

    The rotation reverses with the travel, because a wheel rolling leftwards
    does not keep spinning clockwise. Without that the figure looks like it is
    being dragged backwards rather than rolling.

    Total height is constant, so the panel below never shifts as it hops.
    """
    if not bpm or bpm <= 0:
        bpm = 120.0  # default to a nice 120 BPM tempo

    beat_duration = 60.0 / bpm
    if beat_duration <= 0:
        beat_duration = 0.5

    beats = elapsed / beat_duration
    within_beat = beats % 1.0

    # Triangle over CARTWHEEL_BEATS: out and back, reversing at the ends.
    phase = (beats / CARTWHEEL_BEATS) % 2.0
    forward = phase <= 1.0
    travel = phase if forward else 2.0 - phase

    # One full rotation per beat, spinning the way it is travelling.
    index = int(within_beat * len(_CARTWHEEL_FRAMES))
    if not forward:
        index = len(_CARTWHEEL_FRAMES) - 1 - index
    frame = _CARTWHEEL_FRAMES[index % len(_CARTWHEEL_FRAMES)]

    figure_width = 7
    max_padding = max(0, max_width - figure_width)
    padding = " " * int(travel * max_padding)

    lines = [padding + line for line in frame]
    # Airborne on the beat, landed between. The blank row moves from below to
    # above so the block keeps its height and nothing below it jumps.
    if within_beat < HOP_FRACTION:
        lines = lines + [""]
    else:
        lines = [""] + lines
    return "\n".join(lines)


def duet_cartwheel_frame(
    bpm: float | None,
    elapsed: float,
    *,
    mood: str = "neutral",
    genre: str = "",
    energy: float | None = None,
    brightness: float | None = None,
    width: int = 32,
) -> str:
    """Two mirrored cartwheel dancers with a shared musical interaction.

    The choreography is a 32-beat phrase: approach for two bars, stay together
    for two bars, retreat for two bars, then dance apart for two bars. The left
    dancer travels right and the mirrored dancer travels left. The figures hop
    only at 100 BPM or above, while their cartwheel motion continues at slower
    tempos without vertical jumping.
    """
    safe_bpm = float(bpm or 90.0)
    beat_duration = 60.0 / max(safe_bpm, 1.0)
    beats = elapsed / beat_duration
    phrase_beats = 32.0
    phrase = beats % phrase_beats

    # Approach, together, retreat, apart. Smoothstep makes the meeting feel
    # intentional rather than like two sprites snapping to a new coordinate.
    def smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    # Bass proxy: loudness/energy drives the left dancer's weight. High-tone
    # proxy: spectral brightness drives the right dancer's quickness.
    e = max(0.0, min(1.0, float(energy if energy is not None else 0.5)))
    high = max(0.0, min(1.0, float(brightness if brightness is not None else 0.5)))
    bass_drive = 0.55 * e + 0.45 * (1.0 - high)
    treble_drive = 0.55 * high + 0.45 * e

    apart_gap = max(4, int((1.0 - e) * 7) + 5)
    if phrase < 8.0:
        meeting = False
        progress = smoothstep(phrase / 8.0)
        gap = round(apart_gap + (1 - apart_gap) * progress)
    elif phrase < 16.0:
        meeting = True
        gap = 1
    elif phrase < 24.0:
        meeting = False
        progress = smoothstep((phrase - 16.0) / 8.0)
        gap = round(1 + (apart_gap - 1) * progress)
    else:
        meeting = False
        gap = apart_gap

    # Different genres get different cartwheel cadence and pose emphasis while
    # preserving the same two-person interaction choreography.
    genre_text = (genre or "").casefold()
    if any(word in genre_text for word in ("metal", "hardcore", "punk", "rock")):
        genre_factor = 1.5
        label = "angular duet"
    elif any(word in genre_text for word in ("electronic", "techno", "dance", "house", "edm")):
        genre_factor = 2.0
        label = "electric duet"
    elif any(word in genre_text for word in ("hip hop", "hip-hop", "rap", "soul")):
        genre_factor = 1.0
        label = "groove duet"
    else:
        genre_factor = 1.0
        label = "duet"

    left_frame_index = int(beats * (genre_factor + 0.8 * bass_drive)) % len(_CARTWHEEL_FRAMES)
    right_frame_index = int(beats * (genre_factor + 0.8 * treble_drive) + 1.0) % len(_CARTWHEEL_FRAMES)
    left_frame = _CARTWHEEL_FRAMES[left_frame_index]
    right_frame = tuple(_mirror_ascii_pose(line) for line in _CARTWHEEL_FRAMES[right_frame_index])

    figure_width = max(max(len(line) for line in left_frame),
                       max(len(line) for line in right_frame))

    # Never compose two figures when the panel cannot hold two complete poses.
    # Overlapping two 7-cell cartwheels makes limbs overwrite each other and
    # creates the "half a person on each edge" artifact. A single centered
    # dancer is clearer and remains entirely inside the panel.
    narrow_duet = width < (figure_width * 2 + 2)
    if narrow_duet:
        slash = chr(92)
        # Compact three-column pose: a whole dancer still fits in a tight
        # visual panel instead of showing clipped limbs.
        narrow_frame = (" o ", f"/|{slash}", f"/ {slash}")
        narrow_width = min(3, width)
        narrow_x = max(0, (width - narrow_width) // 2)
        rows: list[str] = []
        airborne = safe_bpm >= 100.0 and (beats % 1.0) < HOP_FRACTION
        for source_line in narrow_frame:
            line = source_line[:narrow_width]
            rows.append((" " * narrow_x + line)[:width].rstrip())
        rows.append((" " * max(0, width // 2 - 1) + "· solo")[:width].rstrip())
        rows.append("")
        if airborne:
            rows.insert(0, "")
        return "\n".join(rows)

    centre = width // 2
    # Both x positions are chosen from complete figure boxes. This keeps every
    # head, arm, and wheel inside its own half of the panel.
    max_safe_gap = max(0, (width - 2 * figure_width) // 2)
    gap = min(max(0, gap), max_safe_gap)
    left_x = max(0, centre - gap - figure_width)
    right_x = min(width - figure_width, centre + gap)

    rows: list[str] = []
    airborne = safe_bpm >= 100.0 and (beats % 1.0) < HOP_FRACTION
    for left_line, right_line in zip(left_frame, right_frame):
        line = [" "] * width
        # Hard ownership boundaries: the left dancer may only draw in the
        # left half, and the mirrored dancer only in the right half. Even if a
        # pose overlaps at the meeting point, characters are dropped rather
        # than merged, wrapped, or drawn on the other side.
        for col, char in enumerate(left_line):
            target = left_x + col
            if 0 <= target < centre and char != " ":
                line[target] = char
        for col, char in enumerate(right_line):
            target = right_x + col
            if centre <= target < width and char != " ":
                line[target] = char
        rows.append("".join(line).rstrip())

    if meeting:
        rows.append(" " * max(0, centre - 2) + "♡   ♥ together")
        label = "♥ together"
    else:
        rows.append(" " * max(0, centre - 1) + "·  " + label)

    # Keep a constant number of rows. The blank line moves inside the frame,
    # rather than changing the widget height and making the UI jump.
    if airborne:
        rows = [""] + rows
    else:
        rows.append("")
    return "\n".join(rows)


def duet_dance_frame(
    bpm: float | None,
    elapsed: float,
    *,
    mood: str = "neutral",
    genre: str = "",
    energy: float | None = None,
    width: int = 28,
) -> str:
    """Render two mirrored ASCII dancers whose relationship follows the song.

    The dancers keep a respectful distance for ordinary songs, occasionally
    meet for tender/love lyrics, and become more angular or chaotic for
    aggressive genres. Hops are deliberately disabled below 100 BPM: slow
    songs sway and pulse instead of looking like the figures are jumping.
    """
    import math

    safe_bpm = float(bpm or 90.0)
    beat = 60.0 / max(safe_bpm, 1.0)
    beats = elapsed / beat
    pulse = math.sin(beats * math.tau)
    half = max(10, width // 2)
    genre_text = (genre or "").casefold()

    if any(word in genre_text for word in ("metal", "hardcore", "punk", "rock")):
        style = "angular"
    elif any(word in genre_text for word in ("electronic", "techno", "dance", "house", "edm")):
        style = "wave"
    elif any(word in genre_text for word in ("hip hop", "hip-hop", "rap", "soul")):
        style = "groove"
    elif mood == "tender":
        style = "tender"
    else:
        style = "sway"

    # The mean song energy influences how far apart they dance. Tender songs
    # pull them together; high-energy songs give them room for independent
    # movement. They meet only during occasional musical phrases.
    e = max(0.0, min(1.0, float(energy if energy is not None else 0.5)))
    tender = mood == "tender" or any(word in genre_text for word in ("love", "romance"))
    phrase = (beats / 8.0) % 1.0
    meet = tender and phrase > 0.70
    max_gap = max(2, int((1.0 - e) * (half * 0.45)))
    gap = 1 if meet else max_gap + int(abs(pulse) * 2)
    gap = min(gap, half - 8)

    # Below 100 BPM they stay grounded. At 100+ BPM a short first-beat lift
    # adds a hop, never changing the total block height.
    airborne = safe_bpm >= 100.0 and (beats % 1.0) < HOP_FRACTION
    lift = 1 if airborne else 0

    slash = chr(92)
    if style == "angular":
        left = [(" o ", f"{slash}|/", f"/ {slash}"), (" o ", f"/|{slash}", f" {slash}/ ")]
    elif style == "wave":
        left = [(" ~o ", f" /|", f" / {slash}"), ("o~  ", f"|{slash} ", f"/ {slash}")]
    elif style == "groove":
        left = [(" o ", f"_/{slash}", " /| "), (" o ", f"{slash}_|", " |/ ")]
    elif style == "tender":
        left = [(" o ", " /|", f" / {slash}"), (" o ", f" /{slash}", " /| ")]
    else:
        left = [(" o ", " /|", f" / {slash}"), (" o ", f"{slash}|/", " /| ")]

    pose = left[int(beats * 2) % len(left)]
    right = tuple(_mirror_ascii_pose(line) for line in pose)
    center = max(1, half - gap - 4)
    left_x = max(0, center - len(pose[0]))
    right_x = min(width - len(right[0]), half + gap)

    rows = []
    for line_left, line_right in zip(pose, right):
        line = [" "] * width
        for i, char in enumerate(line_left):
            if left_x + i < width:
                line[left_x + i] = char
        for i, char in enumerate(line_right):
            if right_x + i < width:
                line[right_x + i] = char
        rows.append("".join(line).rstrip())

    if meet:
        rows.append(" " * max(0, half - 3) + "♡")
    else:
        rows.append(" " * max(0, half - 1) + ("·" if style != "angular" else "✷"))
    if lift:
        rows.insert(0, "")
    else:
        rows.append("")

    label = "♥ together" if meet else style
    return "\n".join(rows) + f"  {label}"


def _mirror_ascii_pose(line: str) -> str:
    """Mirror a tiny ASCII dancer pose without disturbing its width."""
    table = str.maketrans("/\\|_", "\\/|_")
    return line.translate(table)[::-1]


# -- additional dance poses for variety on a wide floor --------------------

_DANCE_POSES: list[list[str]] = [
    # 0  standing
    [" o ", "/|\\", "/ \\"],
    # 1  arms up
    ["\\o/", " | ", "/ \\"],
    # 2  left lean
    [" o ", "/| ", "/ \\"],
    # 3  right lean
    [" o", " |\\", "/ \\"],
    # 4  disco point
    [" o/", " | ", "/ \\"],
    # 5  crouch
    [" o ", " /\\", " ||"],
    # 6  kick left
    [" o ", "/|\\", "_/ "],
    # 7  kick right
    [" o ", "/|\\", " \\_"],
    # 8  wave
    ["~o~", " | ", "/ \\"],
    # 9  jump (arms wide)
    ["\\o/", " | ", "   "],
    # 10 headbang
    [" o ", "\\|/", "/ \\"],
    # 11 dab
    ["  o", " /|_", "/ \\ "],
]


def dance_floor_frame(
    bpm: float | None,
    elapsed: float,
    *,
    width: int = 80,
    height: int = 12,
    num_dancers: int = 4,
    mood: str = "neutral",
    genre: str = "",
    energy: float | None = None,
) -> str:
    """Render a dance floor with multiple ASCII dancers across the full width.

    Each dancer cycles through poses at their own phase offset so the crowd
    looks organic rather than synchronised. They hop on the beat at 100+ BPM
    and sway at slower tempos.

    Args:
        bpm: Current track tempo.
        elapsed: Seconds since the track started.
        width: Available terminal columns.
        height: Available terminal rows.
        num_dancers: How many dancers to render (1-12).
        mood: Current lyric mood.
        genre: Current genre tag.
        energy: Track energy (0-1).
    """
    import math

    safe_bpm = float(bpm or 90.0)
    beat = 60.0 / max(safe_bpm, 1.0)
    beats = elapsed / beat
    e = max(0.0, min(1.0, float(energy if energy is not None else 0.5)))

    num_dancers = max(1, min(12, num_dancers))
    figure_w = 5   # each dancer occupies 5 columns
    # minimum gap so dancers don't overlap
    min_gap = 2
    # available space per dancer slot
    slot_w = max(figure_w, width // max(1, num_dancers))
    # If we can't fit all dancers, reduce count
    while num_dancers > 1 and num_dancers * (figure_w + min_gap) > width:
        num_dancers -= 1

    # Choose pose set based on genre
    genre_text = (genre or "").casefold()
    if any(w in genre_text for w in ("metal", "hardcore", "punk", "rock")):
        pose_indices = [0, 1, 10, 6, 7, 2, 3, 8]  # headbanging, kicks
        floor_char = "✷"
    elif any(w in genre_text for w in ("electronic", "techno", "dance", "house", "edm")):
        pose_indices = [1, 8, 9, 4, 5, 0, 3, 2]  # waves, jumps
        floor_char = "·"
    elif any(w in genre_text for w in ("hip hop", "hip-hop", "rap", "soul")):
        pose_indices = [4, 11, 5, 0, 2, 3, 1, 6]  # disco, dab
        floor_char = "~"
    elif mood == "tender":
        pose_indices = [0, 2, 3, 1, 0, 3, 2, 1]  # gentle sways
        floor_char = "♡"
    else:
        pose_indices = [0, 1, 2, 3, 4, 6, 7, 8]
        floor_char = "·"

    # Speed factor: faster energy = faster pose cycling
    speed = 1.0 + e * 0.8

    # Build each dancer's current pose
    dancer_poses: list[list[str]] = []
    dancer_airborne: list[bool] = []
    for d in range(num_dancers):
        # Phase offset: each dancer is offset by a fraction of the total cycle
        # plus a golden-ratio-based irrational stagger so they never fully sync
        phase_offset = d * 1.618033988749895
        dancer_beats = beats * speed + phase_offset

        # Pick pose from the genre set
        pidx = int(dancer_beats * 2) % len(pose_indices)
        pose = _DANCE_POSES[pose_indices[pidx] % len(_DANCE_POSES)]

        # Should this dancer face left or right? Alternate.
        if d % 2 == 1:
            pose = [_mirror_ascii_pose(line) for line in pose]

        dancer_poses.append(pose)

        # Hop on the beat (100+ BPM only)
        within = dancer_beats % 1.0
        airborne = safe_bpm >= 100.0 and within < HOP_FRACTION
        dancer_airborne.append(airborne)

    # How many rows the dancers need: 3 body + 1 hop/land + 1 floor label
    figure_h = 3
    total_dancer_h = figure_h + 1  # +1 for hop blank row

    # Build the dance floor canvas
    canvas_h = max(total_dancer_h + 2, min(height, total_dancer_h + 4))
    canvas: list[list[str]] = [[" "] * width for _ in range(canvas_h)]

    # Place each dancer on the canvas
    for d in range(num_dancers):
        # x position: evenly spaced across the width
        x = int((d + 0.5) * width / num_dancers) - figure_w // 2
        x = max(0, min(width - figure_w, x))

        # y position: base row, offset by hop
        base_y = canvas_h - figure_h - 2  # -2 for floor line + bottom margin
        if dancer_airborne[d]:
            base_y -= 1  # hop up one row

        pose = dancer_poses[d]
        for row_i, pose_line in enumerate(pose):
            y = base_y + row_i
            if 0 <= y < canvas_h:
                for col_i, ch in enumerate(pose_line):
                    cx = x + col_i
                    if 0 <= cx < width and ch != " ":
                        canvas[y][cx] = ch

    # Draw the dance floor line
    floor_y = canvas_h - 2
    if 0 <= floor_y < canvas_h:
        # Pulsing floor pattern
        pulse_phase = math.sin(beats * math.pi) * 0.5 + 0.5
        for cx in range(width):
            # Create a wave pattern across the floor
            wave = math.sin(cx * 0.3 + elapsed * 2.0) * 0.5 + 0.5
            if wave > (1.0 - pulse_phase * e):
                if canvas[floor_y][cx] == " ":
                    canvas[floor_y][cx] = floor_char

    # Bottom status line
    tempo = tempo_word(safe_bpm)
    status = f"  ♫ {safe_bpm:.0f} bpm · {tempo} · {num_dancers} dancer{'s' if num_dancers != 1 else ''}"
    if mood != "neutral":
        status += f" · {mood}"

    rows = ["".join(row).rstrip() for row in canvas]
    rows.append(status)
    return "\n".join(rows)


def animate_mood_pixels(pixels: list[list[tuple[int, int, int]]], elapsed: float, bpm: float | None):
    if not pixels or not bpm or bpm <= 0:
        return pixels

    beat_duration = 60.0 / bpm
    if beat_duration <= 0:
        beat_duration = 0.5
    beats = elapsed / beat_duration
    within_beat = beats % 1.0

    intensity = max(0.0, 1.0 - (within_beat * 1.5)) # Fade out over 2/3 of a beat
    import math
    
    cols = len(pixels[0])
    animated = []
    for row in pixels:
        new_row = []
        for x, (r, g, b) in enumerate(row):
            progress = x / float(cols)
            if progress < 0.33:
                boost = intensity * (0.4 + 0.6 * math.sin(elapsed * 2.1))
            elif progress < 0.66:
                boost = intensity * (0.4 + 0.6 * math.sin(elapsed * 3.7 + 1.0))
            else:
                boost = intensity * (0.4 + 0.6 * math.sin(elapsed * 5.3 + 2.0))
            
            nr = int(r + (255 - r) * boost)
            ng = int(g + (255 - g) * boost)
            nb = int(b + (255 - b) * boost)

            # boost can be negative (sin dips below 0), which drives a channel
            # below 0 — clamp BOTH ends to the valid 0-255 range so the rendered
            # `rgb(...)` style never carries an out-of-range (e.g. -18) value.
            new_row.append((
                max(0, min(255, nr)),
                max(0, min(255, ng)),
                max(0, min(255, nb)),
            ))
        animated.append(new_row)
    return animated

