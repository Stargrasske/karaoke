"""Karaoke lyric lookup + synced terminal renderer.

Timing logic (`active_index`, `LyricTimeline`) is pure and unit-tested without
audio. Rendering uses Rich; playback clock is driven by time.monotonic() from a
user start keypress (MVP). True audio-position sync is a later upgrade.
"""
from __future__ import annotations

import time
import select
import subprocess
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Optional

from .lyrics import Lyrics, fetch_lrclib, parse_enhanced_lrc, parse_lrc
from .identify import SongRef

# Word-highlight pacing. LRCLIB gives per-LINE timestamps only, so a line's
# words are interpolated across its duration. Without an upper bound, an
# instrumental break after a line stretches that interpolation across the whole
# break (issue #21). Songs are calibrated individually (see
# LyricTimeline.line_pace); these bound the result.
DEFAULT_WORD_S = 0.7    # fallback s/word when a song offers no usable sample
MIN_LINE_S = 1.5        # floor: even a one-word line stays visible briefly
MAX_LINE_S = 12.0       # ceiling: no single line holds the highlight longer
_PACE_SAMPLE_MAX_S = 15.0  # gaps longer than this are breaks, not sung lines
# A gap at least this long counts as an instrumental break for `in_gap`.
GAP_MIN_S = 6.0

# Active-line background per detected mood (see sentiment.mood_of). Word highlight
# stays magenta on top; neutral keeps the original blue.
_MOOD_BG = {
    "happy": "on green",
    "sad": "on blue",
    "angry": "on red",
    "tender": "on deep_pink4",
    "neutral": "on blue",
}
# Panel border colour per mood — also the colour used for the beat "flash".
_MOOD_BORDER = {
    "happy": "green",
    "sad": "blue",
    "angry": "red",
    "tender": "magenta",
    "neutral": "cyan",
}


@dataclass
class LyricTimeline:
    """Ordered (time_seconds, text) lines with active-line lookup."""

    lines: list[tuple[float, str]] = field(default_factory=list)
    # Explicit end times per line index (from empty LRC timestamps / learned
    # timings). Overrides the pace-derived cap when present.
    ends: dict[int, float] = field(default_factory=dict)
    # Real per-word start times per line index (Enhanced LRC / captions).
    word_times: dict[int, list[float]] = field(default_factory=dict)
    _pace: Optional[float] = field(default=None, repr=False, compare=False)

    @property
    def times(self) -> list[float]:
        """Return only the lyric timestamps in display order."""
        return [t for t, _ in self.lines]

    def active_index(self, elapsed: float) -> int:
        """Index of the line active at `elapsed` seconds.

        Returns -1 before the first line's timestamp (intro), otherwise the
        index of the latest line whose timestamp is <= elapsed.
        """
        idx = -1
        for i, (t, _) in enumerate(self.lines):
            if t <= elapsed:
                idx = i
            else:
                break
        return idx

    def next_time(self, elapsed: float) -> Optional[float]:
        """Timestamp of the next upcoming line, or None if past the end."""
        for t, _ in self.lines:
            if t > elapsed:
                return t
        return None

    def line_pace(self) -> float:
        """Median seconds-per-word for this song, from its own normal lines.

        Songs differ a lot (measured across the local cache: ~0.55 s/word for
        Tom Waits up to ~1.16 s/word for slower material), so the pacing used
        to bound a line is calibrated per song rather than hard-coded. Lines
        followed by an unusually long gap are excluded, since those gaps are
        exactly the instrumental breaks being corrected for.
        """
        if self._pace is not None:
            return self._pace
        ratios: list[float] = []
        for i in range(len(self.lines) - 1):
            gap = self.lines[i + 1][0] - self.lines[i][0]
            words = len(self.lines[i][1].split())
            if words and 0.0 < gap <= _PACE_SAMPLE_MAX_S:
                ratios.append(gap / words)
        self._pace = median(ratios) if ratios else DEFAULT_WORD_S
        return self._pace

    def line_end(self, index: int, tail: float = 4.0,
                 max_line_s: Optional[float] = None) -> float:
        """Time at which line `index` stops being sung.

        Normally the next line's timestamp, but bounded by how long the line
        could plausibly take at this song's pace. Without that bound, an
        instrumental break after a line stretches its words across the whole
        gap (issue #21).
        """
        start, text = self.lines[index]
        nxt = (
            self.lines[index + 1][0]
            if index + 1 < len(self.lines)
            else start + tail
        )
        # An explicitly recorded end wins over any heuristic.
        explicit = self.ends.get(index)
        if explicit is not None and explicit > start:
            return min(nxt, explicit)
        if max_line_s is not None:
            cap = max_line_s
        else:
            words = len(text.split()) or 1
            cap = min(MAX_LINE_S, max(MIN_LINE_S, words * self.line_pace()))
        return min(nxt, start + cap)

    def in_gap(self, elapsed: float, min_gap: float = GAP_MIN_S) -> bool:
        """True when `elapsed` falls in an instrumental break.

        A break is the stretch between the (capped) end of the active line and
        the start of the next one, when that stretch is at least `min_gap`
        seconds. Ordinary line spacing is excluded so the rest marker does not
        flicker between normal lines.
        """
        a = self.active_index(elapsed)
        if a < 0 or a + 1 >= len(self.lines):
            return False
        nxt = self.lines[a + 1][0]
        end = self.line_end(a)
        if nxt - end < min_gap:
            return False
        return end <= elapsed < nxt

    def gap_progress(self, elapsed: float, min_gap: float = GAP_MIN_S) -> float:
        """Progress (0..1) through the current instrumental break.

        Returns 0.0 when not in a break. Lets the UI show how long is left
        before the next line arrives.
        """
        if not self.in_gap(elapsed, min_gap=min_gap):
            return 0.0
        a = self.active_index(elapsed)
        end = self.line_end(a)
        nxt = self.lines[a + 1][0]
        if nxt <= end:
            return 0.0
        return max(0.0, min(1.0, (elapsed - end) / (nxt - end)))

    def word_index(self, elapsed: float) -> int:
        """Index of the word to highlight on the active line.

        Prefers real per-word timings (Enhanced LRC / YouTube captions) when
        available for the line; otherwise falls back to interpolating across
        the line's capped duration.
        """
        a = self.active_index(elapsed)
        if a < 0:
            return -1
        times = self.word_times.get(a)
        if times:
            return word_index_at(times, elapsed)
        return active_word_index(self.lines[a][1], self.active_fraction(elapsed))

    def active_fraction(self, elapsed: float, tail: float = 4.0,
                        max_line_s: Optional[float] = None) -> float:
        """Progress (0..1) through the currently-active line.

        Used to interpolate a word-level highlight: LRCLIB gives only per-LINE
        timestamps, so we spread the line's on-screen duration across its words.
        Returns 0.0 in the intro (no active line). For the last line (no next
        timestamp) assumes a `tail`-second duration.

        The line's duration is capped (see `line_end`) so a following
        instrumental break does not drag the word highlight across it.
        """
        a = self.active_index(elapsed)
        if a < 0:
            return 0.0
        start = self.lines[a][0]
        end = self.line_end(a, tail=tail, max_line_s=max_line_s)
        if end <= start:
            return 0.0
        return max(0.0, min(1.0, (elapsed - start) / (end - start)))


def timeline_from_lyrics(ly: Lyrics) -> LyricTimeline:
    """Build a renderable timeline from parsed or raw LRC lyrics.

    Raw LRC is parsed with the enhanced parser so explicit line ends and
    Enhanced LRC word timings are carried through when present.
    """
    if ly.synced_raw:
        lines, ends, word_times = parse_enhanced_lrc(ly.synced_raw)
        if lines:
            return LyricTimeline(lines, ends=ends, word_times=word_times)
    if ly.lines:
        return LyricTimeline(list(ly.lines))
    return LyricTimeline([])


def get_synced(
    ref: SongRef,
    *,
    use_cache: bool = True,
    transcribe: bool = False,
    force_transcribe: bool = False,
    lyrics_file: Optional[str] = None,
    stats_mode: Optional[str] = None,
) -> Lyrics:
    """Return synced lyrics, checking caches before going online."""
    from . import localcache

    artist, title, album, duration = ref.artist, ref.title, ref.album, ref.duration
    audio_path = ref.path


    def _log(event: str, ly: Optional[Lyrics] = None) -> None:
        if not stats_mode:
            return
        localcache.log_event(
            stats_mode, event, artist=artist, title=title,
            source=(ly.source if ly else ""),
            has_synced=bool(ly and ly.has_synced),
        )

    # Force path: Whisper only, no cache read, no LRCLIB.
    if force_transcribe and audio_path:
        from .whisper_sync import transcribe_to_lrc
        
        plain_lyrics = None
        if lyrics_file:
            with open(lyrics_file) as f:
                plain_lyrics = f.read()

        # Whisper is here for RHYTHM. Its words on sung audio are unreliable
        # ("up to do" -> "up to doom", plus "🎵" artifacts), so when a known-good
        # source supplied the lyrics we keep those words and take only the
        # timings — the rule upgrade_timings.upgrade_track already follows for
        # captions. Without supplied lyrics, Whisper's transcript is all we have.
        known_good = (plain_lyrics or "").strip()
        if known_good:
            from .lyric_align import align_lyrics_to_lrc
            from .whisper_sync import transcribe_to_words

            whisper_words = transcribe_to_words(audio_path, text=plain_lyrics)
            lrc = align_lyrics_to_lrc(known_good, whisper_words,
                                      total_duration=duration)
            plain_out, source = known_good, "whisper_aligned"
        else:
            lrc = transcribe_to_lrc(audio_path)
            plain_out = "\n".join(t for _, t in parse_lrc(lrc))
            source = "whisper"

        ly = Lyrics(
            plain=plain_out, synced_raw=lrc, source=source, lines=parse_lrc(lrc),
        ) if lrc.strip() else Lyrics()
        if ly.synced_raw or ly.plain:
            try:
                with localcache.connect() as conn:
                    localcache.add_track_and_lyrics(
                        artist, title, ly,
                        album=album, duration=duration, url=ref.url, conn=conn,
                    )
            except Exception:
                pass
        return ly

    # 1. Local SQLite cache first
    if use_cache:
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id:
                cached = localcache.get_lyrics_by_track_id(track_id, conn)
                if cached is not None and (cached.synced_raw or cached.plain):
                    _log("cache_hit", cached)
                    return cached

    if use_cache:
        _log("cache_miss")

    ly = fetch_lrclib(artist, title, album, duration)

    # Whisper fallback
    if not ly.has_synced and transcribe and audio_path:
        try:
            from .whisper_sync import transcribe_to_lrc

            lrc = transcribe_to_lrc(audio_path)
            if lrc.strip():
                ly = Lyrics(
                    plain="\n".join(t for _, t in parse_lrc(lrc)),
                    synced_raw=lrc,
                    source="whisper",
                    lines=parse_lrc(lrc),
                )
        except Exception:
            pass

    if not (ly.synced_raw or ly.plain):
        _log("no_lyrics", ly)

    # Queue a gap whenever the track cannot drive a karaoke session — that means
    # NO TIMINGS, not merely no lyrics. Plain-text-only lyrics used to be cached
    # and silently forgotten: they report as "has lyrics", so nothing ever
    # revisited them, and backfill's Whisper alignment — which exists precisely
    # to time plain text against audio — never saw them.
    if not ly.synced_raw and stats_mode in ("radio", "player"):
        try:
            with localcache.connect() as conn:
                localcache.log_lyric_gap(artist, title, conn)
        except Exception:
            pass
    
    # Write-through to local cache
    if use_cache and (ly.synced_raw or ly.plain):
        try:
            with localcache.connect() as conn:
                localcache.add_track_and_lyrics(artist, title, ly, album=album, duration=duration, url=ref.url, conn=conn)
        except Exception:
            pass

        # Queue derived work (key/BPM, word timings) for the track we just
        # stored. Previously only the TUI did this, so anything discovered by
        # radio/player never got analysed and the backlog grew every session.
        # enqueue_if_needed is best-effort, bounded, and publishes nothing when
        # the track is already complete.
        try:
            from .postprocess_queue import enqueue_if_needed
            enqueue_if_needed(artist, title, ref.url or "")
        except Exception:
            pass

    return ly


def line_nudge_delta(times: list[float], elapsed: float, direction: int) -> float:
    """Clock delta to move the highlighted line by one, in seconds.

    `direction > 0` advances to the NEXT line (lyrics catch up to audio running
    ahead of the display); `direction < 0` steps back one line. Returns the
    amount to ADD to the lyric clock (the accumulated nudge). Returns 0.0 when
    already at an edge (past the last line going forward, or in the intro going
    back). A small epsilon lands the clock just inside the target line so the
    active-line lookup is unambiguous.
    """
    if not times:
        return 0.0
    # Active index: latest i with times[i] <= elapsed, else -1 (intro).
    a = -1
    for i, t in enumerate(times):
        if t <= elapsed:
            a = i
        else:
            break
    if direction > 0:
        j = a + 1
        if j >= len(times):
            return 0.0  # already at/after the last line
        return (times[j] + 0.05) - elapsed
    # backward
    if a <= -1:
        return 0.0  # already in the intro
    j = a - 1
    if j < 0:
        return (times[0] - 0.5) - elapsed  # step back into the intro
    return (times[j] + 0.05) - elapsed


class _KeyReader:
    """Non-blocking single-key reader (cbreak) for live nudge controls.

    cbreak (not raw) keeps ISIG on, so Ctrl-C still raises KeyboardInterrupt.
    Falls back to a no-op when stdin isn't a TTY (piped/tests). Reads one byte
    at a time via select so it never blocks the render loop.
    """

    def __init__(self) -> None:
        self._fd = None
        self._old = None

    def __enter__(self):
        import sys
        if not sys.stdin.isatty():
            return self
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def get(self) -> Optional[str]:
        if self._fd is None:
            return None
        import select
        import sys
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
        return None

    def __exit__(self, *exc) -> None:
        if self._fd is not None and self._old is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


_NUDGE_HINT = "[v]-line [b]+line [0]reset [q]quit"

# Live recognition (songrec) listens ~10s before returning a position, biasing
# the reported offset low so the highlight starts a couple of lines behind the
# audio. Pre-bias the lyric clock forward by this many seconds on mic/radio
# locks so the start is close without needing to tap `b`. Overridable via
# --lead on the CLI; the TUI's radio mode uses the same figure.
DEFAULT_LEAD_S = 12.6


def word_index_at(word_times: list[float], elapsed: float) -> int:
    """Index of the word under the playhead given REAL per-word start times.

    Unlike :func:`active_word_index`, this needs no interpolation: Enhanced LRC
    and YouTube json3 captions carry a start time per word. Clamps to the first
    word before the line starts and to the last word after it ends.
    """
    if not word_times:
        return -1
    idx = 0
    for i, t in enumerate(word_times):
        if t <= elapsed:
            idx = i
        else:
            break
    return idx


def active_word_index(text: str, frac: float) -> int:
    """Index of the word to highlight given progress `frac` (0..1) through a line.

    LRCLIB timestamps are per-line only, so we spread the line's duration evenly
    across its whitespace-split words and pick the one under the playhead.
    Returns -1 for an empty/blank line. Clamps to the last word at frac>=1.
    """
    words = text.split()
    if not words:
        return -1
    f = max(0.0, min(0.999999, frac))
    return min(len(words) - 1, int(f * len(words)))


def _append_lyric_line(body, line: str, *, kind: str, frac: float = 0.0,
                       mood: str = "neutral", word: Optional[int] = None) -> None:
    """Append one lyric line to a Rich Text body.

    kind: 'active' (current line — highlight the current word in purple over a
    mood-tinted background), 'past' (dim) or 'future' (grey). `frac` drives the
    word highlight unless an explicit `word` index is supplied (real per-word
    timings); `mood` (from sentiment.mood_of) picks the active-line
    background. Imports Rich lazily so pure/tested code needn't depend on it.
    """
    if kind == "past":
        body.append("  " + line + "\n", style="dim")
        return
    if kind == "future":
        body.append("  " + line + "\n", style="grey70")
        return
    # active line: word-level purple highlight over a mood-tinted background
    bg = _MOOD_BG.get(mood, "on blue")
    base = f"bold white {bg}"
    wi = active_word_index(line, frac) if word is None else word
    body.append("♪ ", style=base)
    if wi < 0:
        body.append(line + "\n", style=base)
        return
    words = line.split()
    for j, w in enumerate(words):
        if j == wi:
            body.append(w, style="bold white on magenta")
        else:
            body.append(w, style=base)
        body.append(" " if j < len(words) - 1 else "\n", style=base)


def _gap_marker(progress: float, width: int = 24) -> str:
    """Rest indicator shown during an instrumental break.

    A musical note plus a progress bar counting down to the next lyric line,
    so it is obvious the song is playing and nothing is meant to be sung yet.
    """
    filled = max(0, min(width, int(round(progress * width))))
    return "♪ " + "─" * filled + "•" + " " * (width - filled)


def _render_body(body, tl: "LyricTimeline", elapsed: float,
                 *, before: int = 3, after: int = 5,
                 mood: str = "neutral") -> None:
    """Fill a Rich Text `body` with the window around the active line.

    `mood` tints the active line's background (from sentiment.mood_of on the
    active line's text); pass "neutral" to keep the original blue.

    During an instrumental break the active line is dimmed and a rest marker
    is shown, rather than leaving a stale line highlighted as if it were still
    being sung.

    The active line is bold on a mood-tinted background with the current word
    highlighted; it is deliberately NOT widened or drawn as block type. Both
    were tried: figlet was hard to read at the sizes that fit, fullwidth forms
    rendered as tofu in most terminal fonts, and letter spacing threw off the
    reading rhythm. Normal-width bold reads best.
    """
    active = tl.active_index(elapsed)
    frac = tl.active_fraction(elapsed)
    in_gap = tl.in_gap(elapsed)

    lo = max(0, active - before)
    hi = min(len(tl.lines), active + after)
    for i in range(lo, hi):
        line = tl.lines[i][1]
        if i == active:
            if in_gap:
                # Break in progress: the line is finished, so show it as past
                # and mark the rest until the next line.
                _append_lyric_line(body, line, kind="past")
                body.append(_gap_marker(tl.gap_progress(elapsed)) + "\n",
                            style="cyan")
            else:
                _append_lyric_line(body, line, kind="active", frac=frac,
                                   mood=mood, word=tl.word_index(elapsed))
        elif i < active:
            _append_lyric_line(body, line, kind="past")
        else:
            _append_lyric_line(body, line, kind="future")


def _active_mood(tl: "LyricTimeline", elapsed: float) -> str:
    """Mood of the currently-active lyric line (neutral in the intro)."""
    from .sentiment import mood_of
    a = tl.active_index(elapsed)
    if a < 0:
        return "neutral"
    return mood_of(tl.lines[a][1])


def _build_frame(tl: "LyricTimeline", elapsed: float, header: str, *,
                 beat_times=None, footer_extra: str = ""):
    """Assemble the mood-tinted, beat-flashed Rich Panel for one render tick.

    Shared by every player. `beat_times` (sorted list) drives a real on-beat
    border flash in --file mode; when None we fall back to a per-line pulse so
    audio-less modes (Spotify/live) still blink. The border colour follows the
    active line's mood; on a flash tick it brightens.
    """
    from rich.align import Align
    from rich.panel import Panel
    from rich.text import Text
    from .beats import beat_on, line_pulse

    mood = _active_mood(tl, elapsed)
    body = Text()
    _render_body(body, tl, elapsed, mood=mood)

    if beat_times:
        flash = beat_on(beat_times, elapsed)
    else:
        a = tl.active_index(elapsed)
        line_start = tl.lines[a][0] if a >= 0 else None
        flash = line_pulse(line_start, elapsed)

    color = _MOOD_BORDER.get(mood, "cyan")
    border_style = f"bold {color}" if flash else color

    nxt = tl.next_time(elapsed)
    foot = f"{mood}"
    foot += f"  ·  next in {nxt - elapsed:0.1f}s" if nxt else "  ·  (end)"
    if footer_extra:
        foot = f"{footer_extra}  ·  {foot}"
    return Panel(Align.left(body), title=header, subtitle=foot,
                 border_style=border_style)


def render_lines(tl: LyricTimeline, active: int, context: int = 3) -> str:
    """Plain-text window around the active line (used by tests + fallback)."""
    if not tl.lines:
        return "(no synced lyrics)"
    lo = max(0, active - context)
    hi = min(len(tl.lines), active + context + 1)
    out = []
    for i in range(lo, hi):
        prefix = ">> " if i == active else "   "
        out.append(f"{prefix}{tl.lines[i][1]}")
    return "\n".join(out)


def play(tl: LyricTimeline, *, title: str = "", artist: str = "",
         offset: float = 0.0,
         beat_times: Optional[list[float]] = None) -> None:  # pragma: no cover - interactive
    """Render synced lyrics with a Rich Live view, clock from keypress.

    `beat_times` (from beats.detect_beats on a local file) enables a real on-beat
    border flash; without it the border pulses once per lyric line.
    """
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel

    console = Console()
    if not tl.lines:
        console.print("[yellow]No synced lyrics available for this track.[/]")
        return

    header = f"{artist} - {title}".strip(" -")
    console.print(f"[bold cyan]{header}[/]")
    console.input("[dim]Press Enter when the music starts...[/] ")
    start = time.monotonic()

    def frame() -> Panel:
        elapsed = time.monotonic() - start + offset
        return _build_frame(tl, elapsed, header, beat_times=beat_times)

    try:
        with Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                elapsed = time.monotonic() - start + offset
                live.update(frame())
                if tl.next_time(elapsed) is None and \
                        elapsed > tl.times[-1] + 4:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def play_offset_synced(
    tl: LyricTimeline,
    *,
    title: str = "",
    artist: str = "",
    offset: float,
    offset_mono: float,
    extra_latency: float = 0.0,
) -> None:  # pragma: no cover - interactive/live
    """Render synced lyrics anchored to a known track position (mic/room sync).

    `offset` is the position-in-track (seconds) reported by songrec at monotonic
    instant `offset_mono`. We advance a local clock from that anchor, so the
    highlighted line follows the live audio heard through the mic. `extra_latency`
    nudges for recognizer/audio delay (add a bit if lyrics run early).
    """
    from rich.align import Align
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    console = Console()
    if not tl.lines:
        console.print("[yellow]No synced lyrics available for this track.[/]")
        return

    header = f"{artist} - {title}".strip(" -")
    nudge = [float(extra_latency)]  # live per-line adjustment (v/b keys)

    def elapsed_now() -> float:
        return offset + (time.monotonic() - offset_mono) + nudge[0]

    def frame() -> Panel:
        from .beats import line_pulse
        e = elapsed_now()
        mood = _active_mood(tl, e)
        body = Text()
        _render_body(body, tl, e, mood=mood)
        nxt = tl.next_time(e)
        foot = f"{mood}  ·  {e:0.1f}s"
        foot += f"  ·  next in {nxt - e:0.1f}s" if nxt else "  ·  (end)"
        if abs(nudge[0]) > 1e-6:
            foot += f"  ·  nudge {nudge[0]:+.1f}s"
        foot += "  ·  " + _NUDGE_HINT
        a = tl.active_index(e)
        flash = line_pulse(tl.lines[a][0] if a >= 0 else None, e)
        color = _MOOD_BORDER.get(mood, "cyan")
        return Panel(Align.left(body), title=header, subtitle=foot,
                     border_style=f"bold {color}" if flash else color)

    console.print(f"[bold cyan]{header}[/]  [dim](synced to live audio — v/b nudge, q to stop)[/]")
    try:
        with _KeyReader() as keys, \
                Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                k = keys.get()
                if k:
                    if k in ("q", "\x1b"):
                        break
                    elif k == "v":  # v = step lyrics one line BACK
                        nudge[0] += line_nudge_delta(tl.times, elapsed_now(), -1)
                    elif k == "b":  # b = step lyrics one line FORWARD (catch up)
                        nudge[0] += line_nudge_delta(tl.times, elapsed_now(), +1)
                    elif k == "0":
                        nudge[0] = float(extra_latency)
                e = elapsed_now()
                live.update(frame())
                if tl.next_time(e) is None and e > tl.times[-1] + 4:
                    break
                time.sleep(0.05)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def play_radio_synced(
    *,
    mic: bool = True,
    record: bool = False,
    reidentify_interval: float = 30.0,
    extra_latency: float = 0.0,
    listen_timeout: int = 30,
) -> None:  # pragma: no cover - interactive/live
    """Continuously follow live audio (radio/room): identify, sync, re-lock.

    Runs a background thread that re-identifies every `reidentify_interval`s.
    Each result either re-anchors the current song (drift correction) or, if the
    song changed, swaps in new lyrics. Between/over speech it keeps the last song
    and shows a listening hint. Ctrl-C to stop.
    """
    import threading

    from rich.align import Align
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    from .identify import identify_live
    from .player import get_synced, timeline_from_lyrics  # self-import safe
    from . import localcache

    console = Console()

    # Radio session state tracking in database
    source_name = "mic" if mic else "monitor"
    session_id = localcache.start_radio_session(source=source_name, is_recording=record)

    rec_state = {
        "is_recording": False,
        "recording_id": None,
        "started_at": None,
    }

    def _start_recording() -> tuple[bool, str]:
        from . import recorder, identify, sample_audio
        src = identify._default_source(mic=mic) or (sample_audio.monitor_source() if not mic else "")
        try:
            rec_sess = recorder.start(source=src, keep_audio=True, note=f"Radio session #{session_id}")
            rec_state["is_recording"] = True
            rec_state["recording_id"] = rec_sess.recording_id
            rec_state["started_at"] = time.time()
            localcache.update_radio_session_recording(session_id, rec_sess.recording_id, is_recording=True)
            return True, f"Recording #{rec_sess.recording_id} started ({src or 'default'})"
        except Exception as exc:
            return False, f"Cannot start recording: {exc}"

    def _stop_recording() -> str:
        rid = rec_state["recording_id"]
        if rid is not None:
            from . import recorder
            try:
                rec_cnt, tot_cnt = recorder.mark_count(rid)
            except Exception:
                rec_cnt, tot_cnt = 0, 0
            recorder.stop(rid)
            rec_state["is_recording"] = False
            rec_state["recording_id"] = None
            rec_state["started_at"] = None
            localcache.update_radio_session_recording(session_id, None, is_recording=False)
            return f"Recording #{rid} stopped ({rec_cnt}/{tot_cnt} tracks marked)"
        return "Not recording"

    if record:
        ok, msg = _start_recording()
        if not ok:
            console.print(f"[bold yellow]Warning: {msg}[/bold yellow]")

    # Shared state guarded by a lock; updated by the background identifier.
    state = {
        "key": None,        # (artist, title) currently displayed
        "artist": "",
        "title": "",
        "tl": LyricTimeline([]),
        "offset": 0.0,
        "offset_mono": time.monotonic(),
        "status": "listening…",
        "has_lyrics": False,
    }
    lock = threading.Lock()
    stop = threading.Event()
    nudge = [float(extra_latency)]  # live per-line adjustment (v/b keys)

    def apply(ref) -> None:
        """Fold a fresh identification into shared state."""
        if ref is None or not ref.title or ref.offset is None:
            with lock:
                state["status"] = "listening… (no match — speech/ad/quiet?)"
            return
        key = (ref.artist.lower(), ref.title.lower())
        with lock:
            same = key == state["key"]
        if same:
            # Drift correction: re-anchor the clock, keep the timeline.
            with lock:
                state["offset"] = ref.offset
                state["offset_mono"] = ref.offset_mono
                state["status"] = "in sync"
            localcache.log_event("radio", "relock", artist=ref.artist, title=ref.title)
            localcache.record_radio_track(session_id, ref.artist, ref.title, offset_s=ref.offset)
            if rec_state["is_recording"] and rec_state["recording_id"] is not None:
                try:
                    from . import recorder
                    recorder.add_mark(rec_state["recording_id"], ref)
                except Exception:
                    pass
            return
        # New song -> fetch lyrics (slow) OUTSIDE the lock.
        localcache.log_event(
            "radio", "discover", artist=ref.artist, title=ref.title, source="songrec"
        )
        ly = get_synced(ref, stats_mode="radio")
        tl = timeline_from_lyrics(ly)
        localcache.log_event(
            "radio", "play", artist=ref.artist, title=ref.title,
            source=ly.source, has_synced=bool(tl.lines),
        )
        localcache.record_radio_track(
            session_id, ref.artist, ref.title,
            offset_s=ref.offset, has_synced=bool(tl.lines), lyric_source=ly.source
        )
        if rec_state["is_recording"] and rec_state["recording_id"] is not None:
            try:
                from . import recorder
                recorder.add_mark(rec_state["recording_id"], ref)
            except Exception:
                pass
        with lock:
            state["key"] = key
            state["artist"] = ref.artist
            state["title"] = ref.title
            state["tl"] = tl
            state["offset"] = ref.offset
            state["offset_mono"] = ref.offset_mono
            state["has_lyrics"] = bool(tl.lines)
            state["status"] = "in sync" if tl.lines else "no synced lyrics for this track"

    def identifier() -> None:
        # First pass immediately, then every interval.
        while not stop.is_set():
            try:
                ref = identify_live(mic=mic, timeout=listen_timeout)
                apply(ref)
            except Exception as e:  # keep the loop alive on transient errors
                with lock:
                    state["status"] = f"identify error: {e}"
            stop.wait(reidentify_interval)

    def elapsed_now() -> float:
        return state["offset"] + (time.monotonic() - state["offset_mono"]) + nudge[0]

    def frame() -> Panel:
        from .beats import line_pulse
        with lock:
            tl = state["tl"]
            artist, title = state["artist"], state["title"]
            status = state["status"]
            has = state["has_lyrics"]
        header = f"{artist} - {title}".strip(" -") or "listening for music…"
        body = Text()
        mood = "neutral"
        flash = False
        if has and tl.lines:
            e = elapsed_now()
            mood = _active_mood(tl, e)
            _render_body(body, tl, e, mood=mood)
            a = tl.active_index(e)
            flash = line_pulse(tl.lines[a][0] if a >= 0 else None, e)
        else:
            body.append("\n  ♪ …\n\n", style="dim")
        n = nudge[0]
        rec_indicator = ""
        if rec_state["is_recording"] and rec_state["recording_id"] is not None:
            elapsed_s = int(time.time() - (rec_state["started_at"] or time.time()))
            m, s = divmod(elapsed_s, 60)
            rec_indicator = f"  ·  [bold red]● REC #{rec_state['recording_id']} ({m:02d}:{s:02d})[/bold red]"
        else:
            rec_indicator = "  ·  [dim]rec: off (r)[/dim]"
        foot = f"{mood}  ·  {status}{rec_indicator}"
        if abs(n) > 1e-6:
            foot += f"  ·  nudge {n:+.1f}s"
        foot += "  ·  " + _NUDGE_HINT + " · r rec"
        color = _MOOD_BORDER.get(mood, "cyan")
        return Panel(Align.left(body), title=header, subtitle=foot,
                     border_style=f"bold {color}" if flash else color)

    console.print(f"[bold cyan]Radio karaoke[/] (session #{session_id})  [dim](listening — v/b nudge, r rec, q to stop)[/]")
    th = threading.Thread(target=identifier, daemon=True)
    th.start()
    try:
        with _KeyReader() as keys, \
                Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while not stop.is_set():
                k = keys.get()
                if k:
                    if k in ("q", "\x1b"):
                        break
                    elif k in ("r", "o", "R", "O"):
                        if rec_state["is_recording"]:
                            msg = _stop_recording()
                            with lock:
                                state["status"] = msg
                        else:
                            ok, msg = _start_recording()
                            with lock:
                                state["status"] = msg
                    elif k == "v":  # v = step lyrics one line BACK
                        with lock:
                            times = state["tl"].times
                        nudge[0] += line_nudge_delta(times, elapsed_now(), -1)
                    elif k == "b":  # b = step lyrics one line FORWARD (catch up)
                        with lock:
                            times = state["tl"].times
                        nudge[0] += line_nudge_delta(times, elapsed_now(), +1)
                    elif k == "0":  # reset nudge to the CLI-provided baseline
                        nudge[0] = float(extra_latency)
                live.update(frame())
                time.sleep(0.05)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")
    finally:
        stop.set()
        if rec_state["is_recording"]:
            _stop_recording()
        localcache.finish_radio_session(session_id)


def play_spotify_loop(
    *,
    offset: float = 0.0,
    poll_interval: float = 1.0,
    use_cache: bool = True,
) -> None:  # pragma: no cover - interactive/live
    """Continuously follow Spotify playback, one track after another.

    For each track: fetch synced lyrics and sync to the live position. If a track
    has no synced lyrics, print a note and WAIT (polling) for the next song
    instead of exiting. Auto-advances when the track changes. Runs until playback
    stops entirely or Ctrl-C.
    """
    from rich.console import Console

    from .spotify_client import SpotifyClient

    console = Console()
    sp = SpotifyClient()
    handled_id: Optional[str] = None   # track we've already resolved (played or skipped)
    warned_idle = False

    console.print("[dim]Following Spotify — Ctrl-C to stop.[/]")
    try:
        while True:
            try:
                pb = sp.current_playback()
            except Exception as e:  # noqa: BLE001 - keep the loop alive on transient errors
                console.print(f"[red]Spotify error:[/] {e}")
                time.sleep(poll_interval)
                continue

            if pb is None or not pb.title:
                if not warned_idle:
                    console.print("[dim]Nothing playing on Spotify. Waiting for a track…[/]")
                    warned_idle = True
                handled_id = None
                time.sleep(poll_interval)
                continue
            warned_idle = False

            if pb.track_id == handled_id:
                # Already resolved this track (it had no lyrics) — keep waiting.
                time.sleep(poll_interval)
                continue

            handled_id = pb.track_id
            header = f"{pb.artist} - {pb.title}".strip(" -")
            console.print(f"[bold cyan]{header}[/]  [dim](fetching lyrics…)[/]")

            ref = SongRef(
                artist=pb.artist,
                title=pb.title,
                duration=pb.duration_ms / 1000.0 if pb.duration_ms else None,
                source="spotify",
            )
            ly = get_synced(ref, use_cache=use_cache, stats_mode="spotify")
            tl = timeline_from_lyrics(ly)
            from . import localcache
            localcache.log_event(
                "spotify", "play", artist=pb.artist, title=pb.title,
                source=ly.source, has_synced=bool(tl.lines),
            )
            if not tl.lines:
                console.print(
                    f"[yellow]No synced lyrics for {header} "
                    f"(source={ly.source}). Waiting for the next track…[/]")
                continue

            # Sync this track until it changes or playback stops, then loop.
            _sync_one_spotify_track(sp, tl, header, pb, offset, poll_interval, console)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def _sync_one_spotify_track(
    sp: Any,
    tl: LyricTimeline,
    header: str,
    pb0: Any,
    offset: float,
    poll_interval: float,
    console: Any,
) -> None:  # pragma: no cover - interactive/live
    """Render `tl` locked to the live Spotify position for the current track.

    Returns when the track changes or playback stops (so the caller advances).
    KeyboardInterrupt propagates to the caller to exit the whole loop.
    """
    from rich.align import Align
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    track_id = pb0.track_id
    pos0 = pb0.position_s
    mono0 = time.monotonic()
    playing = pb0.is_playing
    last_poll = time.monotonic()

    def elapsed_now() -> float:
        base = pos0 + offset
        # Only advance the local clock while actually playing (paused holds).
        return base + (time.monotonic() - mono0) if playing else base

    def frame() -> Panel:
        e = elapsed_now()
        return _build_frame(tl, e, header, footer_extra=f"{e:0.1f}s")

    with Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
        while True:
            now = time.monotonic()
            if now - last_poll >= poll_interval:
                last_poll = now
                pb = sp.current_playback()
                if pb is None or not pb.title:
                    console.print("\n[dim]playback stopped[/]")
                    return
                if pb.track_id != track_id:
                    return  # track changed — caller resolves the new one
                pos0 = pb.position_s
                mono0 = time.monotonic()
                playing = pb.is_playing
            live.update(frame())
            time.sleep(0.1)
