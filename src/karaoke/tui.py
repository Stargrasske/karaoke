"""Player-aware Textual TUI for the karaoke library.

Watches the desktop over MPRIS and picks a mode automatically:

- Spotify playing -> ``spotify``: sync and control Spotify.
- Any other player (browser tab, VLC) -> ``scan``: sync to its position.
- Microphone (``R``) -> ``radio``: songrec identifies room audio, which is the
  one case MPRIS cannot cover since external audio has no player publishing
  metadata. The playhead is dead-reckoned from songrec's own offset.
- Nothing playing -> ``browse``: drive the library list.

Layout is two equal side columns around the lyrics, so they sit centred. The
left column carries cover art, a per-track read-out and worker stats; the right
carries mood, key/BPM and the sentiment/rhythm visuals. Both give way on a small
terminal — the way to get bigger lyrics is a bigger terminal FONT, which costs
cells, so the chrome has to yield. ``F`` hides it entirely.

``H`` opens the library over the lyrics on its own layer, costing them no space;
picking a song closes it. ``?`` shows a key reference generated from BINDINGS so
it cannot drift. ``A`` queues the playing track for post-processing.

Tracks with no cached lyrics are recorded as gaps for backfill, and looked up
again in the background against LRCLIB — the cache only holds what someone has
already fetched.

See ``docs/tui.md``.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
try:
    from tqdm import tqdm

    tqdm.set_lock(threading.RLock())
except Exception:
    pass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Select, Static)

from . import (detect, localcache, playerctl, recorder, sample_audio,
               staging, track_analysis, visuals)
from .api_client import ApiClient
from .browse import open_song_url
from .dj_chat import DJChatScreen
from .player_open import (browser_playback, close_cdp, track_finished,
                          track_idle)
from .logger import LOG_FILE, log, stream_logs
from .musictheory import parse_key
from .player import (DEFAULT_LEAD_S, LyricTimeline, _render_body,
                     timeline_from_lyrics)
from .sentiment import mood_of

SongRow = dict[str, object]
SongMapping = Mapping[str, object]

# How long the player may hold nothing before the queue gives up on it and
# moves on. Long enough that a slow watch-URL load is never mistaken for a
# stall — the watcher itself only ticks every 2s.
IDLE_STALL_S = 10.0

# Mood squares. The art rows are 2 cells wide, but the weather glyphs are not
# all the same width: "☔" and "🔥" are genuinely 2 cells while "☀ ♡ ◇" are 1, so
# without padding those two squares render visibly lopsided under
# `content-align: center`. pad_cells fixes that exactly.
#
# It cannot help East-Asian *ambiguous* glyphs, whose width is terminal- and
# font-dependent rather than defined — see visuals.sentiment_bars.
MOOD_GLYPHS = {
    mood: f"{visuals.pad_cells(glyph, 2)}\n{art}"
    for mood, glyph, art in (
        ("happy", "☀", "╲╱\n╱╲"),
        ("sad", "☔", "░░\n▒▒"),
        ("angry", "🔥", "▓▓\n██"),
        ("tender", "♡", "/\\\n\\/"),
        ("neutral", "◇", "··\n··"),
    )
}

FILTER_OPTIONS = [
    ("Working songs (have lyrics)", "working"),
    ("All songs", "all"),
    ("Staging queue", "staging"),
    ("Spotify tracks", "spotify"),
]

MOOD_FILTER_OPTIONS = [
    ("All Moods (0% - 100%)", "all"),
    ("🔥 High Energy (>75%)", "high_energy"),
    ("⚡ Groovy / Upbeat (40-75%)", "groovy"),
    ("🌙 Chill / Mellow (<40%)", "mellow"),
]


def genre_filter_options() -> list[tuple[str, str]]:
    """Build the genre filter from canonical broad genres and active classifications."""
    from .artist_classifier import BROAD_GENRES
    options = [("All Genres", "all")]
    seen = set()
    # 1. First add the 16 canonical broad genres in a clean, curated order
    for bg in BROAD_GENRES:
        key = bg.casefold()
        seen.add(key)
        options.append((bg.title(), bg))

    # 2. Add any additional subgenres found in track_genre or artist_genres
    try:
        with localcache.connect() as conn:
            cur = conn.execute(
                """
                SELECT DISTINCT genre FROM track_genre
                WHERE genre IS NOT NULL AND length(trim(genre)) > 0
                UNION
                SELECT DISTINCT broad_genre FROM artist_genres
                WHERE broad_genre IS NOT NULL AND length(trim(broad_genre)) > 0
                UNION
                SELECT DISTINCT genre FROM artist_genres
                WHERE genre IS NOT NULL AND length(trim(genre)) > 0
                ORDER BY 1
                """
            )
            for row in cur.fetchall():
                genre = str(list(row.values())[0] if hasattr(row, "values") else row[0] or "").strip()
                key = genre.casefold()
                if not genre or key in seen:
                    continue
                seen.add(key)
                options.append((genre.title(), genre))
    except Exception:
        pass
    return options


INITIAL_BROWSE_LIMIT = 70


SORT_OPTIONS = [
    ("Artist / Title (A-Z)", "artist"),
    ("🌱 Priority: Least Played", "least_played"),
    ("⭐ Most Played", "most_played"),
    ("🔥 Mood / Energy (high→low)", "energy_desc"),
    ("🌙 Energy (low→high)", "energy_asc"),
    ("🥁 BPM (fast→slow)", "bpm_desc"),
    ("🎹 BPM (slow→fast)", "bpm_asc"),
    ("🎵 Key", "key"),
]


def feeling_glyph(energy: float, brightness: float | None = None) -> str:
    """A rough 'overall feeling' read from energy (arousal) and brightness
    (valence proxy): bright+loud reads happy/energetic, dark+loud reads
    aggressive, bright+soft reads tender, dark+soft reads melancholy. It's a
    cheap heuristic, not sentiment analysis, but it separates the kinds of
    tracks at a glance."""
    b = 0.5 if brightness is None else brightness
    if energy >= 0.66:
        return "🔥" if b < 0.5 else "☀"
    if energy >= 0.4:
        return "⚡" if b >= 0.5 else "🌗"
    return "♡" if b >= 0.5 else "🌙"

# Manual mode override cycle. None == auto-detect.
MODE_CYCLE = [None, "browse", "scan"]

# Browser MPRIS position can run slightly AHEAD of audible output (output
# buffering/latency); a positive offset (seconds) is subtracted from the reported
# position to pull the highlight back into sync. Default is 0 (no correction) for
# both modes; tune per song with , / . and save with S, or set the env defaults
# KARAOKE_SYNC_OFFSET / KARAOKE_SYNC_OFFSET_SPOTIFY.
def _default_sync_offset(mode: str = "scan") -> float:
    if mode == "spotify":
        env, fallback = "KARAOKE_SYNC_OFFSET_SPOTIFY", 0.0
    else:
        env, fallback = "KARAOKE_SYNC_OFFSET", 0.0
    try:
        return float(os.environ.get(env, str(fallback)))
    except ValueError:
        return fallback


SYNC_OFFSET_STEP = 0.1

# Below these the chrome gives way to the lyrics. The way to make lyrics bigger
# in a terminal is a bigger terminal font, which costs cells — and measuring the
# real layout at 80x24 showed the fixed panels taking 72% of the screen, so they
# have to yield rather than squeeze the lyrics into a corner.
NARROW_COLS = 110
# Below this the header is compacted to a single line, which costs the figlet
# banner entirely. 32 put the cliff edge right where people actually sit: a
# 148x32 terminal keeps its block title and 148x31 loses it, so a tmux status
# line or a one-row nudge silently changed how the app looks. 28 leaves room
# either side of the common case.
SHORT_ROWS = 28

# Mic (radio) mode. songrec needs a few seconds of audio; re-identifying every
# ~30s corrects drift and catches track changes without hammering the service.
MIC_REIDENTIFY_S = 30.0
MIC_LISTEN_TIMEOUT = 20


def _radio_cli_running() -> bool:
    """True if `karaoke --radio` already holds the mic.

    Two recognisers on one input just take turns failing, so the TUI declines
    rather than fighting the CLI for it.
    """
    try:
        out = subprocess.run(["pgrep", "-af", "karaoke"], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any((" -r" in line or "--radio" in line) and "tui" not in line
               for line in out.splitlines())


def lyric_display_state(lyrics) -> str:
    """What can be shown for a track: "synced", "unsynced" or "none".

    Three states, not two. Treating it as "synced or nothing" is what made the
    TUI announce "No synced lyrics — added to the backfill queue" for a track
    whose words were sitting in the database unsynced, and whose words the
    player was showing on screen at the same moment.
    """
    if lyrics is None:
        return "none"
    if lyrics.has_synced:
        return "synced"
    if (lyrics.plain or "").strip():
        return "unsynced"
    return "none"


def genre_line(row) -> str:
    """The stored genre, with the honesty the label needs.

    The margin decides, and only the margin. Marking every broad label as
    doubtful was tried and was wrong: the four clearest decisions in the
    library are "pop" for Lily Allen, Tove Lo, UPSAHL and Lola Young, which are
    pop songs, and stamping a question mark on them put doubt exactly where the
    classifier was most right. "pop" wins both clearly on real pop and thinly
    on rock it cannot place; only the second is uncertain, and a thin margin is
    already what says so.

    So a narrow win shows its runner-up -- "heavy metal ~punk rock" -- which is
    not a hedge: for sludge that pair describes the track better than either
    label alone, and it is what the classifier actually found.
    """
    if row is None:
        return ""
    from .genre import MIN_MARGIN

    label = row["genre"]
    score = row["score"] or 0.0
    runner = row["runner_up"] or ""
    runner_score = row["runner_up_score"] or 0.0
    if runner and (score - runner_score) < MIN_MARGIN:
        return f"{label} ~{runner}"
    return label


def classification_line(genre_row, tone_row) -> str:
    """Genre qualified by the attitude of its words: "cynical pop".

    The two axes come from different material and neither implies the other --
    an instrumental has a genre and no words, and a track whose audio was never
    embedded can still be read. So each is shown when present, and the compound
    only forms when the tone is clear enough to be worth attaching.
    """
    from .tone import MIN_MARGIN as TONE_MARGIN

    genre = genre_line(genre_row)
    if tone_row is None:
        return genre
    tone_name = tone_row["tone"] or ""
    margin = (tone_row["score"] or 0.0) - (tone_row["runner_up_score"] or 0.0)
    from .tone import SHORT

    short = SHORT.get(tone_name, tone_name.split()[0] if tone_name else "")
    if not short:
        return genre
    if margin < TONE_MARGIN:
        # Too close to call. Qualifying a genre with a coin toss reads as a
        # finding rather than the noise it is.
        return genre
    return f"{short} {genre}".strip() if genre else short


def track_info(*, source: str = "", duration: float | None = None,
               offset: float = 0.0, pending: "list[str] | None" = None,
               lyric_lines: int = 0, error: str = "", genre: str = "",
               sound: str = "") -> str:
    """Compact per-track read-out for under the cover art.

    A label column wide enough for the longest label, ASCII throughout, so the
    values line up whatever the terminal does with symbol glyphs.

    An error replaces the block rather than joining it: when something is
    wrong, that is the thing worth reading. Absent values are simply left out,
    so a track with nothing known renders nothing at all.
    """
    if error:
        return f"! {error}"

    rows: list[tuple[str, str]] = []
    if genre:
        rows.append(("genre", genre))
    if sound:
        rows.append(("sound", sound))
    if source:
        # Say plainly when the words are Whisper's guess rather than a real
        # lyric. Nothing else on screen distinguishes them, and the failure is
        # not always obvious from the text -- though sometimes it is:
        # Neubauten's "Installation N 1" is stored as German and Polish
        # fragments fused without spaces.
        from .librarysearch import is_transcribed

        rows.append(("source",
                     f"{source}  (guessed)" if is_transcribed(source) else source))
    if lyric_lines:
        rows.append(("lines", str(lyric_lines)))
    if duration:
        rows.append(("length", f"{int(duration) // 60}:{int(duration) % 60:02d}"))
    rows.append(("offset", f"{offset:+.1f}s"))
    if pending is not None:
        rows.append(("postproc", ", ".join(pending) if pending else "done"))

    if not rows:
        return ""
    width = max(len(label) for label, _ in rows) + 2
    return "\n".join(f"{label:<{width}s}{value}" for label, value in rows)


def short_source(name: str, width: int = 26) -> str:
    """Shorten a PipeWire source name while keeping both informative ends.

    These run long — "bluez_output.1C_5E_82_70_03_6F.1.monitor" — and the two
    ends are what identify it: the device kind at the front and ".monitor" (as
    opposed to a microphone) at the back. A plain truncation would drop exactly
    the part that says whether the right thing is being recorded.
    """
    name = (name or "").strip()
    if len(name) <= width:
        return name
    tail = ".monitor" if name.endswith(".monitor") else name[-8:]
    head = name[:max(1, width - len(tail) - 1)]
    return f"{head}…{tail}"


def record_panel(*, recording_id: int, elapsed_s: float = 0.0,
                 marks_ok: int = 0, marks_total: int = 0,
                 size_bytes: int = 0, source: str = "",
                 blink: bool = True) -> str:
    """The recording indicator for the sidebar.

    ASCII labels in a fixed column, like track_info, so nothing shifts as the
    numbers change. The dot alternates on each refresh: a still display gives
    no sign that capture is actually alive, and this is the one panel where
    that distinction matters.
    """
    dot = "●" if blink else "○"
    mins, secs = divmod(int(max(0.0, elapsed_s)), 60)
    hours, mins = divmod(mins, 60)
    clock = (f"{hours}:{mins:02d}:{secs:02d}" if hours
             else f"{mins:02d}:{secs:02d}")
    rows = [
        ("marks", f"{marks_ok}/{marks_total}"),
        ("size", f"{size_bytes / 1e6:.0f} MB"),
    ]
    if source:
        rows.append(("src", short_source(source)))
    width = max(len(label) for label, _ in rows) + 2
    body = "\n".join(f"{label:<{width}s}{value}" for label, value in rows)
    return f"{dot} REC {recording_id}  {clock}\n{body}"


def binding_rows(bindings, *, key_display=None) -> list[tuple[str, str]]:
    """Return (key, description) pairs for the help screen, from BINDINGS.

    Generated rather than hand-written so the help can never drift from the
    real key map — the previous cheat-sheet was a hard-coded Static that had
    already fallen out of step with BINDINGS.
    """
    from textual.keys import format_key

    rows: list[tuple[str, str]] = []
    for binding in Binding.make_bindings(bindings):
        if not binding.show or not binding.description:
            continue
        if key_display is not None:
            key = key_display(binding)
        else:
            key = binding.key_display or format_key(binding.key)
        rows.append((key, binding.description))
    return rows


def help_table(rows: list[tuple[str, str]]):
    """Render (key, description) pairs as a two-column Rich grid."""
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold cyan", no_wrap=True)
    table.add_column()
    for key, description in rows:
        table.add_row(key, description)
    return table


HELP_NOTES = (
    "",
    "H opens the library over the lyrics; picking a song closes it again.",
    "↑/↓ move the highlight, enter opens (or whitelists in the staging list).",
)


class HelpScreen(ModalScreen[None]):
    """Keyboard reference, rendered from the app's own BINDINGS."""

    CSS = """
    HelpScreen { align: center middle; }
    #help-dialog {
        width: 64; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog") as dialog:
            dialog.border_title = "Keys"
            dialog.border_subtitle = "esc / ? to close"
            yield Static(help_table(self._rows))
            yield Static("\n".join((*HELP_NOTES, f"logs: {LOG_FILE}")))


def stats_panels(lib, summary, status=None) -> "list[tuple[str, list[tuple[str, str]]]]":
    """(section, rows) pairs for the stats screen.

    Pure: takes already-gathered data and returns labelled strings, so the
    layout is testable without a database, a broker or an event loop.
    """
    from .librarystats import bar

    def pct(n: int, d: int) -> str:
        return f"{100 * n / d:.0f}%" if d else "—"

    library = [
        ("tracks", str(lib.tracks)),
        ("karaoke-ready", f"{lib.synced}  {bar(lib.synced, lib.tracks)} "
                          f"{pct(lib.synced, lib.tracks)}"),
        ("plain only", str(lib.plain_only)),
        ("no lyrics", str(lib.tracks - lib.with_lyrics)),
        ("sources", str(lib.sources)),
        ("staged", str(lib.staged)),
    ]

    pipeline = [
        ("analysed", f"{lib.analysed}  {bar(lib.analysed, lib.tracks)} "
                     f"{pct(lib.analysed, lib.tracks)}"),
        ("backlog", str(lib.unanalysed)),
        ("word timings", str(lib.word_timed)),
    ]
    if status is not None:
        pipeline.append(("workers", f"{status.workers} up"))
        pipeline.append(("queue", str(status.queued)))

    listening = [
        ("plays", str(summary.plays)),
        ("discoveries", str(summary.discoveries)),
        ("cache hits", f"{summary.cache_hits}/"
                       f"{summary.cache_hits + summary.cache_misses}  "
                       f"{pct(summary.cache_hits, summary.cache_hits + summary.cache_misses)}"),
        ("artists", str(summary.distinct_artists)),
    ]

    total_lyrics = sum(n for _, n in lib.lyric_sources) or 1
    # Caption source names run to 35 characters and would push the bars out of
    # line; the distinguishing part is the front.
    sources = [(name[:16], f"{n:>4}  {bar(n, total_lyrics)}")
               for name, n in lib.lyric_sources[:6]]

    total_keys = sum(n for _, n in lib.keys) or 1
    keys = [(name, f"{n:>4}  {bar(n, total_keys)}") for name, n in lib.keys[:6]]

    total_bpm = sum(n for _, n in lib.tempo_bands) or 1
    tempo = [(name, f"{n:>4}  {bar(n, total_bpm)}") for name, n in lib.tempo_bands]

    panels = [
        ("library", library),
        ("pipeline", pipeline),
        ("listening", listening),
        ("lyric sources", sources),
        ("gaps", [(s, str(n)) for s, n in lib.gaps]),
        ("keys", keys),
        ("tempo", tempo),
    ]
    return [(title, rows) for title, rows in panels if rows]


class StatsScreen(ModalScreen[None]):
    """Library, pipeline and listening statistics."""

    CSS = """
    StatsScreen { align: center middle; }
    #stats-dialog {
        width: 92; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center; overflow-y: auto;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("T", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, panels) -> None:
        super().__init__()
        self._panels = panels

    def compose(self) -> ComposeResult:
        from rich.table import Table

        grid = Table.grid(padding=(0, 3))
        grid.add_column()
        grid.add_column()
        cells = []
        for title, rows in self._panels:
            inner = Table.grid(padding=(0, 2))
            inner.add_column(justify="left", style="cyan", no_wrap=True)
            inner.add_column(justify="left")
            inner.add_row(f"[bold]{title}[/bold]", "")
            for label, value in rows:
                inner.add_row(label, value)
            cells.append(inner)
        # Two columns, so seven short panels fit without scrolling.
        for i in range(0, len(cells), 2):
            grid.add_row(cells[i], cells[i + 1] if i + 1 < len(cells) else "")

        with Vertical(id="stats-dialog") as dialog:
            dialog.border_title = "Stats"
            dialog.border_subtitle = "esc to close"
            yield Static(grid)


def _playback_endpoint_problem(url: str, *, timeout: float = 3.0):
    """Why a recorded track cannot be served, in words, or None if it can.

    The control API is a long-lived process, so it serves whatever code it was
    started with. When it predates a route it answers 404 and the browser shows
    "Not Found" -- indistinguishable, from the outside, from the recording
    itself being missing. Naming the difference is the whole point of this.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status == 200:
                return None
            return f"Playback endpoint answered {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ("Control API does not know this route — it is probably "
                    "running older code. Restart: python -m karaoke.ctrl_api")
        return f"Playback endpoint answered {exc.code}"
    except urllib.error.URLError as exc:
        return (f"Control API is not reachable ({exc.reason}). "
                "Start it: python -m karaoke.ctrl_api")
    except Exception as exc:                    # pragma: no cover - defensive
        return f"Could not reach the control API: {exc}"


class RecordingBrowseScreen(ModalScreen[None]):
    """Browse what a recording captured, and play a track back from it.

    Playback goes to the browser window that is already open, because that
    window publishes MPRIS and the rest of the app already reads position from
    there. A recorded track therefore syncs its lyrics through exactly the same
    path a stream does.

    Two levels: sessions, then the tracks inside one. Tracks that cannot be
    played -- pruned audio, or a stretch that turned out to be silence -- are
    listed and marked rather than hidden, because a gap in a session is
    evidence about the session.
    """

    CSS = """
    RecordingBrowseScreen { align: center middle; }
    #rec-dialog {
        width: 100; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center;
    }
    #rec-table { height: auto; max-height: 24; }
    #rec-hint { color: $text-muted; height: 1; }
    """

    BINDINGS = [
        ("escape", "back", "Back"),
        ("B", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, recordings: list[dict]) -> None:
        super().__init__()
        self._recordings = recordings
        self._recording_id: Optional[int] = None
        self._rows: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="rec-dialog") as dialog:
            dialog.border_title = "Recordings"
            dialog.border_subtitle = "enter to open · esc to go back"
            yield DataTable(id="rec-table", cursor_type="row")
            yield Static("", id="rec-hint")

    def on_mount(self) -> None:
        self._show_sessions()

    # -- the two levels ---------------------------------------------------

    def _table(self) -> DataTable:
        return self.query_one("#rec-table", DataTable)

    def _reset(self, *columns: str) -> DataTable:
        table = self._table()
        table.clear(columns=True)
        table.add_columns(*columns)
        return table

    def _show_sessions(self) -> None:
        self._recording_id = None
        table = self._reset("When", "Status", "Tracks", "Audio")
        for rec in self._recordings:
            when = time.strftime("%d %b %H:%M", time.localtime(rec["started_at"]))
            size = rec.get("audio_bytes") or 0
            table.add_row(
                when, rec.get("status", ""), str(rec.get("identified", 0)),
                f"{size / 1_000_000_000:.1f} GB" if size else "gone",
                key=str(rec["recording_id"]))
        self.query_one("#rec-hint", Static).update(
            f"{len(self._recordings)} session(s)")

    def _show_tracks(self, recording_id: int) -> None:
        from . import recording_audio

        self._recording_id = recording_id
        self._rows = recording_audio.browse_rows(recording_id)
        table = self._reset("At", "Artist", "Title", "Length", "")
        for row in self._rows:
            at = time.strftime("%H:%M:%S", time.localtime(row["start_wall"]))
            # Why a row cannot be played matters: "gone" is a retention
            # decision, "silent" is what the capture actually contains.
            if not row["playable"]:
                note = "audio gone"
            elif row["silent"]:
                note = "silent"
            elif not row["confident"]:
                note = "boundary ?"
            else:
                note = ""
            table.add_row(at, row["artist"][:24], row["title"][:34],
                          f"{row['duration_s'] / 60:.1f}m", note,
                          key=str(row["index"]))
        playable = sum(1 for r in self._rows if r["playable"] and not r["silent"])
        self.query_one("#rec-hint", Static).update(
            f"recording {recording_id}: {len(self._rows)} track(s), "
            f"{playable} playable")

    # -- interaction ------------------------------------------------------

    def action_back(self) -> None:
        """esc backs out one level, then closes."""
        if self._recording_id is None:
            self.dismiss(None)
        else:
            self._show_sessions()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        if self._recording_id is None:
            self._show_tracks(int(key))
        else:
            self._play(int(key))

    def _play(self, index: int) -> None:
        from . import recording_audio
        from .player_open import open_song_url

        row = next((r for r in self._rows if r["index"] == index), None)
        if row is None:
            return
        if not row["playable"]:
            self.app.notify("That track's audio has been pruned",
                            severity="warning")
            return
        if self._recording_id is None:
            return

        url = recording_audio.track_url(self._recording_id, index)

        # Checked before navigating. Sending the browser to a URL that answers
        # 404 replaces what is playing with an error page, and the message it
        # shows ("Not Found") says nothing about the cause -- which the first
        # time was a control API still running the code it was started with,
        # two days before these routes existed.
        problem = _playback_endpoint_problem(url)
        if problem:
            self.app.notify(problem, severity="error", timeout=10)
            return

        try:
            open_song_url(url, "recording")
        except Exception:
            log.debug("could not open recorded track", exc_info=True)
            self.app.notify("Could not reach the playback window",
                            severity="error")
            return
        self.app.notify(f"Playing {row['title']} from recording "
                        f"{self._recording_id}")
        self.dismiss(None)


class SavedPlaylistsScreen(ModalScreen[None]):
    """Browse and manage saved YouTube Music playlists.

    Playlists created by queue sync (or imported) are stored in SQLite.
    From here, you can:
    - Enter: Load the playlist into the TUI queue and follow it
    - space / o: Open the playlist in YouTube Music (browser)
    - r: Reconcile with remote YouTube Music to fetch new/deleted tracks
    - d: Delete playlist from local database
    - esc / q / P: Close modal
    """

    CSS = """
    SavedPlaylistsScreen { align: center middle; }
    #playlists-dialog {
        width: 100; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center;
    }
    #playlists-table { height: auto; max-height: 24; }
    #playlists-hint { color: $text-muted; height: 1; }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("L", "dismiss", "Close"),
        ("o", "open_browser", "Open in Browser"),
        ("space", "open_browser", "Open in Browser"),
        ("r", "reconcile", "Reconcile Remote"),
        ("d", "delete_playlist", "Delete"),
    ]

    def __init__(self, playlists: list[dict]) -> None:
        super().__init__()
        self._playlists = list(playlists)

    def compose(self) -> ComposeResult:
        with Vertical(id="playlists-dialog") as dialog:
            dialog.border_title = "Saved Playlists"
            dialog.border_subtitle = "enter: load & follow · space: open in browser · r: reconcile · d: delete · esc: close"
            yield DataTable(id="playlists-table", cursor_type="row")
            yield Static("", id="playlists-hint")

    def on_mount(self) -> None:
        self._populate_table()
        self.run_worker(self._auto_sync_remote_playlists, thread=True)

    def _auto_sync_remote_playlists(self) -> None:
        try:
            from . import ytmusic_playlist
            with localcache.connect() as conn:
                dj_pid, _ = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
                if dj_pid and dj_pid.startswith(("PL", "VL", "RD", "OLAK5")):
                    local_tracks = localcache.get_saved_playlist_tracks(dj_pid, conn=conn)
                    client = ytmusic_playlist.get_ytmusic_client()
                    ytmusic_playlist.reconcile_playlist_with_remote(
                        dj_pid, local_tracks, client=client, conn=conn
                    )
            self.app.call_from_thread(self._refresh_playlists)
        except Exception:
            pass

    def _populate_table(self) -> None:
        table = self.query_one("#playlists-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Name", "Tracks", "Search Query", "Updated")
        for pl in self._playlists:
            when = (
                time.strftime("%d %b %H:%M", time.localtime(pl["updated_at"]))
                if pl.get("updated_at")
                else ""
            )
            name = str(pl.get("name") or pl.get("playlist_id") or "")
            tracks_count = str(pl.get("track_count") or 0)
            query = str(pl.get("search_query") or "")
            table.add_row(name, tracks_count, query, when, key=str(pl["playlist_id"]))

        hint = self.query_one("#playlists-hint", Static)
        if self._playlists:
            hint.update(f"{len(self._playlists)} saved playlist(s)")
        else:
            hint.update("No saved playlists found. Use 'Y' on any queue to create one.")

    def _refresh_playlists(self) -> None:
        with localcache.connect() as conn:
            self._playlists = localcache.get_saved_playlists(100, conn=conn)
        self._populate_table()

    def _selected_playlist(self) -> Optional[dict]:
        table = self.query_one("#playlists-table", DataTable)
        if 0 <= table.cursor_row < len(self._playlists):
            return self._playlists[table.cursor_row]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if not key:
            return
        self._load_playlist(str(key))

    def _load_playlist(self, playlist_id: str) -> None:
        self.app.notify(f"Syncing & opening playlist in YouTube Music…")

        def _bg() -> None:
            try:
                from . import ytmusic_playlist, player_open
                with localcache.connect() as conn:
                    client = ytmusic_playlist.get_ytmusic_client()
                    local_tracks = localcache.get_saved_playlist_tracks(playlist_id, conn=conn)
                    if playlist_id.startswith(("PL", "VL", "RD", "OLAK5")):
                        reconciled_rows, _ = ytmusic_playlist.reconcile_playlist_with_remote(
                            playlist_id, local_tracks, client=client, conn=conn
                        )
                    else:
                        reconciled_rows = local_tracks
                    pl = localcache.find_saved_playlist_by_id(playlist_id, conn=conn)

                tracks_to_load = reconciled_rows or local_tracks
                if not tracks_to_load:
                    self.app.call_from_thread(
                        self.app.notify, f"Playlist {playlist_id} has no tracks", severity="warning"
                    )
                    return

                rows = [
                    {
                        "track_id": t.get("track_id"),
                        "artist": t.get("artist") or "",
                        "title": t.get("title") or "",
                        "url": t.get("url") or (f"https://music.youtube.com/watch?v={t['video_id']}" if t.get("video_id") else ""),
                        "kind": "ytmusic",
                        "key": str(t.get("key") or "—"),
                    }
                    for t in tracks_to_load
                ]
                pl_name = (pl.get("name") if pl else "") or playlist_id
                first_vid = next((r.get("video_id") or localcache.extract_youtube_id(r.get("url", "")) for r in rows if (r.get("video_id") or r.get("url"))), "")

                # Load into player queue so TUI follows
                self.app.call_from_thread(self.app._apply_restored_playlist, rows, playlist_id, pl_name)

                # Open the playlist in YouTube Music so player plays it & TUI follows
                play_url = (
                    f"https://music.youtube.com/watch?v={first_vid}&list={playlist_id}"
                    if first_vid and playlist_id.startswith(("PL", "VL", "RD", "OLAK5"))
                    else (pl.get("url") if pl and pl.get("url") else f"https://music.youtube.com/playlist?list={playlist_id}")
                )
                player_open.open_song_url(play_url, "youtube_music_playlist", prefer_audio=True)
                self.app.call_from_thread(
                    self.app.notify, f"Loaded & opened '{pl_name}' in YouTube Music ({len(rows)} tracks)"
                )
            except Exception as exc:
                log.warning("Could not sync and load playlist %s: %s", playlist_id, exc)
                self.app.call_from_thread(
                    self.app.notify, f"Load failed: {exc}", severity="error"
                )

        self.run_worker(_bg, thread=True)
        self.dismiss(None)

    def action_open_browser(self) -> None:
        pl = self._selected_playlist()
        if not pl:
            return
        self._load_playlist(str(pl["playlist_id"]))

    def action_delete_playlist(self) -> None:
        pl = self._selected_playlist()
        if not pl:
            return
        pid = pl["playlist_id"]
        name = pl.get("name") or pid
        with localcache.connect() as conn:
            localcache.delete_saved_playlist(pid, conn=conn)
        self.app.notify(f"Deleted playlist '{name}'")
        self._refresh_playlists()

    def action_reconcile(self) -> None:
        pl = self._selected_playlist()
        if not pl:
            return
        pid = pl["playlist_id"]
        name = pl.get("name") or pid
        self.app.notify(f"Reconciling '{name}' with YouTube Music…")

        def _bg() -> None:
            try:
                from . import ytmusic_playlist
                with localcache.connect() as conn:
                    local_tracks = localcache.get_saved_playlist_tracks(pid, conn=conn)
                    client = ytmusic_playlist.get_ytmusic_client()
                    reconciled_rows, has_drift = ytmusic_playlist.reconcile_playlist_with_remote(
                        pid, local_tracks, client=client, conn=conn
                    )
                self.app.call_from_thread(self._on_reconciled, pid, name, reconciled_rows, has_drift)
            except Exception as exc:
                self.app.call_from_thread(
                    self.app.notify, f"Reconcile failed: {exc}", severity="error"
                )

        self.run_worker(_bg, thread=True)

    def _on_reconciled(self, pid: str, name: str, rows: list[dict], has_drift: bool) -> None:
        if has_drift:
            self.app.notify(f"Reconciled '{name}': synced {len(rows)} tracks from remote")
            if getattr(self.app, "_active_playlist_id", "") == pid:
                self.app._queue = list(rows)
                self.app._unfiltered_queue = list(rows)
                self.app._ytmusic_video_to_queue_index = self.app._queue_video_map()
                self.app._render_queue()
        else:
            self.app.notify(f"'{name}' is up to date with YouTube Music")
        self._refresh_playlists()


class ConfirmScreen(ModalScreen[bool]):
    """A tiny yes/no modal used for the staging whitelist confirmation."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #dialog {
        width: 60; height: auto; border: thick $accent; padding: 1 2;
        background: $surface;
    }
    #buttons { height: auto; margin-top: 1; align-horizontal: center; }
    Button { margin: 0 1; }
    """

    # Without this the dialog can only be dismissed by clicking a button.
    # _prompt_save_offset raises it unprompted on a track change, so being
    # unable to escape it was a real trap.
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, message: str, *, yes_label: str = "Whitelist",
                 no_label: str = "Cancel") -> None:
        super().__init__()
        self._message = message
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal(id="buttons"):
                yield Button(self._yes_label, variant="success", id="yes")
                yield Button(self._no_label, variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        # dismiss(False), not dismiss(): both call sites test the result for
        # truthiness, so escape must mean an explicit "no".
        self.dismiss(False)


def _plays_of(song: dict) -> int:
    v = song.get("play_count")
    return int(v) if isinstance(v, (int, float)) else 0


def _bpm_of(song: dict) -> float:
    v = song.get("bpm")
    if isinstance(v, (int, float, str)):
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _energy_of(song: dict) -> float:
    e = song.get("energy") if song.get("energy") is not None else song.get("_energy")
    if isinstance(e, (int, float)):
        return float(e)
    b = song.get("bpm")
    if isinstance(b, (int, float, str)):
        try:
            return min(1.0, max(0.2, (float(b) - 60.0) / 100.0))
        except Exception:
            pass
    return 0.5


class KaraokeTui(App):
    """A player-aware Textual shell for the karaoke experience."""

    # Textual's default ("*") focuses the first focusable widget at mount,
    # which with the browse overlay hidden is the Select *inside it* — every
    # keypress would then be swallowed by an invisible dropdown. Focus is
    # granted explicitly when the overlay opens instead.
    AUTO_FOCUS = None

    CSS = """
    Screen { layout: vertical; layers: base overlay; }
    #workspace { height: 1fr; }
    #sidebar { width: 34; padding: 0 1; }
    #main { width: 1fr; padding: 0 1; }
    #visuals { width: 34; border: round magenta; padding: 1; }
    /* Tall enough for the figlet title banner (5 rows + artist + status). */
    /* 9 rather than 11. The banner is a 4-row title over a 2-row artist over
       the status line -- 7 -- and the two spare rows read as the panel being
       loose rather than as breathing room. title_banner drops the artist to
       plain text when a descender pushes either font a row taller, which
       costs one line instead of the whole block. */
    #now-playing { height: 9; border: round green; padding: 0 1; margin-bottom: 1; }
    /* overflow-y is hidden, not auto: a scrollbar appearing mid-song steals a
       column and shears the block glyphs, and #lyrics is a non-focusable
       Static so the scrollbar is unreachable by keyboard anyway. */
    #lyrics { height: 1fr; border: round blue; padding: 1; overflow-y: hidden; }
    #statusbar { height: 1; }
    #mode-label { width: 1fr; }
    #worker-load { width: auto; text-align: right; }
    #mood-label { height: auto; text-align: center; }
    #mood-square {
        height: 8; content-align: center middle; text-style: bold;
        border: heavy white; margin-bottom: 1;
    }
    #search-input { height: 3; margin-bottom: 1; border: round $accent; }
    #mood-slider { height: 1; color: $accent; margin-bottom: 1; }
    #mood-select, #genre-select { margin-bottom: 1; }
    #sort-select { margin-bottom: 1; }
    #transport-bar {
        height: 3;
        margin-bottom: 1;
        layout: horizontal;
    }
    #transport-bar Button {
        min-width: 8;
        height: 3;
        margin-right: 1;
        padding: 0 1;
    }
    .browse-label {
        width: 6;
        margin-left: 2;
        content-align: left middle;
    }
    /* Hidden until there is a list, so the lyrics keep the full pane. */
    #queue { display: none; height: 10; border: round cyan; margin-top: 1; }
    #queue.-on { display: block; }
    /* In the left column now, with the other per-track facts. Auto height
       because it holds two or three lines depending on what is known, and a
       fixed 6 left a gap under the short case. */
    #keybpm { height: auto; border: round green; padding: 0 1; margin-top: 1; }
    #ascii-visual { height: 1fr; border: round yellow; padding: 0 1; }
    /* Recording indicator. Hidden entirely when idle rather than shown empty,
       so the sidebar layout is unchanged until it matters. */
    #record-panel { display: none; height: auto; border: round red;
                    padding: 0 1; margin-top: 1; color: $error; }
    #record-panel.-on { display: block; }
    /* Docked, because nothing stretches any more: the art box and the
       facts under it both follow their content, so without this the
       worker read-out floats halfway up the column. */
    #worker-panel { dock: bottom; height: auto; border: round cyan; padding: 0 1;
                    margin-top: 1; }
    /* Takes the rest of the column so the reserved space is visibly held. */
    /* 1fr: soaks up the slack, so the panels under it sit at the bottom. */
    /* Hugs the art rather than filling the column. Since covers are stored and
       drawn at their own aspect, the image is 6 rows for a 16:9 thumbnail and
       11 for a square sleeve -- so a fixed height leaves a gap under one and
       crops the other, and `1fr` left a gap under both.

       padding is 1 row / 2 columns because a terminal cell is about 2.5x
       taller than wide (coverart.CELL_ASPECT): equal numbers would look
       lopsided, and this is what reads as an even margin.

       min-height keeps the box from collapsing before the first render, which
       would make the whole sidebar jump on every track change. */
    /* The frame belongs to the pair, not to the picture: the art and the
       facts under it describe one track. padding is 1 row / 2 columns because
       a terminal cell is about 2.5x taller than wide (coverart.CELL_ASPECT),
       so equal numbers look lopsided and this reads as an even margin.
       min-height stops the box collapsing before the first render, which
       would make the sidebar jump on every track change. */
    #cover-box { height: auto; min-height: 12; padding: 1 2;
                 border: round $surface-lighten-2; }
    /* A floor so the frame has a shape before the first render; the art
       itself decides the height once it arrives. */
    #beat-art { height: auto; min-height: 8; }
    #track-info { height: auto; margin-top: 1; color: $text-muted; }

    /* Browse overlay. On its own layer so showing it never resizes #workspace.
       The offset centres it by arithmetic (Screen is layout: vertical, so
       `align` is unavailable) — if width/height change, offset must too. */
    #browse-overlay {
        layer: overlay; display: none;
        width: 96%; height: 94%; offset: 2% 1%;
        border: round cyan; border-title-align: center;
        background: $surface; padding: 0 1;
    }
    #browse-overlay.-visible { display: block; }

    /* Responsive chrome. A bigger terminal font means fewer cells, so the
       fixed-size panels crowd out the lyrics exactly when they can least
       afford it: at 80x24 the visuals column alone is 42% of the width and
       lyrics got 28% of the screen. These classes are applied on resize. */
    Screen.-narrow #visuals { display: none; }
    Screen.-narrow #sidebar { display: none; }
    Screen.-narrow #transport-bar { display: none; }
    Screen.-short #now-playing { height: 3; padding: 0 1; margin-bottom: 0; }
    Screen.-short #transport-bar { display: none; }
    Screen.-short Header { display: none; }

    /* Focus mode: nothing but the lyrics, at any size. */
    Screen.-focus #visuals { display: none; }
    Screen.-focus #sidebar { display: none; }
    Screen.-focus #now-playing { display: none; }
    Screen.-focus #transport-bar { display: none; }
    Screen.-focus #statusbar { display: none; }
    Screen.-focus Header { display: none; }

    /* Dance mode: visuals pane takes the whole screen for a dance floor. */
    Screen.-dance #sidebar { display: none; }
    Screen.-dance #main { display: none; }
    Screen.-dance Header { display: none; }
    Screen.-dance #statusbar { display: none; }
    Screen.-dance #visuals {
        width: 1fr;
        border: none;
        padding: 0;
    }
    Screen.-dance #mood-square { display: none; }
    Screen.-dance #mood-label { display: none; }
    Screen.-dance #ascii-visual {
        height: 1fr;
        border: round magenta;
        padding: 1 2;
    }

    #browse-head { height: 3; margin-bottom: 0; }
    #browse-head Static { width: auto; min-width: 6; content-align: left middle; padding-right: 1; }
    #filter-select, #mood-select, #genre-select, #sort-select { width: 34; }
    #browse-head Select {
        width: 1fr; min-width: 12; height: 3; max-height: 3;
    }
    #library {
        height: 1fr;
        border: round cyan;
        margin-top: 0;
        display: block;
    }
    #browse-toolbar {
        height: 3;
        margin-top: 0;
        layout: horizontal;
    }
    #browse-toolbar Button {
        min-width: 10;
        height: 3;
        margin-right: 1;
        padding: 0 1;
    }
    #browse-count {
        width: 1fr;
        height: 3;
        content-align: right middle;
        color: $text-muted;
        padding-right: 1;
    }
    #log-label {
        width: auto;
        height: 3;
        content-align: right middle;
        color: $text-muted;
        margin-right: 1;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        # ctrl+c stops a running sample rather than quitting, so the reflex
        # that interrupts a long-running thing does what it looks like it
        # does. With nothing sampling it quits, as it always did.
        Binding("ctrl+c", "cancel_sample", "Stop sampling", show=False,
                priority=True),
        ("H", "toggle_browse", "Browse"),
        ("a", "enqueue_selected", "Enqueue track"),
        ("C", "clear_queue", "Clear queue"),
        ("U", "shuffle_queue", "Shuffle queue"),
        ("G", "suggest_queue", "Keep vibe going"),
        ("M", "more_like_this", "More like this"),
        ("Y", "ytmusic_playlist_queue", "YT playlist"),
        ("L", "browse_playlists", "Playlists"),
        ("u", "queue_rollback", "Rollback"),
        ("minus", "mood_down", "Mood-"),
        ("equals_sign", "mood_up", "Mood+"),
        ("A", "approve_postprocess", "Post-process"),
        ("k", "sample_key", "Sample key/BPM"),
        ("slash", "focus_search", "Search"),
        ("greater_than_sign", "queue_next", "Queue next"),
        ("o", "toggle_play_once", "Follow Queue (o)"),
        ("R", "toggle_mic", "Mic/radio"),
        ("F", "toggle_focus", "Focus"),
        ("W", "toggle_dance", "Dance"),
        ("B", "browse_recordings", "Recordings"),
        ("T", "stats", "Stats"),
        ("D", "ai_dj_chat", "AI DJ"),
        ("v", "lyric_vibe", "Lyric Vibe"),
        ("question_mark", "help", "Keys"),
        # NOT priority: a priority app binding shadows every modal's own
        # escape, which stopped Stats and Help from closing. The
        # ordinary binding already fires from inside the search Input --
        # Input does not bind escape -- so the action, not the binding,
        # was what needed fixing.
        Binding("escape", "hide_browse", "Leave search / close browse",
                show=False),
        ("r", "refresh", "Refresh"),
        ("s", "resync", "Resync"),
        ("comma", "sync_earlier", "Lyrics -0.1s"),
        ("full_stop", "sync_later", "Lyrics +0.1s"),
        ("S", "save_offset", "Save offset"),
        ("m", "cycle_mode", "Mode"),
        ("enter", "select", "Open/Whitelist"),
        ("space", "play_pause", "Play/Pause"),
        ("n", "next_track", "Next"),
        ("p", "previous_track", "Prev"),
        ("V", "toggle_av", "Audio/Video"),
        ("Z", "cycle_sort", "Sort mode"),
        ("left_square_bracket", "seek_back", "Seek -5s"),
        ("right_square_bracket", "seek_fwd", "Seek +5s"),
        ("l", "cycle_log", "Log level"),
    ]

    def __init__(self, *, log_level: str = "err") -> None:
        super().__init__()
        # All player control, playback, sampling and recording actions route
        # through the API client so the TUI drives the exact same backend a
        # future web UI will. It falls back to in-process calls when no API
        # server is running, so single-process `karaoke-tui` still works.
        self.api = ApiClient()
        self._song_data: list[SongRow] = []
        self._filter = "working"
        self._mode_override: str | None = None
        self._det = detect.Detection(mode="browse")
        self._timeline = LyricTimeline([])
        self._sync_key: tuple[str, str] | None = None
        self._sync_mood = "neutral"
        self._gap_logged: set[tuple[str, str]] = set()
        self._elapsed = 0.0
        self._sync_offset = _default_sync_offset()
        self._current_track_id: int | None = None
        self._offset_dirty = False
        self._log_level = log_level
        self._autoloaded: set[str] = set()
        self._postprocess_enqueued: set[tuple[str, str]] = set()
        # Tracks already offered to the auto-classifier this session. It only
        # ever adds what is missing, so a second attempt on the same track is
        # pure cost.
        self._classified: set[int] = set()
        self._cpu_sample: tuple | None = None
        self._current_song: tuple[str, str, str] | None = None
        self._lyrics_fetched: set[tuple[str, str]] = set()
        self._mic_ref = None                       # last songrec identification
        self._offset_mode = ""     # clock the current offset was tuned against
        self._clock_from_mic = False   # mic named the song, a player times it
        # Set once Spotify is unusable (bad auth or an exhausted quota) so a
        # single failure cannot become a lookup storm across a listening session.
        self._spotify_off = False
        self._sampling = False     # a capture is running (real time)
        # Set to stop that capture early: ctrl+c, or the app exiting. The
        # worker polls it, terminates ffmpeg and deletes the partial file.
        self._cancel_sample = False
        self._last_event_ts: float | None = time.time()
        self._recording_id: int | None = None
        self._record_tick = 0
        self._record_marks: tuple[int, int] | None = None
        self._track_duration: float | None = None  # wraps the radio playhead
        self._mic_stop: threading.Event | None = None
        self._last_error = ""      # surfaced in the track-info read-out
        self._mood_pixels = None   # raw pixels for animation
        self._mood_art = None      # rendered picture for the current mood
        self._mood_source = ""     # 'cover' or 'generated'
        self._mood_shown = ""      # mood the picture was rendered for
        self._queue: list = []     # search results, in play order
        self._queue_at = -1        # index currently playing
        self._play_once = True     # queue decides the next track
        self._last_finished_url = ""
        self._idle_since = 0.0     # when the player last held nothing
        self._mood_filter = "all"
        self._genre_filter = "all"
        self._mood_level = 0.0  # slider floor: show tracks with energy >= this
        self._sort = "energy_desc"
        self._queue_is_wildcard = False
        self._ytmusic_queue_follow = False
        self._ytmusic_video_to_queue_index: dict[str, int] = {}
        self._active_playlist_id = ""
        self._active_playlist_name = ""
        self._last_notified_playlist_id = ""
        self._attempted_external_playlists: set[str] = set()
        self._dance_mode = False
        self._dance_num_dancers = 4

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            # Left column balances the right one so the lyrics sit centred
            # rather than pushed against the screen edge.
            with Vertical(id="sidebar"):
                # Art first (it takes the slack), then per-track facts, then
                # the global worker read-out pinned at the bottom.
                # Art and the facts about the same track share one frame.
                # They describe one thing, and three separate boxes down a
                # narrow column read as three subjects rather than one.
                with Vertical(id="cover-box"):
                    yield Static("", id="beat-art")
                    yield Static("", id="track-info")
                # Search sits under that, where the eye already is when
                # looking for "what else is like this".
                # The placeholder carries the way out. There is nothing else
                # focusable on this screen, so a reader who does not know
                # about escape has no affordance to discover.
                yield Input(placeholder="search  (/ · esc to leave)",
                            id="search-input")
                # Mood slider + presets sit directly under search so they
                # narrow both the library list and the search results. The
                # slider ([ / ] adjust) is the fine control; the select is
                # quick presets.
                yield Static("", id="mood-slider")
                yield Select(genre_filter_options(), value="all", id="genre-select",
                             allow_blank=False)
                yield Select(MOOD_FILTER_OPTIONS, value="all", id="mood-select",
                             allow_blank=False)
                yield Select(SORT_OPTIONS, value="energy_desc", id="sort-select",
                             allow_blank=False)
                # Key and tempo sit with the rest of the facts about this
                # track rather than in the visuals column, which is for the
                # things that move.
                yield Static("key: —\nbpm: —", id="keybpm")
                yield Static("workers  —", id="worker-panel")
                # Empty and hidden until recording, so it costs no space.
                yield Static("", id="record-panel")
            with Vertical(id="main"):
                yield Static("Detecting player…", id="now-playing")
                with Horizontal(id="transport-bar"):
                    yield Button("⏮ Prev", id="btn-prev", variant="default")
                    yield Button("⏯ Play/Pause", id="btn-play-pause", variant="primary")
                    yield Button("⏭ Next", id="btn-next", variant="default")
                    yield Button("🎵 Audio/Video", id="btn-av", variant="default")
                    yield Button("🔀 Shuffle", id="btn-shuffle", variant="default")
                    yield Button("🔁 Repeat", id="btn-repeat", variant="default")
                yield Static("Lyrics will render here.", id="lyrics")
                yield DataTable(id="queue", cursor_type="row")
                with Horizontal(id="statusbar"):
                    yield Static("Mode: auto", id="mode-label")
                    yield Static("worker-load: —", id="worker-load")
            with Vertical(id="visuals"):
                yield Static("", id="mood-label")
                yield Static(MOOD_GLYPHS["neutral"], id="mood-square")
                yield Static("sentiment / rhythm", id="ascii-visual")
        # Floats on its own layer above #workspace, so revealing it costs the
        # lyrics no space and does not reflow them.
        with Container(id="browse-overlay") as overlay:
            overlay.border_title = "Library"
            overlay.border_subtitle = "Enter open · a add to queue · M more like this · H close"
            with Horizontal(id="browse-head"):
                yield Static("Filter")
                yield Select(FILTER_OPTIONS, value="working", id="filter-select",
                             allow_blank=False)
                yield Static("Genre", classes="browse-label")
                yield Select(genre_filter_options(), value="all", id="browse-genre-select",
                             allow_blank=False)
                yield Static("Sort", classes="browse-label")
                yield Select(SORT_OPTIONS, value="energy_desc", id="browse-sort-select",
                             allow_blank=False)
            yield DataTable(id="library", cursor_type="row")
            with Horizontal(id="browse-toolbar"):
                yield Button("▶ Open (Enter)", id="btn-browse-open", variant="primary")
                yield Button("➕ Queue (a)", id="btn-browse-enqueue", variant="default")
                yield Button("✨ Similar (M)", id="btn-browse-more", variant="default")
                yield Button("🎤 Lyric Vibe (v)", id="btn-browse-vibe", variant="default")
                yield Static(f"log: {self._log_level}", id="log-label")
                yield Static("", id="browse-count")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#library", DataTable)
        table.add_columns("Artist", "Title", "Key", "BPM", "Energy", "Genre/Feel", "▶", "Src", "♪")
        self.load_songs()
        self._render_mood_slider()
        self._show_selected_song()
        # On startup/reload: restore the latest playlist & sync with player or last played song
        self.run_worker(self._restore_or_sync_latest_playlist, thread=True)
        self.set_interval(1.5, self._poll_detection)
        self.set_interval(0.2, self._tick_lyrics)
        self.set_interval(3.0, self._refresh_worker_load)
        self.set_interval(1.0, self._refresh_record_status)
        self.set_interval(2.5, self._poll_events)
        # 2s: fast enough to catch the end before the site starts
        # its own next track, slow enough not to spam CDP.
        self.set_interval(2.0, self._watch_queue)
        self.apply_size_classes(self.size.width, self.size.height)
        # A row still marked 'recording' with nothing running is a crash, not a
        # live capture. Close it out so the listing does not lie and its audio
        # becomes analysable.
        try:
            rec_status = self.api.record_status()
            active = rec_status.get("recording", [])
            if active:
                self._recording_id = active[0]["recording_id"]
            else:
                recorder.reconcile_stale()
            self._warn_unanalysed_recordings()
            # Audio is kept after analysis now, so something has to bound it.
            from . import recording_worker
            for note in recording_worker.prune_recordings():
                log.info("recording retention:%s", note)
        except Exception:
            log.debug("reconciling stale recordings failed", exc_info=True)
        self._poll_detection()
        self._refresh_worker_load()

    def _restore_or_sync_latest_playlist(self) -> None:
        """Restore the latest playlist on reload, sync with player or last played song."""
        try:
            saved = localcache.load_active_queue()
            rows: list[dict[str, Any]] = []
            playlist_id: str = ""
            playlist_name: str = ""

            if saved and saved.get("rows"):
                rows = saved["rows"]
                playlist_id = saved.get("playlist_id") or ""
                playlist_name = saved.get("name") or ""
            else:
                from . import ytmusic_playlist
                latest_pl = ytmusic_playlist.get_latest_temp_queue_playlist()
                if latest_pl and latest_pl.get("rows"):
                    rows = latest_pl["rows"]
                    playlist_id = latest_pl.get("playlist_id") or ""
                    playlist_name = latest_pl.get("name") or ""
                    localcache.save_active_queue(playlist_id, playlist_name, rows)

            if not rows:
                return

            self.call_from_thread(self._apply_restored_playlist, rows, playlist_id, playlist_name)
        except Exception as exc:
            log.debug("Failed restoring latest playlist on mount: %s", exc)

    def _apply_restored_playlist(
        self,
        rows: list[dict[str, Any]],
        playlist_id: str = "",
        playlist_name: str = "",
    ) -> None:
        self._queue = list(rows)
        self._unfiltered_queue = list(rows)
        self._active_playlist_id = playlist_id
        self._active_playlist_name = playlist_name
        self._ytmusic_video_to_queue_index = self._queue_video_map()
        self._ytmusic_queue_follow = True
        self._play_once = False

        target_index: int = 0
        from . import playerctl
        is_playing = False
        try:
            active = playerctl.playing_player()
            if active and (playerctl.status(active) or "").strip() == "Playing":
                is_playing = True
        except Exception:
            pass

        if is_playing and self._det.is_active:
            vid = localcache.extract_youtube_id(getattr(self._det, "url", "") or "")
            if vid and vid in self._ytmusic_video_to_queue_index:
                target_index = self._ytmusic_video_to_queue_index[vid]
            else:
                det_title = (getattr(self._det, "title", "") or "").strip().casefold()
                for i, r in enumerate(self._queue):
                    if (r.get("title") or "").strip().casefold() == det_title:
                        target_index = i
                        break
            self._queue_at = target_index
            self._render_queue()
            self.notify(f"Synced queue with active playback ({target_index + 1}/{len(rows)})", severity="information")
        else:
            last_played = localcache.get_last_played_track()
            if last_played:
                lp_title = last_played["title"].strip().casefold()
                lp_artist = last_played["artist"].strip().casefold()
                for i, r in enumerate(self._queue):
                    r_title = (r.get("title") or "").strip().casefold()
                    r_artist = (r.get("artist") or "").strip().casefold()
                    if r_title == lp_title and (not lp_artist or not r_artist or lp_artist == r_artist):
                        target_index = i
                        break
            self._queue_at = target_index
            self._render_queue()
            label = (
                f"Resumed queue at #{target_index + 1}: {self._queue[target_index]['artist']} - {self._queue[target_index]['title']}"
                if self._queue
                else "Loaded latest playlist"
            )
            self.notify(label, severity="information")

    # -- library ----------------------------------------------------------
    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "filter-select":
            self._filter = str(event.value)
            self._apply_mood_change()
        elif event.select.id == "mood-select":
            self._mood_filter = str(event.value)
            if self._mood_filter == "high_energy":
                self._mood_level = 0.75
            elif self._mood_filter == "groovy":
                self._mood_level = 0.40
            elif self._mood_filter in ("mellow", "all"):
                self._mood_level = 0.0
            self._apply_mood_change()
        elif event.select.id in ("genre-select", "browse-genre-select"):
            self._genre_filter = str(event.value)
            for sel_id in ("#genre-select", "#browse-genre-select"):
                try:
                    sel = self.query_one(sel_id, Select)
                    if sel.value != self._genre_filter:
                        sel.value = self._genre_filter
                except Exception:
                    pass
            self._apply_mood_change()
        elif event.select.id in ("sort-select", "browse-sort-select"):
            self._sort = str(event.value)
            for sel_id in ("#sort-select", "#browse-sort-select"):
                try:
                    sel = self.query_one(sel_id, Select)
                    if sel.value != self._sort:
                        sel.value = self._sort
                except Exception:
                    pass
            self._apply_mood_change()

    def _render_mood_slider(self) -> None:
        """Draw the energy-floor slider bar in the sidebar."""
        try:
            widget = self.query_one("#mood-slider", Static)
        except Exception:
            return
        pct = int(self._mood_level * 100)
        filled = int(round(pct / 10))
        bar = "█" * filled + "░" * (10 - filled)
        widget.update(f"energy ≥ {pct:>3}% [{bar}]  -/+")

    def action_mood_down(self) -> None:
        self._mood_level = max(0.0, round(self._mood_level - 0.1, 2))
        self._apply_mood_change()

    def action_mood_up(self) -> None:
        self._mood_level = min(1.0, round(self._mood_level + 0.1, 2))
        self._apply_mood_change()

    def _apply_mood_change(self) -> None:
        """Apply mood/energy filtering without letting a busy DB kill the TUI."""
        self._render_mood_slider()
        try:
            self.load_songs()
            self._show_selected_song()
            if getattr(self, "_last_search_query", ""):
                self.run_worker(
                    lambda q=self._last_search_query: self._background_search(q, start_playing=False),
                    exclusive=False,
                    thread=True,
                )
            elif hasattr(self, "_unfiltered_queue") and self._unfiltered_queue:
                self._filter_and_set_queue()
        except Exception as exc:
            log.warning("mood/filter refresh deferred: %s", exc)
            self.notify("Library is busy importing; filter refresh will retry", severity="warning")


    def load_songs(self) -> None:
        try:
            table = self.query_one("#library", DataTable)
        except Exception:
            return
        table.clear()
        self._song_data.clear()
        with localcache.connect() as conn:
            from .track_analysis import ensure_schema
            ensure_schema(conn)
            if self._filter == "staging":
                self._load_staging(conn)
            elif self._filter == "spotify":
                self._load_spotify(conn)
            else:
                self._load_tracks(conn, only_working=self._filter == "working")

        # Compute an energy value per song once, then filter, sort, render.
        def _energy_of(song: dict) -> float:
            energy_val = song.get("energy")
            bpm_val = song.get("bpm")
            if isinstance(energy_val, (int, float, str)):
                try:
                    return float(energy_val)
                except (ValueError, TypeError):
                    pass
            if isinstance(bpm_val, (int, float, str)):
                try:
                    return min(1.0, max(0.2, (float(bpm_val) - 60.0) / 100.0))
                except (ValueError, TypeError):
                    pass
            return 0.5

        filtered = []
        for song in self._song_data:
            energy = _energy_of(song)
            song["_energy"] = energy
            if energy < self._mood_level:
                continue
            if self._mood_filter == "high_energy" and energy < 0.75:
                continue
            if self._mood_filter == "groovy" and not (0.40 <= energy <= 0.75):
                continue
            if self._mood_filter == "mellow" and energy > 0.40:
                continue
            if not self._genre_matches(song):
                continue
            filtered.append(song)

        filtered = self._sort_rows(filtered)

        for song in filtered:
            energy = song["_energy"]
            bpm_val = song.get("bpm")
            e_bar = "🔥" if energy >= 0.75 else "⚡" if energy >= 0.40 else "🌙"
            e_str = f"{e_bar} {int(energy * 100)}%"

            bpm_display = "—"
            if isinstance(bpm_val, (int, float, str)):
                try:
                    bpm_display = f"{float(bpm_val):.0f}"
                except (ValueError, TypeError):
                    bpm_display = "—"

            bright = song.get("brightness")
            bright_f = float(bright) if isinstance(bright, (int, float)) else None
            genre = str(song.get("genre") or "").strip()
            feel = feeling_glyph(energy, bright_f)
            genre_feel = f"{genre[:10]} {feel}" if genre else feel

            artist_str = str(song.get("artist") or "")
            title_str = str(song.get("title") or "")
            table.add_row(
                artist_str[:15],
                title_str[:30],
                str(song.get("key") or "—"),
                bpm_display,
                e_str,
                genre_feel,
                str(_plays_of(song)),
                str(song.get("kind") or "—"),
                "♪" if song.get("synced_lyrics") else (
                    "·" if song.get("plain_lyrics") else " "
                ),
            )
        self._song_data = filtered
        self._update_browse_count()

    def _load_spotify(self, conn, *, limit: int | None = None) -> None:
        """Tracks that have a Spotify source, joined to that source.

        _load_tracks deliberately ranks spotify *below* every browser-openable
        kind so Enter opens the browser, which leaves Spotify-only tracks
        unreachable from the library. This lists them against their Spotify
        source instead, so the row's url/kind are the Spotify ones and
        _open_selected needs no special case — open_song_url already rewrites
        them to the web player and navigates the existing Chrome window.
        """
        cur = conn.cursor()
        sort_mode = getattr(self, "_sort", "artist")
        if sort_mode == "most_played":
            order_sql = "t.play_count DESC, lower(t.artist), lower(t.title)"
        elif sort_mode == "least_played":
            order_sql = "t.play_count ASC, lower(t.artist), lower(t.title)"
        else:
            order_sql = "lower(t.artist), lower(t.title)"

        limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
        cur.execute(
            f"""
            SELECT t.track_id, t.artist, t.title,
                   s.url AS url, s.kind AS kind,
                   COALESCE(l.source, '') AS lyric_source,
                   COALESCE(l.synced_lyrics, '') AS synced_lyrics,
                   COALESCE(l.plain_lyrics, '') AS plain_lyrics
            FROM tracks t
            JOIN sources s
              ON s.track_id = t.track_id AND s.kind = 'spotify'
            LEFT JOIN lyrics l
              ON t.track_id = l.track_id AND l.kind = 'approved'
            
            ORDER BY {order_sql}
            {limit_sql}
            """,
        )
        for row in cur.fetchall():
            self._song_data.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "url": row["url"],
                "kind": row["kind"],
                "lyric_source": row["lyric_source"],
                "synced_lyrics": row["synced_lyrics"],
                "plain_lyrics": row["plain_lyrics"],
            })

    def _load_tracks(self, conn, *, only_working: bool, limit: int | None = None) -> None:
        cur = conn.cursor()
        try:
            from .track_analysis import ensure_schema
            ensure_schema(conn)
        except Exception:
            pass
        has_artist_genres = localcache.table_exists(conn, "artist_genres")
        artist_genres_map = localcache.get_all_artist_genres_map(conn) if has_artist_genres else {}

        where_clauses = []
        params = {}
        if limit is not None:
            params["browse_limit"] = limit
        genre_filter = str(getattr(self, "_genre_filter", "all") or "all").strip()
        if genre_filter != "all":
            params["genre_filter"] = genre_filter
            if has_artist_genres:
                where_clauses.append(
                    """(
                        EXISTS (
                            SELECT 1 FROM artist_genres ag
                            WHERE (lower(trim(ag.broad_genre)) = lower(trim(%(genre_filter)s))
                                   OR lower(trim(ag.genre)) = lower(trim(%(genre_filter)s)))
                              AND ag.artist_normalized = lower(trim(t.artist))
                        )
                        OR (
                            NOT EXISTS (
                                SELECT 1 FROM artist_genres ag
                                WHERE ag.artist_normalized = lower(trim(t.artist))
                            )
                            AND lower(trim(COALESCE(g.genre, ''))) = lower(trim(%(genre_filter)s))
                        )
                    )"""
                )
            else:
                where_clauses.append("lower(trim(COALESCE(g.genre, ''))) = lower(trim(%(genre_filter)s))")
        if only_working:
            where_clauses.append("(COALESCE(l.synced_lyrics, '') != '' OR COALESCE(l.plain_lyrics, '') != '')")
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        sort_mode = getattr(self, "_sort", "artist")
        if sort_mode == "most_played":
            order_sql = "t.play_count DESC, lower(t.artist), lower(t.title)"
        elif sort_mode == "least_played":
            order_sql = "t.play_count ASC, lower(t.artist), lower(t.title)"
        elif sort_mode == "energy_desc":
            order_sql = "a.energy DESC NULLS LAST, a.bpm DESC NULLS LAST, lower(t.artist), lower(t.title)"
        elif sort_mode == "energy_asc":
            order_sql = "a.energy ASC NULLS LAST, a.bpm ASC NULLS LAST, lower(t.artist), lower(t.title)"
        elif sort_mode == "bpm_desc":
            order_sql = "a.bpm DESC NULLS LAST, lower(t.artist), lower(t.title)"
        elif sort_mode == "bpm_asc":
            order_sql = "a.bpm ASC NULLS LAST, lower(t.artist), lower(t.title)"
        elif sort_mode == "key":
            order_sql = "a.detected_key ASC NULLS LAST, lower(t.artist), lower(t.title)"
        else:
            order_sql = "lower(t.artist), lower(t.title)"

        limit_sql = "LIMIT %(browse_limit)s" if limit is not None else ""

        # Prefer a browser-openable source (youtube/http) over spotify so Enter
        # opens in the browser. Deterministic per track (see browse.py).
        cur.execute(
            f"""
            SELECT t.track_id, t.artist, t.title, t.play_count,
                   COALESCE(s.url, '') AS url,
                   COALESCE(s.kind, '') AS kind,
                   COALESCE(l.source, '') AS lyric_source,
                   COALESCE(l.synced_lyrics, '') AS synced_lyrics,
                   COALESCE(l.plain_lyrics, '') AS plain_lyrics,
                   a.detected_key AS key, a.bpm, a.energy, a.brightness,
                   g.genre AS genre, g.runner_up AS runner_up
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
            LEFT JOIN lyrics l
              ON t.track_id = l.track_id AND l.kind = 'approved'
            LEFT JOIN track_analysis a
              ON a.track_id = t.track_id
            LEFT JOIN track_genre g
              ON g.track_id = t.track_id
            {where_sql}
            
            ORDER BY {order_sql}
            {limit_sql}
            """,
            params,
        )
        for row in cur.fetchall():
            has_lyrics = bool(row["synced_lyrics"] or row["plain_lyrics"])
            if only_working and not has_lyrics:
                continue
            norm_art = str(row["artist"] or "").strip().casefold()
            a_info = artist_genres_map.get(norm_art, {})
            self._song_data.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "url": row["url"],
                "kind": row["kind"],
                "key": row["key"],
                "bpm": row["bpm"],
                "energy": row["energy"],
                "brightness": row["brightness"],
                "genre": row["genre"],
                "runner_up": row["runner_up"] if "runner_up" in row.keys() else "",
                "broad_genres": a_info.get("broad", []),
                "artist_genres": a_info.get("specific", []),
                "play_count": row["play_count"],
                "lyric_source": row["lyric_source"],
                "synced_lyrics": row["synced_lyrics"],
                "plain_lyrics": row["plain_lyrics"],
            })

    def _load_staging(self, conn) -> None:
        for item in staging.list_staged(status="all", limit=200, conn=conn):
            self._song_data.append({
                "staged_id": item.id,
                "status": item.status,
                "artist": item.artist,
                "title": item.title,
                "url": item.source_url,
                "kind": f"{item.source_kind}/{item.status}",
                "lyric_source": item.source_kind,
                "synced_lyrics": item.synced_lyrics,
                "plain_lyrics": item.plain_lyrics,
            })

    # -- selection / previews --------------------------------------------
    def on_data_table_row_highlighted(self, _e: DataTable.RowHighlighted) -> None:
        if getattr(_e.data_table, "id", "") == "library":
            self._show_selected_song()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # One handler for both tables: the queue plays its row directly, the
        # library overlay opens the selected song. (Two methods of the same
        # name would silently shadow each other.)
        table_id = getattr(event.data_table, "id", "")
        if table_id == "queue":
            self.play_queue_index(event.cursor_row)
        else:
            self.action_select()

    # -- browse overlay ---------------------------------------------------
    _BROWSE_OPEN = "-visible"

    def _browse_open(self) -> bool:
        try:
            return self.query_one("#browse-overlay").has_class(self._BROWSE_OPEN)
        except Exception:
            return False

    def _update_browse_count(self) -> None:
        """Update live track count and filter info in the browse toolbar."""
        try:
            lbl = self.query_one("#browse-count", Static)
            count = len(getattr(self, "_song_data", []) or [])
            desc = getattr(self, "_filter", "working")
            genre = str(getattr(self, "_genre_filter", "all") or "all").strip()
            if genre and genre != "all":
                desc = f"{desc} · {genre}"
            lbl.update(f"{count:,} tracks ({desc})")
        except Exception:
            pass

    def _show_browse(self) -> None:
        try:
            self.query_one("#browse-overlay").add_class(self._BROWSE_OPEN)
            lib = self.query_one("#library", DataTable)
            if hasattr(lib, "remove_class"):
                lib.remove_class("-off")
            lib.focus()
        except Exception:
            pass
        self._update_browse_count()

    def _hide_browse(self) -> None:
        try:
            self.query_one("#browse-overlay").remove_class(self._BROWSE_OPEN)
        except Exception:
            pass
        self.set_focus(None)

    def action_toggle_browse(self) -> None:
        self._hide_browse() if self._browse_open() else self._show_browse()

    def search_has_focus(self) -> bool:
        """Whether typing currently goes into the search box.

        ``self.focused`` needs a mounted screen stack, which an app object
        constructed for a unit test does not have. Answering False there is
        right as well as convenient: nothing is focused when nothing is
        running.
        """
        try:
            focused = self.focused
        except Exception:
            return False
        return focused is not None and getattr(focused, "id", "") == "search-input"

    def action_hide_browse(self) -> None:
        """escape: leave whatever has taken over the keyboard.

        The search box first. Nothing else on this screen is focusable, so a
        box with focus had no Tab target and no way out -- every key went into
        it, including the ones bound to actions. Escape is the way out of a
        text field everywhere else, so it is the way out here.
        """
        if self.search_has_focus():
            self.set_focus(None)
            return
        if self._browse_open():
            self._hide_browse()

    def title_banner(self, artist: str, title: str, width: int,
                     height: int = 99) -> str:
        """The song title in figlet block type, or plain text if it will not fit.

        This is where block type earns its keep: a header is one short string
        with space around it, unlike a lyric line that has to stay legible and
        in rhythm with its neighbours.

        Falls back to plain text whenever the banner would not fit — a narrow
        panel, a long title, or a compacted header on a short terminal.
        """
        from . import bigtext

        plain = f"♪ {artist} - {title}"
        # Three different thresholds can drop the banner to plain text and the
        # only visible sign is that the block type is gone. Saying which one
        # turns "the figlet disappeared" into "the terminal is two rows too
        # short", which is a thing the reader can act on.
        if width < bigtext.MIN_WIDTH:
            log.debug("banner plain: panel is %d cols, needs %d",
                      width, bigtext.MIN_WIDTH)
            return plain
        if height < 6:
            log.debug("banner plain: %d rows for the banner, needs 6 "
                      "(terminal under %d rows compacts the header)",
                      height, SHORT_ROWS)
            return plain
        # A long title simply does not fit in the big face: "A Little God in
        # My Hands" wants 100 columns and the panel has 74, which is why the
        # banner kept vanishing on perfectly ordinary songs. The smaller face
        # fits it in 3 rows, so it is tried before giving up on block type
        # altogether -- a smaller title still reads as a title, plain text does
        # not.
        rendered = bigtext.render(title, width, max_rows=1)
        used_small_face = False
        if rendered is None:
            rendered = bigtext.render(title, width, max_rows=1,
                                      font=bigtext.SMALL_FONT)
            used_small_face = rendered is not None
        if rendered is None:
            log.debug("banner plain: %r does not fit %d cols in either face",
                      title, width)
            return plain
        title_rows = list(rendered[0].rows)
        if len(title_rows) + 1 > height:
            return plain

        # Ladder rather than all-or-nothing. Both fonts grow a row for
        # descenders, so a fixed header cannot always hold title-plus-artist in
        # block type -- and dropping the whole banner to plain text over one
        # row of overflow loses far more than dropping the artist to plain does.
        # No byline in block type when the title already had to shrink: two
        # lines in the same small face read as one wrapped title rather than a
        # heading and its artist.
        byline = (None if used_small_face
                  else bigtext.render_line(artist, bigtext.SMALL_FONT))
        if byline is not None and len(title_rows) + len(byline.rows) <= height:
            fits_width = all(len(r) <= width for r in byline.rows)
            if fits_width:
                return "\n".join((*title_rows, *byline.rows))
        return "\n".join((*title_rows, f"  {artist}"))

    # -- responsive chrome ------------------------------------------------
    def apply_size_classes(self, width: int, height: int) -> None:
        """Shrink the surrounding panels when there are few cells to spend.

        The way to get bigger lyrics in a terminal is a bigger terminal font,
        which costs cells — so the fixed-size chrome has to give ground rather
        than squeeze the lyrics into a corner.
        """
        self.screen.set_class(width < NARROW_COLS, "-narrow")
        self.screen.set_class(height < SHORT_ROWS, "-short")

    def on_resize(self, event) -> None:
        self.apply_size_classes(event.size.width, event.size.height)
        # Force the header to be drawn again. It is computed once per track --
        # _poll_detection returns early while the sync key is unchanged -- and
        # the banner is sized from the panel, so widening the window mid-song
        # left the title in the plain text it had chosen when the panel was
        # narrow. It looked exactly like the figlet being broken.
        self._sync_key = None

    def action_toggle_focus(self) -> None:
        """`F`: lyrics only, hiding every other panel."""
        if self._dance_mode:
            self.screen.remove_class("-dance")
            self._dance_mode = False
        on = not self.screen.has_class("-focus")
        self.screen.set_class(on, "-focus")
        self.notify("Focus mode" if on else "Focus mode off")

    def action_toggle_dance(self) -> None:
        """`W`: dance floor mode — visuals pane fills the whole screen."""
        if self.screen.has_class("-focus"):
            self.screen.remove_class("-focus")
        self._dance_mode = not self._dance_mode
        self.screen.set_class(self._dance_mode, "-dance")
        if self._dance_mode:
            self.notify(
                f"Dance mode! {self._dance_num_dancers} dancers · "
                "[ / ] to add/remove dancers · W to exit"
            )
        else:
            self.notify("Dance mode off")

    # -- mic / radio mode -------------------------------------------------
    def mic_elapsed(self, now: float | None = None) -> float | None:
        """Playhead position from the last mic identification, or None.

        songrec reports where in the track it heard us, plus the monotonic
        instant it finished listening. Everything since is dead reckoning off
        that anchor — there is no MPRIS position to read in radio mode.

        DEFAULT_LEAD_S is added for the same reason `karaoke -r` adds it:
        songrec listens for ~10s before returning, so the offset it reports is
        already stale by roughly that much and the highlight would sit a couple
        of lines behind the audio. Both modes use the same figure so they stay
        in step.
        """
        ref = self._mic_ref
        if ref is None or ref.offset is None or ref.offset_mono is None:
            return None
        now = time.monotonic() if now is None else now
        pos = max(0.0, ref.offset + (now - ref.offset_mono) + DEFAULT_LEAD_S)

        # Bound the reckoning to the track. Nothing here observes the audio, so
        # a song left on repeat keeps counting upward past its own end: the
        # lyrics run out, the footer counts down to a track change that never
        # comes, and the next re-identification is the only thing that rescues
        # it. Wrapping is right for repeat and self-correcting otherwise, since
        # a genuine track change re-anchors within MIC_REIDENTIFY_S either way.
        #
        # Guarded on a sane duration: a bad or missing value must not fold a
        # correct playhead back to zero.
        dur = self._track_duration
        if dur and dur > 30.0 and pos >= dur:
            pos %= dur
        return pos

    def _mic_running(self) -> bool:
        """Whether the mic worker is currently listening."""
        return self._mic_stop is not None and not self._mic_stop.is_set()

    def _mic_loop(self) -> None:
        """Identify room audio on a loop until the mic is switched off.

        Each result either re-anchors the current song (correcting drift) or
        swaps in a new one. A failed identification is ignored rather than
        clearing the song: songrec returns nothing over speech and quiet
        passages, and dropping the lyrics every time would be worse than
        holding the last known track.
        """
        from .identify import identify_live

        while not self._mic_stop.is_set():
            try:
                ref = identify_live(mic=True, timeout=MIC_LISTEN_TIMEOUT)
            except Exception as exc:
                log.debug("mic identify failed: %s", exc)
                ref = None
            if ref is not None and ref.title:
                changed = (self._mic_ref is None
                           or (ref.artist, ref.title)
                           != (self._mic_ref.artist, self._mic_ref.title))
                self._mic_ref = ref
                if changed:
                    # Force _poll_detection to re-resolve instead of
                    # short-circuiting on an unchanged key.
                    self._sync_key = None
                    log.info("mic: %s - %s", ref.artist, ref.title)
            self._mic_stop.wait(MIC_REIDENTIFY_S)

    def action_toggle_mic(self) -> None:
        """`R`: follow room audio via the microphone (songrec, or shazamio where songrec isn't available)."""
        import shutil

        if self._mic_stop is not None and not self._mic_stop.is_set():
            self._mic_stop.set()
            self._mic_ref = None
            self._sync_key = None
            self.notify("Mic off")
            return

        if _radio_cli_running():
            # Two recognisers on one mic just take turns failing.
            self.notify("karaoke -r is already using the mic", severity="warning")
            return
        if not shutil.which("songrec"):
            from . import identify_shazamio
            if not identify_shazamio.available():
                self.notify("songrec is not installed", severity="error")
                return

        self._mic_stop = threading.Event()
        self._mic_ref = None
        self.run_worker(self._mic_loop, exclusive=False, thread=True)
        self.notify("Mic on — listening…")

    # -- post-processing --------------------------------------------------
    def approve_postprocess(self, artist: str, title: str,
                            url: str = "") -> tuple[bool, str]:
        """Queue the given track for post-processing. Returns (ok, message).

        Pure enough to test: does the DB lookup and the publish, returns what
        happened, and leaves the notifying to the caller.

        Requires lyrics to exist. Post-processing derives word timings from the
        approved lyrics and key/BPM from the audio; with no lyrics there is
        nothing to upgrade, and the track is already queued as a lyric gap for
        the backfill to pick up instead.
        """
        from .postprocess_queue import needs_postprocessing, publish_postprocess_task

        if not (artist or title):
            return False, "Nothing playing to approve"
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id is None:
                return False, f"{artist} - {title} is not in the library yet"
            cached = localcache.get_lyrics_by_track_id(track_id, conn)
            if cached is None or not (cached.synced_raw or cached.plain):
                return False, f"No lyrics for {title} yet — queued as a gap"
            pending = needs_postprocessing(track_id, conn)
        if not pending:
            return False, f"{title} is already fully processed"
        if not publish_postprocess_task(artist, title, url):
            return False, "Broker unreachable — nothing queued"
        return True, f"Queued {title}: {', '.join(pending)}"

    def _trigger_postprocess_if_needed(self, artist: str, title: str, url: str = "") -> None:
        """Asynchronously enqueue background postprocessing if missing BPM/key/timings."""
        artist = (artist or "").strip()
        title = (title or "").strip()
        if not (artist and title):
            return
        if not hasattr(self, "_postprocess_enqueued"):
            self._postprocess_enqueued = set()
        pp_key = (artist.lower(), title.lower())
        if pp_key in self._postprocess_enqueued:
            return
        self._postprocess_enqueued.add(pp_key)
        if not hasattr(self, "run_worker"):
            return
        try:
            from .postprocess_queue import enqueue_if_needed
            self.run_worker(
                lambda a=artist, t=title, u=url: enqueue_if_needed(a, t, u),
                exclusive=False,
                thread=True,
            )
        except Exception:
            log.debug("postprocess enqueue dispatch failed", exc_info=True)

    # -- search and the play queue ----------------------------------------

    def action_focus_search(self) -> None:
        """`/`: jump to the search box."""
        self.query_one("#search-input", Input).focus()

    def _genre_matches(self, song_or_genre: object) -> bool:
        selected = str(getattr(self, "_genre_filter", "all") or "all").strip().casefold()
        if selected == "all":
            return True
        if isinstance(song_or_genre, dict):
            audio_g = str(song_or_genre.get("genre") or "").strip().casefold()
            if audio_g == selected:
                return True
            broad_genres = song_or_genre.get("broad_genres") or []
            if isinstance(broad_genres, (list, tuple, set)):
                if any(str(bg).strip().casefold() == selected for bg in broad_genres):
                    return True
            artist_genres = song_or_genre.get("artist_genres") or []
            if isinstance(artist_genres, (list, tuple, set)):
                if any(str(ag).strip().casefold() == selected for ag in artist_genres):
                    return True
            return False
        return str(song_or_genre or "").strip().casefold() == selected

    def _sort_rows(self, rows: list) -> list:
        """Sort library or queue rows by the active sort setting (self._sort)."""
        sort_mode = getattr(self, "_sort", "key")
        res = list(rows)
        if sort_mode == "energy_desc":
            res.sort(key=_energy_of, reverse=True)
        elif sort_mode == "energy_asc":
            res.sort(key=_energy_of)
        elif sort_mode == "bpm_desc":
            res.sort(key=_bpm_of, reverse=True)
        elif sort_mode == "bpm_asc":
            res.sort(key=_bpm_of)
        elif sort_mode == "key":
            res.sort(key=lambda s: (str(s.get("key") or "~"), str(s.get("artist") or "")))
        elif sort_mode == "least_played":
            res.sort(key=lambda s: (_plays_of(s), str(s.get("artist") or "")))
        elif sort_mode == "most_played":
            res.sort(key=_plays_of, reverse=True)
        elif sort_mode == "artist":
            res.sort(key=lambda s: (str(s.get("artist") or "").lower(), str(s.get("title") or "").lower()))

        return res

    def _apply_mood_filter(self, rows: list, conn) -> list:
        """Filter and sort search-result rows by current mood/genre/sort setting."""
        if not rows:
            return rows
        ids = [r.get("track_id") for r in rows if r.get("track_id") is not None]
        placeholders = ",".join(["%s"] * len(ids)) if ids else ""
        energies: dict = {}
        genres: dict = {}
        keys: dict = {}
        bpms: dict = {}
        plays: dict = {}
        has_artist_genres = localcache.table_exists(conn, "artist_genres")
        artist_genres_map = localcache.get_all_artist_genres_map(conn) if has_artist_genres else {}

        if placeholders:
            try:
                cur = conn.execute(
                    f"SELECT track_id, energy, bpm, detected_key FROM track_analysis "
                    f"WHERE track_id IN ({placeholders})",
                    ids,
                )
                for row in cur.fetchall():
                    e = row["energy"]
                    if e is None and row["bpm"] is not None:
                        e = min(1.0, max(0.2, (float(row["bpm"]) - 60.0) / 100.0))
                    energies[row["track_id"]] = e
                    keys[row["track_id"]] = row["detected_key"]
                    bpms[row["track_id"]] = row["bpm"]
                cur = conn.execute(
                    f"SELECT track_id, genre FROM track_genre "
                    f"WHERE track_id IN ({placeholders})",
                    ids,
                )
                for row in cur.fetchall():
                    genres[row["track_id"]] = row["genre"]
                cur = conn.execute(
                    f"SELECT track_id, play_count FROM tracks "
                    f"WHERE track_id IN ({placeholders})",
                    ids,
                )
                for row in cur.fetchall():
                    plays[row["track_id"]] = row["play_count"]
            except Exception:
                pass

        genre_filter = str(getattr(self, "_genre_filter", "all") or "all")
        mood_filter = getattr(self, "_mood_filter", "all")
        mood_level = getattr(self, "_mood_level", 0.0)

        kept = []
        for r in rows:
            tid = r.get("track_id")
            e = r.get("energy")
            if e is None:
                e = energies.get(tid)
            if e is None:
                e = 0.5
            r["energy"] = e
            r["_energy"] = e

            if r.get("key") is None and tid in keys:
                r["key"] = keys.get(tid)
            if r.get("bpm") is None and tid in bpms:
                r["bpm"] = bpms.get(tid)
            if not r.get("genre") and tid in genres:
                r["genre"] = genres.get(tid)
            if not r.get("broad_genres") or not r.get("artist_genres"):
                norm_art = str(r.get("artist") or "").strip().casefold()
                a_info = artist_genres_map.get(norm_art, {})
                r["broad_genres"] = a_info.get("broad", [])
                r["artist_genres"] = a_info.get("specific", [])
            if r.get("play_count") is None and tid in plays:
                r["play_count"] = plays.get(tid, 0)

            if e < mood_level:
                continue
            if mood_filter == "high_energy" and e < 0.75:
                continue
            if mood_filter == "groovy" and not (0.40 <= e <= 0.75):
                continue
            if mood_filter == "mellow" and e > 0.40:
                continue
            if genre_filter != "all" and not self._genre_matches(r):
                continue
            kept.append(r)

        return self._sort_rows(kept)

    def on_input_submitted(self, event) -> None:
        """Enter in the search box: build a list and start playing it."""
        if event.input.id != "search-input":
            return
        # Focus has to leave the box, or every subsequent keypress is typed
        # into it instead of reaching the bindings -- and it has to leave on
        # the empty query too. Returning early there was a way to stay stuck:
        # nothing else is focusable, so Tab had nowhere to go and Enter on an
        # empty box did not release either.
        self.set_focus(None)
        query = (event.value or "").strip()
        self._last_search_query = query
        if not query:
            self._queue_is_wildcard = False
            self._set_queue([])
            return
        self._queue_is_wildcard = query == "*"
        self.run_worker(lambda q=query: self._background_search(q),
                        exclusive=False, thread=True)

    def _background_search(self, query: str, *, start_playing: bool = True) -> None:
        """Search off the UI thread; scoring reads every track's lyrics."""
        from . import librarysearch

        start_playing = True
        try:
            from .player_mpv import is_playing
            if is_playing():
                start_playing = False
        except Exception:
            pass

        try:
            with localcache.connect() as conn:
                if query.strip() == "*":
                    rows = self._all_queue_rows(conn)
                else:
                    genre_filter = str(getattr(self, "_genre_filter", "all") or "all").strip()
                    hits = librarysearch.search(
                        query,
                        conn,
                        limit=INITIAL_BROWSE_LIMIT,
                        genre=genre_filter if genre_filter != "all" else None,
                    )
                    rows = [{"track_id": h.track_id, "artist": h.artist,
                             "title": h.title, "score": h.score,
                             "fields": h.fields,
                             "genre": h.genre,
                             "broad_genres": list(h.broad_genres),
                             "url": librarysearch.playable_url(h.track_id, conn) or ""}
                            for h in hits]
                rows = self._apply_mood_filter(rows, conn)
        except Exception as exc:
            log.debug("search failed", exc_info=True)
            self.call_from_thread(self.notify, f"Search failed: {exc}",
                                  severity="error")
            return
        self.call_from_thread(self._set_queue, rows, query, start_playing=start_playing)

    def _all_queue_rows(self, conn) -> list[dict]:
        """Playable library rows for `*` search, before mood/genre filters."""
        cur = conn.cursor()
        try:
            from .track_analysis import ensure_schema
            ensure_schema(conn)
        except Exception:
            pass
        has_artist_genres = localcache.table_exists(conn, "artist_genres")

        where_clauses = [
            "(t.duration IS NULL OR t.duration <= %(album_seconds)s)",
            "(COALESCE(l.synced_lyrics, '') != '' OR COALESCE(l.plain_lyrics, '') != '')",
        ]
        params = {
            "album_seconds": localcache.ALBUM_UPLOAD_SECONDS,
            "wildcard_limit": INITIAL_BROWSE_LIMIT,
        }
        genre_filter = str(getattr(self, "_genre_filter", "all") or "all").strip()
        if genre_filter != "all":
            params["genre_filter"] = genre_filter
            if has_artist_genres:
                where_clauses.append(
                    """(
                        EXISTS (
                            SELECT 1 FROM artist_genres ag
                            WHERE (lower(trim(ag.broad_genre)) = lower(trim(%(genre_filter)s))
                                   OR lower(trim(ag.genre)) = lower(trim(%(genre_filter)s)))
                              AND ag.artist_normalized = lower(trim(t.artist))
                        )
                        OR (
                            NOT EXISTS (
                                SELECT 1 FROM artist_genres ag
                                WHERE ag.artist_normalized = lower(trim(t.artist))
                            )
                            AND lower(trim(COALESCE(g.genre, ''))) = lower(trim(%(genre_filter)s))
                        )
                    )"""
                )
            else:
                where_clauses.append("lower(trim(COALESCE(g.genre, ''))) = lower(trim(%(genre_filter)s))")

        cur.execute(
            f"""
            SELECT t.track_id, t.artist, t.title, t.play_count,
                   COALESCE(s.url, '') AS url,
                   g.genre AS genre,
                   a.energy,
                   a.detected_key AS key,
                   a.bpm
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
            LEFT JOIN lyrics l
              ON t.track_id = l.track_id AND l.kind = 'approved'
            LEFT JOIN track_analysis a
              ON a.track_id = t.track_id
            LEFT JOIN track_genre g
              ON g.track_id = t.track_id
            WHERE {' AND '.join(where_clauses)}
            
            ORDER BY RANDOM()
            LIMIT %(wildcard_limit)s
            """,
            params,
        )
        return [{"track_id": row["track_id"], "artist": row["artist"] or "",
                 "title": row["title"] or "", "url": row["url"] or "",
                 "genre": row["genre"], "energy": row["energy"],
                 "key": row["key"], "bpm": row["bpm"],
                 "play_count": row["play_count"] or 0}
                for row in cur.fetchall()]

    def _set_queue(self, rows: list, query: str = "", *, start_playing: bool = True) -> None:
        """Show the result list and start it playing.

        A populated local queue always becomes authoritative. This prevents a
        stale YouTube Music temporary queue from taking over after a search.
        """
        if query:
            self._last_search_query = query
            try:
                localcache.record_search_query(query, len(rows))
            except Exception as exc:
                log.debug("record_search_query failed: %s", exc)
        if rows:
            self._play_once = True
        self._ytmusic_queue_follow = False
        self._ytmusic_video_to_queue_index = {}
        self._unfiltered_queue = list(rows)
        self._filter_and_set_queue(query=query, start_playing=start_playing)

    def _filter_and_set_queue(self, query: str = "", start_playing: bool = False) -> None:
        rows = list(getattr(self, "_unfiltered_queue", []))
        if rows:
            try:
                with localcache.connect() as conn:
                    rows = self._apply_mood_filter(rows, conn)
            except Exception:
                pass
        if getattr(self, "_queue_is_wildcard", False):
            # Wildcard means "browse everything", not "repeat the database
            # order". Shuffle after filtering so every reload/session gets a
            # fresh playable order while genre/mood constraints still apply.
            import random
            random.SystemRandom().shuffle(rows)
        self._queue = rows
        table = self.query_one("#queue", DataTable)

        if not rows:
            table.set_class(False, "-on")
            table.clear()
            if query:
                self.notify(f"No matches for {query!r}", severity="warning")
            return
        table.set_class(True, "-on")
        self._render_queue()
        if query:
            self.notify(f"{len(rows)} match(es) for {query!r}")
        if start_playing and rows:
            self.play_queue_index(0)

    def _render_queue(self) -> None:
        """Draw the list, marking what is playing."""
        table = self.query_one("#queue", DataTable)
        table.clear()
        if not table.columns:
            table.add_columns(" ", "Artist", "Title", "Key", "")
        for i, row in enumerate(self._queue):
            playing = i == self._queue_at
            # A row with no source cannot be played; say so rather than
            # skipping silently when it is reached.
            note = "" if row.get("url") else "no source"
            key_val = str(row.get("key") or "—")
            table.add_row(">" if playing else str(i + 1),
                          row["artist"][:18], row["title"][:28], key_val, note)
        follow_label = "  (YT Music follows)" if getattr(self, "_ytmusic_queue_follow", False) else (
            "  (follow queue)" if self._play_once else "")
        table.border_title = (f"queue  {self._queue_at + 1}/{len(self._queue)}"
                              f"{follow_label}"
                              f"  [sort: {self._sort}]")
        
        # Keep cursor on currently playing item, which scrolls it into view
        if self._queue_at >= 0:
            table.move_cursor(row=self._queue_at, animate=False)

    def action_toggle_play_once(self) -> None:
        """`o`: play the queue through once, or let the player carry on.

        With it on, the TUI takes playback back at the end of every track and
        moves to the next entry. That is what stops YouTube Music's own radio
        from taking over -- opening a watch URL makes it attach an RD… station,
        so the "next" song is its suggestion rather than the list you built.
        """
        if self._queue:
            self._play_once = True
            self._render_queue()
            self.notify("Follow Karaoke Queue is locked on while the local queue is active")
            return
        self._play_once = not self._play_once
        self._render_queue()
        self.notify("Follow Karaoke Queue: local queue decides what plays next" if self._play_once
                    else "Follow off: YouTube Music chooses what plays next")

    def _watch_queue(self) -> None:
        """Advance the queue when the browser finishes a track.

        MPRIS cannot answer this: when the site moves to its own suggestion the
        metadata simply changes, which looks the same as the user picking
        something. The page's video element reports that it ended.
        """
        if getattr(self, "_ytmusic_queue_follow", False):
            return
        if not (self._play_once and self._queue and self._queue_at >= 0):
            return
        try:
            state = browser_playback(timeout=0.2)
        except TypeError:
            state = browser_playback()

        if not state:
            return

        is_casting = bool(state.get("casting"))
        if is_casting:
            url = str(state.get("url") or "")
            if "list=" in url:
                self._ytmusic_queue_follow = True
                return
            # While casting, remote device manages buffering and idle transitions
            if not track_finished(state):
                self._idle_since = 0.0
                return

        if not track_finished(state):
            # The window can also come to rest holding nothing at all: the
            # site navigated off the watch URL, or the load never took. That
            # never reads as finished -- no duration, and `ended` will never
            # be set -- so without this the queue waits for an end that cannot
            # arrive. A fresh watch URL looks the same for a moment, so let it
            # hold before treating it as the end of the track.
            if not track_idle(state):
                self._idle_since = 0.0
                return
            now = time.monotonic()
            if not self._idle_since:
                self._idle_since = now
                return
            if now - self._idle_since < IDLE_STALL_S:
                return
            log.info("player idle for %.0fs; advancing the queue", IDLE_STALL_S)
        self._idle_since = 0.0
        # Only act once per track: after advancing, the new track is short of
        # its end again, but a stalled read could otherwise fire repeatedly.
        url = (state or {}).get("url", "")
        if url and url == self._last_finished_url:
            return
        self._last_finished_url = url
        self.action_queue_next()

    def play_queue_index(self, index: int) -> bool:
        """Open the queue entry at ``index``. Returns whether it played."""
        if not (0 <= index < len(self._queue)):
            return False
        row = self._queue[index]
        url = row.get("url") or ""
        if not url:
            self.notify(f"No source for {row['artist']} - {row['title']}",
                        severity="warning")
            self._queue_at = index
            self._render_queue()
            return False
        try:
            open_song_url(
                url, "youtube" if "youtu" in url else "",
                artist=str(row.get("artist") or ""),
                title=str(row.get("title") or ""),
                prefer_audio=True,
            )
        except Exception as exc:
            log.exception("queue play failed")
            self.notify(f"Play failed: {exc}", severity="error")
            return False
        if not getattr(self, "_ytmusic_queue_follow", False):
            # Drop older played items, keep last 2 for history (so current is 3rd)
            if index > 2:
                drop_count = index - 2
                self._queue = self._queue[drop_count:]
                index = 2

        self._queue_at = index
        self._render_queue()
        localcache.save_active_queue(
            getattr(self, "_active_playlist_id", ""),
            getattr(self, "_active_playlist_name", ""),
            self._queue,
            current_index=index,
        )
        localcache.record_queue_event(
            "advance",
            row.get("track_id"),
            str(row.get("artist") or ""),
            str(row.get("title") or ""),
            index,
        )
        self.notify(f"Playing {row['artist']} - {row['title']}")
        self._trigger_postprocess_if_needed(
            str(row.get("artist") or ""),
            str(row.get("title") or ""),
            url,
        )
        return True

    def action_queue_next(self) -> None:
        """Advance to the next playable entry in the queue."""
        if not self._queue:
            return
        index = self._queue_at + 1
        while index < len(self._queue):
            if self.play_queue_index(index):
                return
            index += 1
        self.notify("End of queue")

    def action_queue_rollback(self) -> None:
        """`u`: Rollback to previously played track/state in the queue."""
        if not self._queue:
            self.notify("Queue is empty", severity="warning")
            return
        events = localcache.get_recent_queue_events(limit=15)
        target_idx: int | None = None
        for ev in events:
            idx = ev.get("queue_index")
            if idx is not None and isinstance(idx, int) and 0 <= idx < len(self._queue) and idx != self._queue_at:
                target_idx = idx
                break

        if target_idx is None:
            if self._queue_at > 0:
                target_idx = self._queue_at - 1

        if target_idx is not None and 0 <= target_idx < len(self._queue):
            row = self._queue[target_idx]
            self._queue_at = target_idx
            self._render_queue()
            if self._play_once:
                self.play_queue_index(target_idx)
            else:
                from . import player_open
                if not player_open.cdp_previous_track():
                    if self._det.is_active:
                        self.api.player_previous(self._control_player())
            localcache.save_active_queue(
                getattr(self, "_active_playlist_id", ""),
                getattr(self, "_active_playlist_name", ""),
                self._queue,
                current_index=target_idx,
            )
            localcache.record_queue_event(
                "rollback",
                row.get("track_id"),
                str(row.get("artist") or ""),
                str(row.get("title") or ""),
                target_idx,
            )
            self.notify(f"Rolled back to #{target_idx + 1}: {row.get('artist')} - {row.get('title')}")
        else:
            self.notify("No earlier queue state to roll back to", severity="warning")
            index += 1
        self.notify("End of queue")

    def action_enqueue_selected(self) -> None:
        """`a`: Add the currently selected track to the end of the queue."""
        song = self._selected_song()
        if song is None:
            self.notify("No song selected to enqueue", severity="warning")
            return
        row = {
            "track_id": song.get("track_id"),
            "artist": str(song.get("artist") or ""),
            "title": str(song.get("title") or ""),
            "url": str(song.get("url") or ""),
            "kind": str(song.get("kind") or ""),
        }
        if not self._queue:
            self._play_once = True
            self._set_queue([row])
            return
        self._play_once = True
        self._queue.append(row)
        try:
            self.query_one("#queue", DataTable).set_class(True, "-on")
        except Exception:
            pass
        self._render_queue()
        self.notify(f"Enqueued {row['artist']} - {row['title']} (queue: {len(self._queue)})")
        self._sync_enqueued_to_ytmusic([row])

    def action_shuffle_queue(self) -> None:
        """`U`: Shuffle upcoming items in the queue."""
        import random
        if not self._queue or self._queue_at >= len(self._queue) - 1:
            self.notify("No upcoming queue items to shuffle", severity="warning")
            return
        upcoming = self._queue[self._queue_at + 1:]
        random.shuffle(upcoming)
        self._queue = self._queue[:self._queue_at + 1] + upcoming
        self._render_queue()
        self.notify(f"Shuffled {len(upcoming)} upcoming queue items")

    def action_clear_queue(self) -> None:
        """`C`: Clear the current queue."""
        self._queue = []
        self._queue_at = -1
        self._ytmusic_queue_follow = False
        self._active_playlist_id = ""
        self._active_playlist_name = ""
        self._ytmusic_video_to_queue_index = {}
        try:
            table = self.query_one("#queue", DataTable)
            table.set_class(False, "-on")
            table.clear()
        except Exception:
            pass
        self.notify("Queue cleared")

    def action_suggest_queue(self) -> None:
        """`G`: keep the vibe going — append tracks that fit the whole queue.

        Seeds on every track in the queue by audio similarity (CLAP, spectral
        fallback), pools the neighbours, and appends the ones that fit best,
        skipping anything already queued.
        """
        seeds = [row.get("track_id") for row in self._queue
                 if row.get("track_id")]
        if not seeds:
            self.notify("Queue is empty — nothing to seed suggestions on",
                        severity="warning")
            return
        self.notify("Finding tracks that keep the vibe going…")

        def _bg() -> None:
            try:
                picks = self.api.suggest_queue(list(seeds), limit=5)
            except Exception as exc:
                self.call_from_thread(
                    self.notify, f"Suggestions failed: {exc}", severity="error")
                return
            self.call_from_thread(self._apply_suggestions, picks)

        self.run_worker(_bg, thread=True)

    def _apply_suggestions(self, picks: list) -> None:
        """Append suggested tracks to the queue (called on the UI thread)."""
        if not picks:
            self.notify("No suggestions — seed tracks may lack audio vectors",
                        severity="warning")
            return
        self._play_once = True
        new_rows = []
        for p in picks:
            r = {
                "track_id": p.get("track_id"),
                "artist": str(p.get("artist") or ""),
                "title": str(p.get("title") or ""),
                "url": str(p.get("url") or ""),
                "kind": "",
            }
            self._queue.append(r)
            new_rows.append(r)
        try:
            self.query_one("#queue", DataTable).set_class(True, "-on")
        except Exception:
            pass
        self._render_queue()
        space = picks[0].get("space", "audio") if picks else "audio"
        self.notify(f"Added {len(picks)} tracks to keep the vibe going "
                    f"({space}); queue: {len(self._queue)}")
        self._sync_enqueued_to_ytmusic(new_rows)

    def action_more_like_this(self) -> None:
        """`M`: Find and enqueue tracks that sound acoustically similar via CLAP.

        Seeds on the currently selected song in the library table, or the
        currently playing track if no table row is highlighted.
        """
        # DataTable.cursor_row is 0, not None, when no row was ever
        # highlighted, so _selected_song() always returns something -- the
        # first row of the current filter. Seeding on it unconditionally meant
        # M always searched from that same track no matter what was playing,
        # and the branches below were unreachable. Only trust the library
        # selection when the user is actually in the library table.
        song = self._selected_song() if self._library_has_focus() else None
        target_id = None
        target_label = ""
        if song and song.get("track_id"):
            target_id = int(song["track_id"])
            target_label = f"{song.get('artist')} - {song.get('title')}"
        elif self._current_track_id is not None:
            target_id = self._current_track_id
            if self._song:
                target_label = f"{self._song.get('artist')} - {self._song.get('title')}"
            else:
                target_label = f"track #{target_id}"
        elif self._queue and self._queue_at < len(self._queue):
            q_song = self._queue[self._queue_at]
            if q_song.get("track_id"):
                target_id = int(q_song["track_id"])
                target_label = f"{q_song.get('artist')} - {q_song.get('title')}"

        if not target_id:
            # Nothing playing and nothing queued: fall back to the highlighted
            # library row even without focus, so M still does something useful
            # rather than refusing.
            fallback = self._selected_song()
            if fallback and fallback.get("track_id"):
                target_id = int(fallback["track_id"])
                target_label = f"{fallback.get('artist')} - {fallback.get('title')}"

        if not target_id:
            self.notify("No track selected or playing to find similar songs for",
                        severity="warning")
            return

        self.notify(f"Finding tracks that sound like '{target_label}'…")

        def _bg() -> None:
            try:
                picks = self.api.sounds_like(target_id, limit=5)
            except Exception as exc:
                self.call_from_thread(
                    self.notify, f"Sound search failed: {exc}", severity="error")
                return
            self.call_from_thread(self._apply_more_like_this, picks, target_label)

        self.run_worker(_bg, thread=True)

    def _apply_more_like_this(self, picks: list, seed_label: str) -> None:
        """Append tracks matching seed sound to the queue (called on UI thread)."""
        if not picks:
            self.notify(f"No acoustic matches found for '{seed_label}'",
                        severity="warning")
            return
        self._play_once = True
        new_rows = []
        for p in picks:
            r = {
                "track_id": p.get("track_id"),
                "artist": str(p.get("artist") or ""),
                "title": str(p.get("title") or ""),
                "url": str(p.get("url") or ""),
                "kind": "",
            }
            self._queue.append(r)
            new_rows.append(r)
        try:
            self.query_one("#queue", DataTable).set_class(True, "-on")
        except Exception:
            pass
        self._render_queue()
        space = picks[0].get("space", "audio") if picks else "audio"
        self.notify(f"Added {len(new_rows)} tracks sounding like '{seed_label}' "
                    f"({space}); queue: {len(self._queue)}")
        self._sync_enqueued_to_ytmusic(new_rows)

    def _queue_video_map(self) -> dict[str, int]:
        """Map YouTube video IDs in the current queue to their row index."""
        mapping: dict[str, int] = {}
        for idx, row in enumerate(self._queue):
            vid = localcache.extract_youtube_id(str(row.get("url") or ""))
            if vid and vid not in mapping:
                mapping[vid] = idx
        return mapping

    def action_ytmusic_playlist_queue(self) -> None:
        """`Y`: mirror the current search queue into a private YT Music playlist.

        Only queue rows with already-known YouTube/YT Music video IDs are added;
        missing/Spotify/local-only rows are skipped rather than guessed by search.
        Playlists are capped to max 50 tracks, given a clean name matching the active search,
        persisted to SQLite DB, and synced via a background event-driven worker.
        Once the playlist is opened, YT Music owns playback advancement and the
        TUI follows detected video changes to keep lyrics/highlight in sync.
        """
        if not self._queue:
            self.notify("Queue is empty — search first", severity="warning")
            return

        MAX_PLAYLIST_TRACKS = 50
        capped_rows = list(self._queue)[:MAX_PLAYLIST_TRACKS]
        query = getattr(self, "_last_search_query", "")
        from . import ytmusic_playlist
        clean_name = ytmusic_playlist.clean_playlist_name_for_query(query)

        self.notify(f"Syncing '{clean_name}' (max {MAX_PLAYLIST_TRACKS} tracks)…")

        def _bg() -> None:
            try:
                from . import ytmusic_playlist
                from .postprocess_queue import orchestrator

                candidates = ytmusic_playlist.queue_rows_to_ytmusic_candidates(capped_rows)
                if not candidates:
                    self.call_from_thread(
                        self.notify,
                        "No YouTube/YT Music rows in current queue",
                        severity="warning",
                    )
                    return

                playlist_id = ""
                playlist_title = clean_name
                unresolved = []

                if orchestrator() != "legacy":
                    try:
                        from .tasks import sync_ytmusic_playlist
                        async_task = sync_ytmusic_playlist.delay({
                            "name": clean_name,
                            "query": query,
                            "rows": capped_rows,
                            "max_tracks": MAX_PLAYLIST_TRACKS,
                        })
                        res = async_task.get(timeout=25)
                        playlist_id = res.get("playlist_id") or ""
                        playlist_title = res.get("name") or clean_name
                    except Exception as exc:
                        log.debug("Celery sync_ytmusic_playlist fell back: %s", exc)

                if not playlist_id:
                    result = ytmusic_playlist.create_temp_queue_playlist(
                        capped_rows,
                        name=clean_name,
                        search_query=query,
                        max_tracks=MAX_PLAYLIST_TRACKS,
                    )
                    playlist_id = result.playlist_id
                    playlist_title = result.name
                    unresolved = result.unresolved

                if not playlist_id:
                    self.call_from_thread(
                        self.notify,
                        "YouTube Music playlist was not created",
                        severity="error",
                    )
                    return

                first = candidates[0].video_id
                playlist_url = (
                    f"https://music.youtube.com/watch?v={first}"
                    f"&list={playlist_id}"
                )
                open_song_url(playlist_url, "youtube_music_playlist", prefer_audio=True)
            except Exception as exc:
                log.exception("YT Music playlist sync failed")
                self.call_from_thread(
                    self.notify, f"YT Music playlist failed: {exc}", severity="error")
                return
            self.call_from_thread(
                self._enable_ytmusic_queue_follow,
                playlist_id,
                len(candidates),
                unresolved,
                playlist_title,
            )

        self.run_worker(_bg, thread=True)

    def _sync_enqueued_to_ytmusic(self, new_rows: list[dict[str, Any]]) -> None:
        """If currently following a YT Music playlist, append new track(s) to DB and remote."""
        if not getattr(self, "_ytmusic_queue_follow", False):
            return
        playlist_id = getattr(self, "_active_playlist_id", "")
        if not playlist_id:
            return

        self._ytmusic_video_to_queue_index = self._queue_video_map()
        with localcache.connect() as conn:
            for r in new_rows:
                localcache.append_playlist_track(playlist_id, r, conn=conn)
            localcache.save_active_queue(
                playlist_id,
                getattr(self, "_active_playlist_name", ""),
                self._queue,
                current_index=self._queue_at,
                conn=conn,
            )

        def _bg() -> None:
            from .postprocess_queue import orchestrator
            from . import ytmusic_playlist

            for r in new_rows:
                artist = str(r.get("artist") or "")
                title = str(r.get("title") or "")
                vid = localcache.extract_youtube_id(str(r.get("url") or ""))
                added = False
                if orchestrator() != "legacy":
                    try:
                        from .tasks import add_to_ytmusic_playlist_task
                        res = add_to_ytmusic_playlist_task.delay({
                            "playlist_id": playlist_id,
                            "artist": artist,
                            "title": title,
                            "video_id": vid or None,
                        }).get(timeout=15)
                        added = bool(res.get("ok"))
                    except Exception as exc:
                        log.debug("Celery add_to_ytmusic_playlist_task fell back: %s", exc)
                if not added:
                    try:
                        ok, _ = ytmusic_playlist.add_track_to_ytmusic_playlist(
                            playlist_id, artist, title, video_id=vid or "",
                        )
                        added = ok
                    except Exception as exc:
                        log.warning("Failed to add track to remote YT Music playlist: %s", exc)
                if added:
                    log.info("Synced track '%s - %s' to remote YT Music playlist %s", artist, title, playlist_id)

        if playlist_id.startswith(("PL", "VL", "RD", "OLAK5")):
            self.run_worker(_bg, thread=True, exclusive=False)

    def _enable_ytmusic_queue_follow(self, playlist_id: str, count: int, unresolved: list, name: str = "") -> None:
        self._ytmusic_queue_follow = True
        self._active_playlist_id = playlist_id
        self._active_playlist_name = name
        # YT Music is now the player queue. Do not force-open local next tracks
        # when one finishes; just mirror detected watch URL changes back to the
        # local queue highlight/lyrics.
        self._play_once = False
        self._ytmusic_video_to_queue_index = self._queue_video_map()
        self._render_queue()
        localcache.save_active_queue(playlist_id, name, self._queue, current_index=self._queue_at)
        localcache.record_queue_event(
            "loaded",
            metadata={"playlist_id": playlist_id, "count": count, "name": name},
        )
        skipped = len(unresolved)
        extra = f"; {skipped} unresolved" if skipped else ""
        self.notify(
            f"Playing YT Music playlist {playlist_id} ({count} queued{extra})")

    def _sync_queue_to_detection(self, det, track_id: int | None) -> None:
        """Move the local queue highlight to the YT Music item now playing."""
        if not getattr(self, "_ytmusic_queue_follow", False):
            return
        index: int | None = None
        vid = localcache.extract_youtube_id(getattr(det, "url", "") or "")
        if vid:
            index = self._ytmusic_video_to_queue_index.get(vid)
        if index is None and track_id is not None:
            for i, row in enumerate(self._queue):
                if row.get("track_id") == track_id:
                    index = i
                    break
        if index is None and getattr(det, "title", ""):
            d_title = det.title.strip().casefold()
            d_artist = (getattr(det, "artist", "") or "").strip().casefold()
            for i, row in enumerate(self._queue):
                r_title = (row.get("title") or "").strip().casefold()
                r_artist = (row.get("artist") or "").strip().casefold()
                if r_title == d_title and (not d_artist or not r_artist or d_artist in r_artist or r_artist in d_artist):
                    index = i
                    break
        if index is None or index == self._queue_at:
            return
        self._queue_at = index
        self._render_queue()
        row = self._queue[index] if 0 <= index < len(self._queue) else {}
        localcache.save_active_queue(
            getattr(self, "_active_playlist_id", ""),
            getattr(self, "_active_playlist_name", ""),
            self._queue,
            current_index=index,
        )
        localcache.record_queue_event(
            "follow",
            row.get("track_id"),
            str(row.get("artist") or ""),
            str(row.get("title") or ""),
            index,
        )

    def _check_playlist_detection(self, det: Any, conn: Any) -> None:
        """Detect if active playback belongs to a synced or saved YouTube Music playlist."""
        pl_id = getattr(det, "playlist_id", "") or ""
        if not pl_id:
            url = getattr(det, "url", "") or ""
            if url:
                pl_id = localcache.extract_playlist_id(url)

        saved_pl = None
        if pl_id:
            saved_pl = localcache.find_saved_playlist_by_id(pl_id, conn=conn)

        if not saved_pl:
            vid = localcache.extract_youtube_id(getattr(det, "url", "") or "")
            if vid:
                matches = localcache.find_playlist_by_video_id(vid, conn=conn)
                if matches:
                    saved_pl = matches[0]

        if not saved_pl:
            # Check if this is an external/unsynced YouTube Music playlist playing
            if pl_id and pl_id not in getattr(self, "_attempted_external_playlists", set()):
                if not hasattr(self, "_attempted_external_playlists"):
                    self._attempted_external_playlists = set()
                self._attempted_external_playlists.add(pl_id)
                self.run_worker(
                    lambda p=pl_id: self._fetch_and_follow_external_playlist(p),
                    thread=True,
                    exclusive=False,
                )
            return

        found_id = str(saved_pl.get("playlist_id") or "")
        pl_name = str(saved_pl.get("name") or found_id)
        active_id = getattr(self, "_active_playlist_id", "")

        if found_id != active_id:
            if not self._queue or getattr(self, "_ytmusic_queue_follow", False):
                tracks = localcache.get_saved_playlist_tracks(found_id, conn=conn)
                if tracks:
                    rows = [
                        {
                            "track_id": t.get("track_id"),
                            "artist": t.get("artist") or "",
                            "title": t.get("title") or "",
                            "url": t.get("url") or (f"https://music.youtube.com/watch?v={t['video_id']}" if t.get("video_id") else ""),
                            "kind": "ytmusic",
                        }
                        for t in tracks
                    ]
                    self._apply_restored_playlist(rows, found_id, pl_name)
                    self.notify(f"Detected playlist '{pl_name}' — following queue", severity="information")
            else:
                if getattr(self, "_last_notified_playlist_id", "") != found_id:
                    self._last_notified_playlist_id = found_id
                    self.notify(f"Playing from saved playlist '{pl_name}' (press Shift+L to view)", severity="information")

    def _fetch_and_follow_external_playlist(self, pl_id: str) -> None:
        """Fetch an external/unsynced YouTube Music playlist from remote and follow it."""
        try:
            from . import ytmusic_playlist
            client = ytmusic_playlist.get_ytmusic_client()
            remote_tracks = client.get_playlist_tracks(pl_id, limit=200)
            if not remote_tracks:
                self.call_from_thread(self._fallback_to_songrec, pl_id)
                return
            pl_title = f"YT Music Playlist {pl_id[:8]}"
            try:
                pl_info = client.get_playlist(pl_id, limit=1)
                if pl_info and pl_info.get("title"):
                    pl_title = pl_info["title"]
            except Exception:
                pass

            saved_tracks = []
            with localcache.connect() as conn:
                for idx, t in enumerate(remote_tracks):
                    artist = str(t.get("artist") or (t.get("artists", [{}])[0].get("name") if t.get("artists") else "") or "").strip()
                    title = str(t.get("title") or "").strip()
                    vid = str(t.get("videoId") or "").strip()
                    url = f"https://music.youtube.com/watch?v={vid}" if vid else ""
                    tid = localcache.find_track_id(artist, title, conn)
                    if tid is None and url:
                        found = localcache.find_track_by_url(url, conn)
                        if found:
                            tid = found[0]
                    if tid is None and vid:
                        tid = localcache.add_track_source(artist, title, url=url, kind="youtube_music", conn=conn)
                    saved_tracks.append({
                        "position": idx + 1,
                        "track_id": tid,
                        "artist": artist,
                        "title": title,
                        "video_id": vid,
                        "url": url,
                    })
                localcache.save_playlist(pl_id, pl_title, search_query="", tracks=saved_tracks, conn=conn)

            rows = [
                {
                    "track_id": t.get("track_id"),
                    "artist": t.get("artist") or "",
                    "title": t.get("title") or "",
                    "url": t.get("url") or (f"https://music.youtube.com/watch?v={t['video_id']}" if t.get("video_id") else ""),
                    "kind": "ytmusic",
                }
                for t in saved_tracks
            ]
            self.call_from_thread(self._on_external_playlist_loaded, rows, pl_id, pl_title)
        except Exception as exc:
            log.debug("Failed fetching external playlist %s: %s", pl_id, exc)
            self.call_from_thread(self._fallback_to_songrec, pl_id)

    def _fallback_to_songrec(self, pl_id: str = "") -> None:
        """Fallback when external playlist is inaccessible: listen via songrec and start postprocess."""
        import shutil
        if not shutil.which("songrec"):
            self.notify(f"External playlist {pl_id[:8]} inaccessible and songrec not installed", severity="warning")
            return
        if self._mic_running():
            return
        self._mic_stop = threading.Event()
        self._mic_ref = None
        self.run_worker(self._mic_loop, exclusive=False, thread=True)
        self.notify("External playlist inaccessible — falling back to radio mode audio recognition", severity="information")

    def _on_external_playlist_loaded(self, rows: list[dict], pl_id: str, title: str) -> None:
        if not self._queue or getattr(self, "_ytmusic_queue_follow", False):
            self._apply_restored_playlist(rows, pl_id, title)
            self.notify(f"Synced & following YT Music playlist '{title}' ({len(rows)} tracks)", severity="information")
        else:
            self.notify(f"Imported YT Music playlist '{title}' (press Shift+L to view)", severity="information")

    def action_sample_key(self) -> None:
        """`k`: detect key/BPM by recording what is playing.

        For Spotify there is no file to analyse, so those tracks can never be
        post-processed the normal way. Recording the sink monitor gives a clean
        digital copy of the audio and closes that gap.

        Capture is real time and takes the better part of a minute, so it runs
        in a worker and is guarded against being started twice.
        """
        det = self._det
        if not (det.artist and det.title):
            self.notify("Nothing playing to sample", severity="warning")
            return
        if self._sampling:
            self.notify("Already sampling", severity="warning")
            return
        self._sampling = True
        self._cancel_sample = False
        seconds = sample_audio.DEFAULT_SECONDS
        self.notify(f"Sampling {seconds:.0f}s of {det.title}…")
        self.run_worker(
            lambda a=det.artist, t=det.title: self._background_sample(a, t),
            exclusive=False, thread=True,
        )

    def action_cancel_sample(self) -> None:
        """ctrl+c: stop a capture and throw it away; otherwise quit.

        A 45-second capture is the one thing here long enough that a user
        reaches for ctrl+c, and before this it did nothing to it -- the app
        quit while ffmpeg carried on writing to a file nobody would read.
        """
        # A second press quits regardless. This binding is priority, so if
        # _sampling ever stuck true a single-purpose handler would leave no way
        # out of the app at all -- and an unquittable TUI is a worse bug than
        # the one being fixed.
        if not self._sampling or self._cancel_sample:
            self.exit()
            return
        self._cancel_sample = True
        self.notify("Stopping the sample — ctrl+c again to quit")

    def _background_classify(self, track_id: int) -> None:
        """Label a track that has no genre or tone, in a worker thread.

        Runs behind whatever is playing, so it must not raise and must not be
        worth noticing. The read-out refreshes only when something was added.
        """
        from . import autoclassify

        try:
            with localcache.connect() as conn:
                added = autoclassify.run(track_id, conn)
        except Exception:
            log.debug("auto-classify failed for %s", track_id, exc_info=True)
            return
        if added and track_id == self._current_track_id:
            self.call_from_thread(self._refresh_track_info, None)

    def _background_sample(self, artist: str, title: str) -> None:
        """Record and analyse the playing audio, in a worker thread."""
        def _keep_going() -> bool:
            if self._cancel_sample:
                return False
            det = self._det
            return bool(det.artist == artist and det.title == title)

        try:
            result = sample_audio.sample_and_analyse(
                artist, title, should_continue=_keep_going)
        except sample_audio.CaptureAborted:
            # Not a failure: the track changed under it. Sampling on would have
            # stored this song's key, tempo and genre against the previous one.
            log.info("sample abandoned: %s - %s stopped playing", artist, title)
            self.call_from_thread(
                self.notify, f"Stopped sampling — {title} ended",
                severity="warning")
            return
        except sample_audio.CaptureError as exc:
            log.warning("sample failed: %s", exc)
            self.call_from_thread(self.notify, f"Sample failed: {exc}",
                                  severity="error")
            return
        except sample_audio.AnalysisUnavailable as exc:
            # The recording worked; this interpreter just cannot analyse it.
            # Say so plainly rather than reporting an unknown key.
            log.warning("sample not analysed: %s", exc)
            self.call_from_thread(self.notify, str(exc), severity="warning")
            return
        except Exception as exc:
            log.exception("sample analysis failed")
            self.call_from_thread(self.notify, f"Analysis failed: {exc}",
                                  severity="error")
            return
        finally:
            self._sampling = False

        key = result.key.name if result.key else "unknown"
        bpm = f"{result.bpm:.0f}" if result.bpm else "?"
        # The genre is read back rather than returned, because labelling is
        # best-effort inside analyse_sample and a failure there must not fail
        # the sample. Saying so here is how a silent failure becomes visible:
        # every `k` was failing to label for an evening and the only sign was
        # a DEBUG line.
        label = ""
        try:
            with localcache.connect() as conn:
                track_id = localcache.find_track_id(artist, title, conn)
                if track_id is not None:
                    label = classification_line(
                        localcache.genre_for(track_id, conn),
                        localcache.tone_for(track_id, conn))
        except Exception:
            log.debug("could not read back the sampled label", exc_info=True)
        summary = f"{artist} - {title}: {key}, {bpm} BPM"
        self.call_from_thread(self.notify,
                              f"{summary} · {label}" if label else summary)
        # The analysis panel reads from the DB, so refresh it now the row exists.
        self.call_from_thread(self._refresh_track_info, None)

    def on_unmount(self) -> None:
        """Stop any capture when the app exits.

        Without this the ffmpeg child outlives the TUI: it keeps writing audio
        with nothing supervising it, its `recordings` row stays 'recording'
        forever, and the next TUI -- seeing no session of its own -- starts a
        second recorder on the same source. Observed in the wild: a capture
        orphaned for 1h51m alongside a live one.
        """
        # A sample is the other capture that can outlive the app. It holds an
        # ffmpeg on the monitor sink for the better part of a minute, so
        # leaving it running on exit repeats the orphan above in miniature.
        self._cancel_sample = True
        try:
            if hasattr(self, "api") and self.api is not None:
                self.api.record_stop()
        except Exception:
            log.debug("stopping recordings via api on exit failed", exc_info=True)
        try:
            recorder.stop_all()
        except Exception:
            log.debug("stopping recordings on exit failed", exc_info=True)

        # The mic worker is a thread, and a thread worker cannot be cancelled
        # from outside -- Textual can only ask. `_mic_loop` watches this event
        # and nothing else, so without setting it the loop runs forever and
        # quitting hangs on the executor join that never completes (issue #39).
        mic_stop = getattr(self, "_mic_stop", None)
        if mic_stop is not None:
            mic_stop.set()

        # Same shape, one layer down: the shared CDP connection owns a thread
        # with a live event loop, and a loop nobody stops is a loop that gets
        # joined at exit.
        try:
            close_cdp()
        except Exception:
            log.debug("closing the CDP channel on exit failed", exc_info=True)

        # Cancel all active Textual workers so background threads don't block
        # the executor shutdown at exit.
        try:
            self.workers.cancel_all()
        except Exception:
            pass

    def _analyse_recording(self, recording_id: int) -> None:
        """Decompile a finished recording, in a worker thread.

        Minutes of work for an evening's audio, so it cannot run on the UI
        thread; and it is deliberately *not* run on app exit, where starting a
        long job during shutdown would be worse than leaving it. A session
        closed that way is caught by the reminder at mount instead.
        """
        def _work() -> None:
            from . import recording_worker
            try:
                lines = recording_worker.analyse(recording_id)
            except Exception as exc:
                log.exception("recording analysis failed")
                self.call_from_thread(self.notify,
                                      f"Analysis failed: {exc}", severity="error")
                return
            done = sum(1 for line in lines if line.strip().startswith("ok"))
            self.call_from_thread(
                self.notify,
                f"Recording {recording_id}: {done} track(s) analysed")

        self.notify(f"Analysing recording {recording_id}…")
        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("analysis dispatch failed", exc_info=True)

    def _warn_unanalysed_recordings(self) -> None:
        """Point out sessions that finished without being analysed.

        Quitting mid-recording, or a crash, closes the row without analysing
        it. Without this they are invisible: the audio sits on disk and the
        tracks never gain their key or BPM.
        """
        try:
            with localcache.connect() as conn:
                rows = conn.execute(
                    "SELECT count(*) n FROM recordings WHERE status = 'complete'"
                ).fetchone()
        except Exception:
            return
        pending = int(rows["n"]) if rows else 0
        if pending:
            self.notify(f"{pending} recording(s) awaiting analysis "
                        f"(karaoke-recording --analyse)", severity="warning")

    def _refresh_record_status(self) -> None:
        """Keep the recording indicator current; also catches a died recorder."""
        try:
            panel = self.query_one("#record-panel", Static)
        except Exception:
            return
        if self._recording_id is None:
            rec_status = self.api.record_status()
            active = rec_status.get("recording", [])
            if active:
                self._recording_id = int(active[0]["recording_id"])
            else:
                panel.set_class(False, "-on")
                panel.update("")
                return

        rec_status = self.api.record_status()
        active = rec_status.get("recording", [])
        active_map = {int(item["recording_id"]): item for item in active}

        rec_id = self._recording_id
        if rec_id is None:
            panel.set_class(False, "-on")
            panel.update("")
            return

        if rec_id in active_map:
            item = active_map[rec_id]
            self._record_tick += 1
            panel.set_class(True, "-on")
            panel.update(record_panel(
                recording_id=rec_id,
                elapsed_s=float(item.get("elapsed_s") or 0.0),
                marks_ok=int(item.get("identified") or 0),
                marks_total=int(item.get("marks") or 0),
                size_bytes=int(item.get("audio_bytes") or 0),
                source=str(item.get("source") or ""),
                blink=self._record_tick % 2 == 1,
            ))
            return

        if not recorder.is_running(rec_id):
            # The capture died on its own (ffmpeg exited, or a cap was hit).
            self.notify(f"Recording {rec_id} ended", severity="warning")
            self._recording_id = None
            panel.set_class(False, "-on")
            panel.update("")
            return

        self._record_tick += 1
        if self._record_tick % 5 == 1 or self._record_marks is None:
            self._record_marks = recorder.mark_count(rec_id)
        directory = recorder.session_directory(rec_id)
        panel.set_class(True, "-on")
        panel.update(record_panel(
            recording_id=rec_id,
            elapsed_s=recorder.elapsed(rec_id) or 0.0,
            marks_ok=self._record_marks[0], marks_total=self._record_marks[1],
            size_bytes=recorder.directory_size(directory) if directory else 0,
            source=recorder.session_source(rec_id) or "",
            blink=self._record_tick % 2 == 1,
        ))

    def action_approve_postprocess(self) -> None:
        """`A`: post-process whatever is playing, on demand.

        The automatic enqueue in _poll_detection is silent and fires once per
        session, so it cannot be retried if the broker was down at the time.
        This is the deliberate version: it reports what happened and clears the
        session guard so the track can be queued again.
        """
        if not self._current_song:
            self.notify("Nothing playing to approve", severity="warning")
            return
        artist, title, url = self._current_song
        self._postprocess_enqueued.discard((artist.lower(), title.lower()))
        ok, message = self.approve_postprocess(artist, title, url)
        self.notify(message, severity="information" if ok else "warning")

    def action_browse_recordings(self) -> None:
        """`B`: browse recorded sessions and play a track out of one."""
        from . import recording_worker

        try:
            with localcache.connect() as conn:
                rows = conn.execute(
                    "SELECT recording_id, started_at, status, dir,"
                    "       (SELECT COALESCE(sum(m.ok), 0) FROM recording_marks m"
                    "         WHERE m.recording_id = r.recording_id) AS identified"
                    " FROM recordings r"
                    " WHERE status != 'discarded'"
                    " ORDER BY recording_id DESC LIMIT 40").fetchall()
                recordings = [
                    {"recording_id": r["recording_id"],
                     "started_at": r["started_at"],
                     "status": r["status"],
                     "identified": r["identified"],
                     "audio_bytes": recording_worker.audio_bytes(r)}
                    for r in rows]
        except Exception as exc:
            self.notify(f"Recordings unavailable: {exc}", severity="error")
            return
        if not recordings:
            self.notify("No recordings yet")
            return
        self.push_screen(RecordingBrowseScreen(recordings))

    def action_browse_playlists(self) -> None:
        """`L`: browse saved YouTube Music playlists, load/follow or open in browser."""
        try:
            with localcache.connect() as conn:
                playlists = localcache.get_saved_playlists(100, conn=conn)
        except Exception as exc:
            self.notify(f"Playlists unavailable: {exc}", severity="error")
            return
        self.push_screen(SavedPlaylistsScreen(playlists))

    def action_stats(self) -> None:
        """`T`: library, pipeline and listening statistics."""
        from . import librarystats

        try:
            with localcache.connect() as conn:
                lib = librarystats.collect(conn)
            summary = localcache.summarize()
        except Exception as exc:
            self.notify(f"Stats unavailable: {exc}", severity="error")
            return
        status = None
        try:
            from .postprocess_status import get_status
            status = get_status(sample_cpu=False)
        except Exception:
            pass          # broker down is not a reason to hide the rest
        self.push_screen(StatsScreen(stats_panels(lib, summary, status)))

    def _get_current_or_selected_track_id(self) -> int | None:
        if self._current_track_id is not None:
            return self._current_track_id
        if getattr(self, "_song", None) and self._song.get("track_id"):
            try:
                return int(self._song["track_id"])
            except (ValueError, TypeError):
                pass
        if getattr(self, "_queue", None) and 0 <= getattr(self, "_queue_at", -1) < len(self._queue):
            q_song = self._queue[self._queue_at]
            if q_song and q_song.get("track_id"):
                try:
                    return int(q_song["track_id"])
                except (ValueError, TypeError):
                    pass
        song = self._selected_song()
        if song and song.get("track_id"):
            try:
                return int(song["track_id"])
            except (ValueError, TypeError):
                pass
        return None

    def action_ai_dj_chat(self, initial_prompt: Optional[str] = None) -> None:
        """`D`: open the interactive AI DJ Chat booth."""
        self.push_screen(DJChatScreen(
            current_track_provider=self._get_current_or_selected_track_id,
            current_track_id=self._get_current_or_selected_track_id(),
            initial_prompt=initial_prompt,
        ))

    def action_lyric_vibe(self) -> None:
        """`v`: show lyric sentiment vibe, emotional arc & vocal tips for the currently playing song."""
        self.action_ai_dj_chat(initial_prompt="/vibe")

    def action_help(self) -> None:
        # get_key_display is the app's own formatter, so the help screen and
        # the Footer always agree on how a key is written.
        self.push_screen(HelpScreen(
            binding_rows(self.BINDINGS, key_display=self.get_key_display)))

    def action_refresh(self) -> None:
        self.load_songs()
        self._show_selected_song()
        self._poll_detection()
        self.notify("Refreshed")

    def action_resync(self) -> None:
        self._sync_key = None
        self._poll_detection()
        self.notify("Resynced playhead")

    def action_sync_earlier(self) -> None:
        # Show lyrics earlier: increase the offset we subtract from position.
        self._sync_offset = round(self._sync_offset + SYNC_OFFSET_STEP, 2)
        self._offset_dirty = True
        self._nudge_sync()

    def action_sync_later(self) -> None:
        # Show lyrics later: decrease the offset (can go negative to delay).
        self._sync_offset = round(self._sync_offset - SYNC_OFFSET_STEP, 2)
        self._offset_dirty = True
        self._nudge_sync()

    def _nudge_sync(self) -> None:
        # Record which clock this tuning was made against. Radio dead-reckons
        # the playhead and adds DEFAULT_LEAD_S; MPRIS modes do not. Capturing it
        # here rather than at save time is what makes it correct: by the time
        # the save prompt fires on a track change, self._det already describes
        # the *next* track.
        self._offset_mode = self._det.mode
        hint = " · press S to save" if self._current_track_id is not None else ""
        self.notify(f"Lyric sync offset: {self._sync_offset:+.1f}s{hint}")
        if self._det.is_active and self._timeline.lines:
            self._tick_lyrics()

    def action_save_offset(self) -> None:
        if self._current_track_id is None:
            self.notify("No track to save offset for", severity="warning")
            return
        with localcache.connect() as conn:
            localcache.set_sync_offset(self._current_track_id, self._sync_offset,
                                       conn, mode=self._offset_mode or self._det.mode)
        self._offset_dirty = False
        self.notify(f"Saved offset {self._sync_offset:+.1f}s for this track")
        log.info("saved sync offset %.2f for track %s",
                 self._sync_offset, self._current_track_id)

    def _prompt_save_offset(self, track_id: int, offset: float) -> None:
        """Ask whether to persist an unsaved offset for a track that's ending."""
        def _on_confirm(save: bool | None) -> None:
            if save:
                with localcache.connect() as conn:
                    localcache.set_sync_offset(track_id, offset, conn,
                                               mode=self._offset_mode)
                self.notify(f"Saved offset {offset:+.1f}s")
                log.info("saved sync offset %.2f for track %s (on change)",
                         offset, track_id)

        self.push_screen(
            ConfirmScreen(
                f"Save lyric sync offset {offset:+.1f}s for this track?",
                yes_label="Save", no_label="Discard",
            ),
            _on_confirm,
        )

    def action_cycle_log(self) -> None:
        order = ["off", "err", "info", "full"]
        self._log_level = order[(order.index(self._log_level) + 1) % len(order)
                                if self._log_level in order else 1]
        stream_logs(self._log_level)
        self.query_one("#log-label", Static).update(f"log: {self._log_level}")
        log.warning("log level set to %s", self._log_level)
        self.notify(f"log level: {self._log_level}")

    def _refresh_worker_load(self) -> None:
        """Update the post-processing worker-load read-out (in a thread).

        Uses a NON-blocking CPU delta between successive ticks (no sleep) so the
        timer keeps firing indefinitely, and hits the RabbitMQ management API off
        the UI thread. Best-effort — a failure updates the line but never stops
        future refreshes.
        """
        prev = self._cpu_sample

        def _work() -> None:
            line = "worker-load: (unavailable)"
            panel = "workers  (unavailable)"
            try:
                from . import postprocess_status as ps
                st = ps.get_status(prev_cpu_sample=prev)
                # Persist the fresh sample for the next tick's delta.
                self._cpu_sample = st.cpu_sample
                line = ps.worker_load_line(st)
                panel = ps.worker_panel(st)
            except Exception:
                log.debug("worker-load refresh failed", exc_info=True)
            for selector, text in (("#worker-load", line),
                                   ("#worker-panel", panel)):
                try:
                    self.call_from_thread(
                        self.query_one(selector, Static).update, text
                    )
                except Exception:
                    pass

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("worker-load dispatch failed", exc_info=True)

    def _poll_events(self) -> None:
        """Poll the platform event ledger (Argo Events -> OpenSearch) off the UI thread.

        When Celery post-processing, playback queue hooks, or metadata harvesters
        emit completion events, refresh the active track info, lyrics, and library
        in place — without a destructive page reload.
        """
        last_ts = self._last_event_ts

        def _work() -> None:
            try:
                res = self.api.recent_events(since_ts=last_ts, limit=10)
                items = res.get("events", [])
            except Exception:
                log.debug("poll events failed", exc_info=True)
                return

            if not items:
                return

            newest_ts = max((e.get("ts") for e in items if e.get("ts") is not None), default=last_ts)
            if newest_ts:
                self._last_event_ts = float(newest_ts)

            self.call_from_thread(self._handle_platform_events, items)

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("event worker dispatch failed", exc_info=True)

    def _handle_platform_events(self, events: list[dict[str, Any]]) -> None:
        """Process incoming platform events and update UI components reactively."""
        has_postprocess = False
        has_metadata = False

        for ev in events:
            name = str(ev.get("task_name") or "")
            state = str(ev.get("state") or "").upper()
            if state != "SUCCESS":
                continue

            # 1. Post-processing task completed (analysis, word-sync, vectors)
            if name.startswith("karaoke.tasks."):
                has_postprocess = True

            # 2. Metadata harvesting (Wikibase/Wikimedia entity links, extra tags)
            elif name.startswith("karaoke.metadata.") or "wikibase" in name:
                has_metadata = True

            # 3. Playback queue events (e.g. queue mutated, auto-next queued)
            elif name.startswith("karaoke.playback.") or name.startswith("karaoke.queue."):
                saved = localcache.load_active_queue()
                if saved and saved.get("rows"):
                    self._queue = saved["rows"]
                    self._unfiltered_queue = list(saved["rows"])
                    self._queue_at = saved.get("current_index", 0)
                self._render_queue()

            # 4. YouTube Music playlist sync / remote additions
            elif (
                name.startswith("karaoke.playlist.")
                or "ytmusic_sync" in name
                or name == "karaoke.tasks.add_to_ytmusic_playlist_task"
                or name == "karaoke.tasks.sync_ytmusic_playlist"
            ):
                if getattr(self, "_ytmusic_queue_follow", False):
                    saved = localcache.load_active_queue()
                    if saved and saved.get("rows"):
                        self._queue = saved["rows"]
                        self._unfiltered_queue = list(saved["rows"])
                        self._queue_at = saved.get("current_index", 0)
                        self._ytmusic_video_to_queue_index = self._queue_video_map()
                    self._render_queue()

        if has_postprocess or has_metadata:
            # Re-read active track lyrics & analysis in-place
            self._sync_key = None
            self._poll_detection()
            if not self._det.is_active:
                try:
                    self.load_songs()
                except Exception:
                    pass
            self.notify("Track data updated from background event", severity="information")

    def _library_has_focus(self) -> bool:
        """Whether the library table is the widget the user is driving.

        Distinguishes a deliberate row selection from the table's default
        cursor position, which sits at row 0 whether or not anything was ever
        highlighted.
        """
        try:
            return bool(self.query_one("#library", DataTable).has_focus)
        except Exception:
            return False

    def _selected_song(self) -> SongRow | None:
        try:
            table = self.query_one("#library", DataTable)
        except Exception:
            return None
        if not self._song_data:
            return None
        try:
            row_idx = table.cursor_row
            if 0 <= row_idx < len(self._song_data):
                return self._song_data[row_idx]
        except Exception:
            pass
        return None

    def _show_selected_song(self) -> None:
        # When a player is syncing, the lyrics panel is owned by the clock.
        if self._det.is_active and self._timeline.lines:
            return
        song = self._selected_song()
        try:
            lyrics = self.query_one("#lyrics", Static)
        except Exception:
            return
        lyrics.border_subtitle = None
        if song is None:
            lyrics.update("No songs match this filter yet.")
            self._update_mood("neutral")
            self._update_keybpm(None)
            try:
                self.query_one("#ascii-visual", Static).update("sentiment / rhythm")
            except Exception:
                pass
            return
        preview = lyric_preview(song)
        lyrics.update(preview or "No lyrics cached for this track yet.")

        # Show consensus genre & acoustic sound in border subtitle
        broad_g = song.get("broad_genres") or []
        spec_g = song.get("artist_genres") or []
        audio_g = str(song.get("genre") or "").strip()
        runner = str(song.get("runner_up") or "").strip()
        tags = []
        if broad_g:
            bg_str = "/".join(broad_g)
            if spec_g and spec_g[0].lower() != broad_g[0].lower():
                bg_str = f"{bg_str} ({spec_g[0]})"
            tags.append(f"Genre: {bg_str}")
        if audio_g:
            sound_str = f"{audio_g} ~{runner}" if runner else audio_g
            tags.append(f"Sound: {sound_str}")
        lyrics.border_subtitle = " • ".join(tags) if tags else None

        self._render_visuals(song, preview, self._elapsed)
        self._trigger_postprocess_if_needed(
            str(song.get("artist") or ""),
            str(song.get("title") or ""),
            str(song.get("url") or ""),
        )

    # -- opening / whitelist / controls ----------------------------------
    def action_select(self) -> None:
        if self._filter == "staging":
            self._whitelist_selected()
        else:
            self._open_selected()

    def _whitelist_selected(self) -> None:
        song = self._selected_song()
        if song is None or "staged_id" not in song:
            self.notify("No staged item selected", severity="warning")
            return
        if song.get("status") == "approved":
            self.notify("Already whitelisted", severity="information")
            return
        artist = str(song.get("artist") or "")
        title = str(song.get("title") or "")
        staged_id = int(song["staged_id"])  # type: ignore[arg-type]

        def on_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                staging.whitelist_staged(staged_id)
            except Exception as exc:
                log.exception("whitelist failed for #%s", staged_id)
                self.notify(f"Whitelist failed: {exc}", severity="error")
                return
            log.info("whitelisted staged #%s: %s - %s", staged_id, artist, title)
            self.notify(f"Whitelisted {artist} - {title}")
            self.load_songs()

        self.push_screen(
            ConfirmScreen(f"Whitelist (approve) lyrics for\n{artist} - {title}?"),
            on_confirm,
        )

    def _open_selected(self) -> None:
        song = self._selected_song()
        if song is None:
            self.notify("No selected song", severity="warning")
            return
        url = str(song.get("url") or "")
        kind = str(song.get("kind") or "")
        artist = str(song.get("artist") or "")
        title = str(song.get("title") or "")
        if not url:
            query = quote_plus(f"{artist} {title}".strip())
            if not query:
                self.notify("No URL or search terms for this row", severity="warning")
                return
            url = f"https://www.youtube.com/results?search_query={query}"
            kind = "youtube_search"

        # Close the overlay immediately and open in a background thread. The
        # open goes through CDP, whose calls can block for several seconds each
        # when the kiosk socket is stale (see the TimeoutErrors in the log).
        # Running it on the UI thread froze the whole event loop, so the next
        # `H` (and every other key) was silently dropped until it returned.
        self._hide_browse()
        self.notify(f"Opening {artist} - {title}…")
        self._trigger_postprocess_if_needed(artist, title, url)

        def _bg() -> None:
            try:
                pid = open_song_url(
                    url, kind, artist=artist, title=title, prefer_audio=True)
            except Exception as exc:
                log.exception("KaraokeTui failed to open %s", url)
                self.call_from_thread(
                    self.notify, f"Open failed; see {LOG_FILE}", severity="error")
                self.call_from_thread(
                    lambda: self.query_one("#now-playing", Static).update(f"Open failed: {exc}"))
                return
            suffix = f" (pid {pid})" if pid is not None else ""
            self.call_from_thread(self.notify, f"Opened {artist} - {title}{suffix}")

        self.run_worker(_bg, thread=True)

    def action_cycle_mode(self) -> None:
        idx = MODE_CYCLE.index(self._mode_override)
        self._mode_override = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
        label = self._mode_override or "auto"
        log.info("mode override -> %s", label)
        self.notify(f"Mode: {label}")
        self._sync_key = None  # force a re-resolve
        self._poll_detection()

    def _control_player(self) -> str:
        return self._det.mpris_name if self._det.is_active else ""

    def _resolve_track_id(self, det, artist, title, conn) -> int | None:
        """Best-effort canonical track id for the current detection.

        Prefers a source-URL / video-ID match (reliable for browser tabs), then
        the resolved or raw artist/title. Returns None on a miss.
        """
        if getattr(det, "url", ""):
            found = localcache.find_track_by_url(det.url, conn)
            if found:
                return found[0]
        for a, t in ((artist, title), (det.artist, det.title)):
            if a and t:
                tid = localcache.find_track_id(a, t, conn)
                if tid is not None:
                    return tid
        return None

    def action_play_pause(self) -> None:
        """Toggle play/pause via CDP first or fallback to MPRIS."""
        from . import player_open
        if not player_open.cdp_play_pause():
            if self._det.is_active:
                ok = self.api.player_play_pause(self._control_player())
                if not ok:
                    self.notify("control failed", severity="warning")

    def action_next_track(self) -> None:
        """Skip to next track (via active queue or CDP/MPRIS)."""
        if self._queue and self._queue_at >= 0 and not getattr(self, "_ytmusic_queue_follow", False):
            self.action_queue_next()
            return
        from . import player_open
        if not player_open.cdp_next_track():
            if self._det.is_active:
                self.api.player_next(self._control_player())
        self.notify("Next track")

    def action_previous_track(self) -> None:
        """Skip to previous track (via active queue or CDP/MPRIS)."""
        if (self._play_once and self._queue and self._queue_at > 0
                and not getattr(self, "_ytmusic_queue_follow", False)):
            self.play_queue_index(self._queue_at - 1)
            return
        from . import player_open
        if not player_open.cdp_previous_track():
            if self._det.is_active:
                self.api.player_previous(self._control_player())
        self.notify("Previous track")

    def action_cycle_sort(self) -> None:
        """`Z`: Cycle through available sort modes."""
        sort_keys = [k for k, _ in SORT_OPTIONS]
        try:
            cur_idx = sort_keys.index(self._sort)
        except ValueError:
            cur_idx = 0
        next_idx = (cur_idx + 1) % len(sort_keys)
        self._sort = sort_keys[next_idx]
        try:
            self.query_one("#sort-select", Select).value = self._sort
        except Exception:
            pass
        try:
            self.query_one("#browse-sort-select", Select).value = self._sort
        except Exception:
            pass
        sort_label = dict(SORT_OPTIONS).get(self._sort, self._sort)
        self.notify(f"Sort mode: {sort_label}")
        self._apply_mood_change()

    def action_toggle_av(self) -> None:
        """`V`: Toggle YouTube Music Audio (Song) mode vs Video mode."""
        from . import player_open
        res = player_open.cdp_toggle_av()
        if res == "audio":
            self.notify("Switched to Audio (Song) mode")
            try:
                self.query_one("#btn-av", Button).label = "🎵 Audio"
            except Exception:
                pass
        elif res == "video":
            self.notify("Switched to Video mode")
            try:
                self.query_one("#btn-av", Button).label = "🎬 Video"
            except Exception:
                pass
        elif res == "no_av_toggle":
            self.notify("No Audio/Video toggle on current track", severity="warning")
        else:
            self.notify(f"A/V toggle: {res}")

    def action_toggle_shuffle(self) -> None:
        """Toggle shuffle mode on YouTube Music."""
        from . import player_open
        if player_open.cdp_toggle_shuffle():
            self.notify("Toggled Shuffle")
        else:
            self.notify("Shuffle control unavailable", severity="warning")

    def action_toggle_repeat(self) -> None:
        """Toggle repeat mode on YouTube Music."""
        from . import player_open
        if player_open.cdp_toggle_repeat():
            self.notify("Toggled Repeat")
        else:
            self.notify("Repeat control unavailable", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-play-pause":
            self.action_play_pause()
        elif bid == "btn-next":
            self.action_next_track()
        elif bid == "btn-prev":
            self.action_previous_track()
        elif bid == "btn-av":
            self.action_toggle_av()
        elif bid == "btn-shuffle":
            self.action_toggle_shuffle()
        elif bid == "btn-repeat":
            self.action_toggle_repeat()
        elif bid == "btn-browse-open":
            self.action_select()
        elif bid == "btn-browse-enqueue":
            self.action_enqueue_selected()
        elif bid == "btn-browse-more":
            self.action_more_like_this()
        elif bid == "btn-browse-vibe":
            self.action_lyric_vibe()

    def action_seek_back(self) -> None:
        if getattr(self, "_dance_mode", False):
            self._dance_num_dancers = max(1, self._dance_num_dancers - 1)
            self.notify(f"Dancers: {self._dance_num_dancers}")
            return
        if self._det.is_active:
            self.api.player_seek(-5, self._control_player())

    def action_seek_fwd(self) -> None:
        if getattr(self, "_dance_mode", False):
            self._dance_num_dancers = min(12, self._dance_num_dancers + 1)
            self.notify(f"Dancers: {self._dance_num_dancers}")
            return
        if self._det.is_active:
            self.api.player_seek(5, self._control_player())

    # -- detection + live sync -------------------------------------------
    def _effective_detection(self) -> detect.Detection:
        """Apply the manual mode override on top of auto-detection.

        A live mic identification decides *which song* is playing: it hears the
        room, and MPRIS may be reporting a stale track from some other player.

        It does not decide the *clock*. When a player is playing the song the
        mic just named — the Spotify app, typically — its MPRIS position is
        exact, while radio dead-reckons from songrec's offset plus
        DEFAULT_LEAD_S and drifts until the next re-anchor. In that case hand
        back the player's own detection so the accurate position is used. The
        mic still chose the song; only the clock changed.
        """
        if self._mic_ref is not None and self._mic_ref.title:
            # Pass the mic's track so that when several players are playing,
            # the one actually making the sound in the room is chosen.
            det = detect.detect_active(self._mic_ref.artist, self._mic_ref.title)
            if det.is_active and detect.same_track(
                    det.artist, det.title,
                    self._mic_ref.artist, self._mic_ref.title):
                self._clock_from_mic = True
                return det
            self._clock_from_mic = False
            return detect.Detection(
                mode="radio", player="songrec",
                artist=self._mic_ref.artist, title=self._mic_ref.title,
            )
        self._clock_from_mic = False
        det = detect.detect_active()
        if self._mode_override is None:
            return det
        if self._mode_override == "browse":
            return detect.Detection(mode="browse")
        # spotify / scan forced: keep detection only if it matches, else browse
        if det.is_active and det.mode == self._mode_override:
            return det
        if det.is_active:
            # a player is active but not the forced kind; still sync it
            return det
        return detect.Detection(mode="browse")

    def _poll_detection(self) -> None:
        try:
            mode_label = self.query_one("#mode-label", Static)
            now = self.query_one("#now-playing", Static)
        except Exception:
            return
        det = self._effective_detection()
        self._det = det
        override = f" (forced {self._mode_override})" if self._mode_override else " (auto)"
        # The mic identified the song but a player is supplying the position.
        # Say so, or it looks like the mic quietly switched itself off.
        if self._clock_from_mic:
            override = " (mic)"
        if not det.is_active:
            mode_label.update(f"Mode: browse{override}")
            self._timeline = LyricTimeline([])
            self._sync_key = None
            now.update(
                "Nothing playing.\nPick a song and press Enter to open it "
                "in the browser, or start Spotify / a YouTube tab."
            )
            self._show_selected_song()
            return

        key = (det.artist.lower(), det.title.lower())
        mode_label.update(f"Mode: {det.mode}{override} · {det.player or 'player'}")
        if key == self._sync_key:
            return

        # Track changed: if the previous track has unsaved offset edits, ask to
        # save them before we load the new track's offset.
        if self._offset_dirty and self._current_track_id is not None:
            self._prompt_save_offset(self._current_track_id, self._sync_offset)
            self._offset_dirty = False

        self._sync_key = key
        with localcache.connect() as conn:
            self._check_playlist_detection(det, conn)
            artist, title, lyrics = detect.resolve_lyrics(det, conn)
            # Resolve the canonical track id and load any saved per-track offset,
            # falling back to the session default when none has been saved.
            self._current_track_id = self._resolve_track_id(det, artist, title, conn)
            self._sync_queue_to_detection(det, self._current_track_id)
            saved = (
                localcache.get_sync_offset(self._current_track_id, conn, det.mode)
                if self._current_track_id is not None else None
            )
            self._sync_offset = saved if saved is not None else _default_sync_offset(det.mode)
            self._offset_dirty = False
            self._offset_mode = det.mode
            # Needed by mic_elapsed to wrap the dead-reckoned playhead on repeat.
            self._track_duration = self._load_duration(self._current_track_id, conn)
            # Radio identifies songs MPRIS never sees, so this is where a
            # Spotify link is worth resolving — once per track, and only while
            # the mic is actually running, so browsing costs no API quota.
            if (self._mic_running() and not self._spotify_off
                    and localcache.spotify_lookup_due(self._current_track_id, conn)):
                self.run_worker(
                    lambda tid=self._current_track_id, a=artist, t=title:
                        self._background_fetch_spotify(tid, a, t),
                    exclusive=False, thread=True, exit_on_error=False,
                )
            if lyrics is None or not lyrics.has_synced:
                # The cache is only what has already been fetched. Radio mode
                # calls get_synced, which hits LRCLIB on a miss and caches the
                # result; the TUI used to read the cache and stop, so a track
                # nobody had fetched yet showed as having no lyrics forever.
                if key not in self._lyrics_fetched:
                    self._lyrics_fetched.add(key)
                    self.run_worker(
                        lambda a=det.artist, t=det.title: self._background_fetch_lyrics(a, t),
                        exclusive=False,
                        thread=True,
                        exit_on_error=False,
                    )
                if key not in self._gap_logged:
                    detect.record_gap(det, conn)
                    self._gap_logged.add(key)
                    log.info("no lyrics; queued gap: %s - %s", det.artist, det.title)
                
                # Auto-autoload captions in background if URL is YouTube/YT Music
                if det.url and (detect.is_youtube_url(det.url) or "music.youtube.com" in det.url.lower()):
                    from .localcache import extract_youtube_id
                    vid = extract_youtube_id(det.url)
                    if vid and vid not in self._autoloaded:
                        self._autoloaded.add(vid)
                        self.run_worker(
                            lambda u=det.url, v=vid: self._background_autoload_captions(u, v),
                            exclusive=False,
                            thread=True,
                            exit_on_error=False,
                        )
        display_artist = artist or det.artist
        display_title = title or det.title
        current_song = {"artist": display_artist, "title": display_title}
        # Remembered so `A` can post-process whatever is playing without
        # re-resolving it.
        self._current_song = (display_artist, display_title, det.url or "")
        self._refresh_cover_art(det.url or "")
        if lyrics is not None and (lyrics.synced_raw or lyrics.plain):
            self._last_error = ""      # resolved fine; drop any stale error
        self._refresh_track_info(lyrics)
        keybpm_line = self._format_keybpm_line(current_song)
        # Enqueue background post-processing (key/BPM analysis, word-timing upgrade)
        # if this track is missing derived assets. Best-effort; no-op if the
        # RabbitMQ broker is unreachable.
        # Fill in genre and tone for a track that has neither. Cheap work only:
        # autoclassify will not start a recording, so a Spotify track with no
        # downloadable audio still needs `k`.
        if (self._current_track_id is not None
                and self._current_track_id not in self._classified):
            self._classified.add(self._current_track_id)
            self.run_worker(
                lambda tid=self._current_track_id: self._background_classify(tid),
                exclusive=False, thread=True, exit_on_error=False,
            )

        self._trigger_postprocess_if_needed(display_artist, display_title, det.url or "")
        state = lyric_display_state(lyrics)
        # The banner is what is playing, so it is drawn whatever the lyrics
        # turn out to be. It used to appear only on the synced branch, which
        # meant the title silently dropped to a one-line "artist - title" for
        # every track without timings -- the state a track is in before any
        # work has been done on it, and the one you look at most.
        size = now.content_size
        banner = self.title_banner(display_artist, display_title,
                                   size.width or 0, (size.height or 0) - 1)
        if state == "synced":
            self._timeline = timeline_from_lyrics(lyrics)
            try:
                lyrics_widget = self.query_one("#lyrics", Static)
                lyrics_widget.border_subtitle = f"synced · {lyrics.source if lyrics else ''}"
            except Exception:
                pass
            now.update(
                f"{banner}\n"
                f"{det.mode} · {det.player or '—'} · {keybpm_line} · "
                f"{lyrics.source} · offset {self._sync_offset:+.1f}s (, / .)"
            )
        elif state == "unsynced":
            # Words without timings. Previously this fell through to "no
            # lyrics", which was wrong in a way that hid real text: the player
            # showed the song's words while the TUI claimed to have none and
            # queued it for backfill it did not need. There is nothing to
            # highlight, so they are shown plainly and labelled as unsynced.
            self._timeline = LyricTimeline([])
            lyrics_widget = self.query_one("#lyrics", Static)
            lyrics_widget.border_subtitle = f"unsynced · {lyrics.source}"
            lyrics_widget.update(lyrics.plain.strip())
            now.update(
                f"{banner}\n"
                f"{det.mode} · {det.player or '—'} · {keybpm_line} · "
                f"{lyrics.source} · words only, no timings"
            )
        else:
            self._timeline = LyricTimeline([])
            lyrics_widget = self.query_one("#lyrics", Static)
            lyrics_widget.border_subtitle = None
            lyrics_widget.update(
                f"No lyrics for {display_artist} - {display_title}.\n"
                "Added to the staging/backfill queue — run karaoke-stage or "
                "karaoke-backfill, then whitelist it in the Staging view."
            )
            now.update(
                f"{banner}\n"
                f"{det.mode} · {det.player or '—'} · {keybpm_line} · "
                "no lyrics — queued for staging"
            )

    def _tick_lyrics(self) -> None:
        if not self._det.is_active:
            return
        # Radio mode has no MPRIS player to ask; the playhead is dead-reckoned
        # from where songrec last heard us.
        pos = None
        if self._det.mode == "radio":
            pos = self.mic_elapsed()
        else:
            try:
                b_state = browser_playback(timeout=0.08)
            except TypeError:
                b_state = browser_playback()
            if b_state and b_state.get("present") and b_state.get("position") is not None:
                pos = float(b_state["position"])
            else:
                try:
                    pos = playerctl.position(self._control_player(), timeout=0.15)
                except TypeError:
                    pos = playerctl.position(self._control_player())
        if pos is None:
            return
        # Pull the highlight back by the sync offset: browser MPRIS position runs
        # ahead of audible output, so lyrics would otherwise lead the sound.
        elapsed = max(0.0, pos - self._sync_offset)
        self._elapsed = elapsed
        if self._timeline.lines:
            self._render_synced(elapsed)      # also refreshes the visuals
            return
        # No synced lyrics for this track -- common in Spotify mode -- but the
        # beat animation only needs BPM and elapsed time. Gating it on the
        # timeline froze the rhythm bar and cartwheel while the BPM read-out
        # beside them kept updating, which just looked broken.
        self._render_visuals(self._current_song_row(), "", elapsed)

    def _render_synced(self, elapsed: float) -> None:
        try:
            lyrics_widget = self.query_one("#lyrics", Static)
        except Exception:
            return
        tl = self._timeline
        active = tl.active_index(elapsed)
        mood = mood_of(tl.lines[active][1]) if active >= 0 else "neutral"
        # Fill the panel. _render_body defaults to 8 lines total, which left
        # most of a full-height pane empty. Weighted towards what is coming up,
        # and the active line is kept off the very top and bottom edges.
        rows = max(0, lyrics_widget.content_size.height)
        before, after = (3, 5) if rows < 10 else (rows // 3, rows - rows // 3)
        body = Text()
        _render_body(body, tl, elapsed, mood=mood, before=before, after=after)
        lyrics_widget.update(body)
        
        nxt = tl.next_time(elapsed)
        lyrics_widget.border_subtitle = f"next in {nxt - elapsed:0.1f}s" if nxt else "(end)"
        if mood != self._sync_mood:
            self._sync_mood = mood
            self._update_mood(mood)
        # Refresh the sentiment/rhythm read-out against the playing track.
        preview = "\n".join(t for _, t in tl.lines)
        self._render_visuals(self._current_song_row(), preview, elapsed)

    def _current_song_row(self) -> SongMapping:
        """Best-effort song row for the currently-syncing track (for visuals)."""
        return {"artist": self._det.artist, "title": self._det.title}

    # -- visuals ----------------------------------------------------------
    def _update_mood(self, mood: str) -> None:
        """Show the mood, as a picture when one has been rendered for it.

        The mood *word* stays above the image. The picture carries the feeling,
        but it should not be the only thing naming it -- the word is what makes
        a wrong match obviously wrong rather than merely odd, and it is the only
        thing that still works on a terminal without colour.
        """
        try:
            square_panel = self.query_one("#mood-square", Static)
            label_panel = self.query_one("#mood-label", Static)
        except Exception:
            return
        
        if mood != self._mood_shown or self._mood_art is None:
            if mood != self._mood_shown:
                self._mood_shown = mood
                self._refresh_mood_art(mood)
                
        import pyfiglet
        from rich.text import Text
        fig_str = pyfiglet.figlet_format(mood.upper(), font="mini").rstrip()
        label_panel.update(Text(fig_str, style="bold cyan"))
                
        if self._mood_art is None:
            # Fall back to glyphs until rendering finishes
            square_panel.update(MOOD_GLYPHS.get(mood, MOOD_GLYPHS['neutral']))
            return

        square_panel.update(self._mood_art)

    def _refresh_mood_art(self, mood: str) -> None:
        """Render a picture for this mood in a worker thread.

        Scoring the cover pool costs a handful of ffmpeg calls, so this runs on
        a mood change only -- never on the 0.2s lyric tick, which is what reads
        the result.
        """
        def _work() -> None:
            from . import coverart, moodframe
            try:
                panel = self.query_one("#mood-square", Static)
                cols = max(0, panel.content_size.width)
                rows = max(0, panel.content_size.height - 1)   # the mood word
                if cols < 4 or rows < 2:
                    return
                analysis = self._lookup_analysis(self._current_song_row())
                pixels, source = moodframe.image_for(mood, analysis, cols, rows)
                if not pixels:
                    return
                self._mood_pixels = pixels
                self._mood_art = coverart.to_text(pixels)
                self._mood_source = source
                self.call_from_thread(self._update_mood, mood)
            except Exception:
                log.debug("mood art refresh failed", exc_info=True)

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("mood art dispatch failed", exc_info=True)

    def _update_keybpm(self, song: SongMapping | None) -> None:
        try:
            panel = self.query_one("#keybpm", Static)
        except Exception:
            return
        if song is None:
            panel.update("key: —\nbpm: —")
            return
        analysis = self._lookup_analysis(song)
        if analysis is None:
            panel.update(
                "key: not analysed\nbpm: —\n"
                "(make install-audio, then\n karaoke-analyze)"
            )
            return
        key = analysis.resolved_key or analysis.detected_key
        key_line = key.name if key else "unknown"
        extras = []
        if key:
            extras.append(f"camelot {key.camelot}")
        if analysis.key_relation in ("relative", "parallel", "conflict"):
            extras.append(analysis.key_relation)
        bpm_line = f"{analysis.bpm:.0f} bpm · {visuals.tempo_word(analysis.bpm)}" \
            if analysis.bpm else "bpm —"
        conf = f"conf {analysis.key_confidence:.0%} {analysis.key_agreement}" \
            if analysis.key_confidence else ""
        panel.update(
            f"key: {key_line}"
            + (f"  ({', '.join(extras)})" if extras else "")
            + f"\n{bpm_line}\n{conf}"
        )

    def _lookup_analysis(self, song: SongMapping):
        raw_id = song.get("track_id")
        try:
            with localcache.connect() as conn:
                if raw_id is None:
                    resolved = localcache.find_track_id(
                        str(song.get("artist") or ""),
                        str(song.get("title") or ""),
                        conn,
                    )
                else:
                    resolved = int(str(raw_id))
                if resolved is None:
                    return None
                return track_analysis.get_analysis(int(resolved), conn)
        except Exception:
            return None

    def _format_keybpm_line(self, song: SongMapping | None) -> str:
        """Compact key/BPM line for the now-playing panel."""
        analysis = self._lookup_analysis(song) if song else None
        if analysis is None:
            return "key: not analysed  bpm: —"
        key = analysis.resolved_key or analysis.detected_key
        key_text = key.name if key else "unknown"
        bpm_text = f"{analysis.bpm:.0f}" if analysis.bpm else "—"
        return f"key: {key_text}  bpm: {bpm_text}"

    def _render_visuals(self, song: SongMapping | None, preview: str,
                        elapsed: float) -> None:
        profile = visuals.analyze_sentiment(preview or "")
        if not (self._det.is_active and self._timeline.lines):
            self._update_mood(profile.dominant)
        self._update_keybpm(song)
        analysis = self._lookup_analysis(song) if song else None
        bpm = analysis.bpm if analysis else None
        energy = analysis.energy if analysis else None
        brightness = analysis.brightness if analysis else None
        genre = ""
        if song:
            genre = str(song.get("genre") or "")
        if not genre and self._current_track_id is not None:
            try:
                with localcache.connect() as conn:
                    genre = str(localcache.genre_for(self._current_track_id, conn) or "")
            except Exception:
                pass

        if getattr(self, "_mood_pixels", None):
            try:
                from rich.text import Text
                from . import coverart
                import pyfiglet
                
                animated_pixels = visuals.animate_mood_pixels(self._mood_pixels, elapsed, bpm)
                animated_text = coverart.to_text(animated_pixels)
                
                # Generate ASCII art text, strip trailing newlines
                fig_str = pyfiglet.figlet_format(self._mood_shown.upper(), font="mini").rstrip()
                label = Text(fig_str, style="bold cyan")
                
                self.query_one("#mood-label", Static).update(label)
                self.query_one("#mood-square", Static).update(animated_text)
            except Exception as e:
                import logging
                logging.getLogger("karaoke").error(f"Error animating mood square: {e}", exc_info=True)
        arc = visuals.sentiment_arc(profile)
        bars = visuals.sentiment_bars(profile)
        rhythm = visuals.rhythm_bar(bpm, elapsed)

        # Dance mode: full-screen dance floor replaces the normal visuals.
        if self._dance_mode:
            try:
                visual_panel = self.query_one("#ascii-visual", Static)
                pw = visual_panel.content_size.width or visual_panel.region.width or 80
                ph = visual_panel.content_size.height or visual_panel.region.height or 20
                dance = visuals.dance_floor_frame(
                    bpm,
                    elapsed,
                    width=max(20, pw),
                    height=max(6, ph),
                    num_dancers=self._dance_num_dancers,
                    mood=profile.dominant,
                    genre=genre,
                    energy=energy,
                )
                visual_panel.update(dance)
            except Exception:
                pass
            return

        duet_width = 32
        try:
            visual_panel = self.query_one("#ascii-visual", Static)
            duet_width = max(12, min(32, visual_panel.content_size.width or visual_panel.region.width))
        except Exception:
            pass
        duet = visuals.duet_cartwheel_frame(
            bpm,
            elapsed,
            mood=profile.dominant,
            genre=genre,
            energy=energy,
            brightness=brightness,
            width=duet_width,
        )
        try:
            self.query_one("#ascii-visual", Static).update(
                f"sentiment arc\n{arc}\n\n{bars}\n\nrhythm\n{rhythm}\n\n{duet}"
            )
        except Exception:
            pass

    def cover_source(self, url: str = "") -> "os.PathLike | None":
        """Where to get artwork for the current track.

        Prefers the player's own MPRIS art, which browsers have already written
        to a local file — no download, and it is the cover the user is looking
        at. Falls back to the cached YouTube media, whose first frame stands in
        when there is no cover.
        """
        from . import coverart

        # resolve_art, not art_path_from_url: the Spotify app publishes a
        # remote https artUrl where Chromium publishes a local file, so
        # local-only resolution left Spotify with no thumbnail at all.
        path = coverart.resolve_art(playerctl.art_url(self._control_player()) or "")
        if path is not None:
            return path
        vid = localcache.extract_youtube_id(url or "")
        if not vid:
            return None
        from .config import settings
        for ext in (".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg"):
            candidate = Path(settings.youtube_dir) / f"{vid}{ext}"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _load_duration(track_id: "int | None", conn) -> "float | None":
        """Stored track length in seconds, or None when unknown."""
        if track_id is None:
            return None
        row = conn.execute("SELECT duration FROM tracks WHERE track_id = %s",
                           (track_id,)).fetchone()
        return float(row["duration"]) if row and row["duration"] else None

    def _refresh_track_info(self, lyrics) -> None:
        """Fill the read-out under the cover art. Best-effort and never fatal."""
        try:
            pending = None
            duration = None
            genre = ""
            sound = ""
            if self._current_track_id is not None:
                from .postprocess_queue import needs_postprocessing
                with localcache.connect() as conn:
                    pending = needs_postprocessing(self._current_track_id, conn)
                    duration = self._load_duration(self._current_track_id, conn)
                    # 1. Consensus artist genres
                    art_row = conn.execute(
                        "SELECT artist FROM tracks WHERE track_id = %s",
                        (self._current_track_id,),
                    ).fetchone()
                    if art_row and art_row["artist"]:
                        spec_g, broad_g = localcache.get_artist_genres(art_row["artist"], conn)
                        if broad_g:
                            genre = "/".join(broad_g)
                            if spec_g and spec_g[0].lower() != broad_g[0].lower():
                                genre = f"{genre} ({spec_g[0]})"
                    # 2. CLAP acoustic sound / timbre
                    acoustic = classification_line(
                        localcache.genre_for(self._current_track_id, conn),
                        localcache.tone_for(self._current_track_id, conn))
                    if acoustic:
                        sound = f"{acoustic} (acoustic)"
                    if not genre and acoustic:
                        genre = acoustic
            text = track_info(
                source=(lyrics.source if lyrics else ""),
                duration=duration,
                offset=self._sync_offset,
                pending=pending,
                lyric_lines=len(self._timeline.lines),
                error=self._last_error,
                genre=genre,
                sound=sound,
            )
        except Exception as exc:
            log.debug("track info refresh failed", exc_info=True)
            text = track_info(error=str(exc)[:40])
        try:
            self.query_one("#track-info", Static).update(text)
        except Exception:
            pass

    def _refresh_cover_art(self, url: str = "") -> None:
        """Render cover art into the left column, in a worker thread.

        ffmpeg is a subprocess, so this stays off the UI thread and only runs
        on a track change rather than every poll.
        """
        def _work() -> None:
            from . import coverart
            try:
                panel = self.query_one("#beat-art", Static)
                cols = max(0, panel.content_size.width)
                rows = max(0, panel.content_size.height)
                source = self.cover_source(url)
                art = None
                # Width alone gates this. Requiring rows > 1 was circular
                # once the box began following its content: an empty Static is
                # one row tall, so the art would never draw and the box would
                # never grow.
                if source is not None and cols > 3:
                    # Sized from the width alone. The panel is auto-height now,
                    # so taking its height as a limit would be circular -- a
                    # small box would draw small art, which would keep the box
                    # small. coverart.render derives the rows from the width
                    # and the cell aspect.
                    art = coverart.render(source, cols)
                if art is not None:
                    # Keep it while the source still resolves. Spotify's image
                    # URLs expire and YouTube thumbnails change with
                    # re-uploads, so this is the only moment the art is
                    # reliably obtainable.
                    self._remember_cover(source, url)
                elif cols > 3:
                    # The source is gone or unreadable; a kept copy is what
                    # makes that recoverable rather than simply blank.
                    art = self._stored_cover(cols, rows)
                self.call_from_thread(panel.update, art if art is not None else "")
            except Exception as exc:
                log.debug("cover art refresh failed", exc_info=True)
                self._last_error = f"cover art: {exc}"[:60]

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("cover art dispatch failed", exc_info=True)

    def _remember_cover(self, source, url: str) -> None:
        """Keep the art that just rendered, so it outlives its URL.

        Uses the track id the poll loop already resolved. An earlier version
        resolved it again in a helper named ``_current_track_id`` -- which is
        the name of the attribute holding it, so the attribute shadowed the
        method and calling it raised ``'NoneType' object is not callable``.
        """
        from . import cover_store

        track_id = self._current_track_id
        if track_id is None or source is None:
            return
        try:
            with localcache.connect() as conn:
                cover_store.ensure_table(conn)
                if cover_store.grid_for_track(track_id, conn) is not None:
                    return          # already kept; sampling again buys nothing
                cover_store.capture(track_id, source, conn, source_url=url)
        except Exception:
            log.debug("could not store cover art", exc_info=True)

    def _stored_cover(self, cols: int, rows: int):
        """Art from the database, for when the source no longer resolves."""
        from . import cover_store, coverart

        track_id = self._current_track_id
        if track_id is None:
            return None
        try:
            with localcache.connect() as conn:
                # Same reasoning as the live path: width drives the size,
                # not the panel height, which now follows the content.
                return cover_store.render_for_track(
                    track_id, cols, max(1, int(cols / coverart.CELL_ASPECT)),
                    conn)
        except Exception:
            log.debug("could not read stored cover art", exc_info=True)
            return None

    def _background_fetch_spotify(self, track_id: int, artist: str,
                                  title: str) -> None:
        """Resolve a Spotify URI for a track and store it, in a worker thread.

        Runs at most once per track: a hit is cached as a ``spotify`` source and
        a miss in ``spotify_lookups``, and ``spotify_lookup_due`` consults both.
        Search is the rate-limited endpoint and this project has already lost a
        day of API access to calling it in a loop, so the guards matter more
        than the feature.
        """
        if self._spotify_off or not (artist and title):
            return
        from .spotify_client import (SpotifyAuthError, SpotifyClient,
                                     SpotifyRateLimited)
        try:
            uri = SpotifyClient().search_track(artist, title)
        except SpotifyRateLimited as exc:
            # Not a miss. Recording it would cache a false negative that
            # spotify_lookup_due would then honour permanently.
            self._spotify_off = True
            log.warning("spotify rate limited; lookups off for this session: %s", exc)
            self.call_from_thread(
                self.notify,
                f"Spotify rate limited; retry in {exc.retry_after // 60}min",
                severity="warning")
            return
        except SpotifyAuthError as exc:
            self._spotify_off = True
            log.warning("spotify auth failed; lookups off: %s (see: make auth-status)", exc)
            return
        except Exception:
            log.debug("spotify lookup failed for %s - %s", artist, title,
                      exc_info=True)
            return

        try:
            with localcache.connect() as conn:
                if uri:
                    localcache.add_track_source(artist, title, url=uri,
                                                kind="spotify", conn=conn)
                localcache.record_spotify_lookup(track_id, uri, conn)
        except Exception:
            log.debug("storing spotify lookup failed", exc_info=True)
            return
        if uri:
            log.info("spotify source stored for %s - %s", artist, title)

    def _background_fetch_lyrics(self, artist: str, title: str) -> None:
        """Fetch lyrics from LRCLIB for a cache miss, in a worker thread.

        The detection path is cache-only by design (it runs on a 1.5s timer and
        must not block on the network). This is the missing other half: on a
        miss, go and get them once, cache them, and clear the sync key so the
        next poll picks the track up with lyrics attached.
        """
        if not (artist and title):
            return
        try:
            from .lyrics import clean_title, fetch_lrclib

            found = None
            for candidate in {title, clean_title(title)}:
                ly = fetch_lrclib(artist, candidate, timeout=3.0)
                if ly.synced_raw or ly.plain:
                    found = ly
                    break
            if found is None:
                # LRCLIB does not have everything. The player may already be
                # showing the words in its own lyrics tab -- that panel is the
                # only source that knows about a track nobody has indexed.
                found = self._panel_lyrics(artist, title)
            if found is None:
                return
            with localcache.connect() as conn:
                localcache.add_track_and_lyrics(artist, title, found, conn=conn)
                # Words without timings cannot drive a session. Whisper can
                # supply the rhythm they lack -- but only from the whole track,
                # so this goes to the worker rather than being done here.
                if not found.synced_raw and found.plain:
                    self._enqueue_sync(artist, title, conn)
            log.info("fetched lyrics for %s - %s (%s)", artist, title, found.source)
            # Force the next poll to re-resolve rather than short-circuit on an
            # unchanged key.
            self._sync_key = None
        except Exception as exc:
            log.debug("background lyric fetch failed", exc_info=True)
            self._last_error = f"lyric fetch: {exc}"[:60]

    def _enqueue_sync(self, artist: str, title: str, conn) -> None:
        """Ask the worker to give plain lyrics a rhythm.

        Only when there is audio to align against: the lyrics panel answers for
        plenty of tracks whose audio cannot be fetched, and queueing those would
        fail on every retry the way Spotify-only analysis used to.
        """
        from .postprocess_queue import (enqueue_if_needed,
                                        has_downloadable_source)

        track_id = localcache.find_track_id(artist, title, conn)
        if track_id is None or not has_downloadable_source(track_id, conn):
            log.debug("no fetchable audio to sync %s - %s against", artist, title)
            return
        row = conn.execute(
            "SELECT url FROM sources WHERE track_id = %s AND url LIKE '%%youtu%%'"
            " LIMIT 1", (track_id,)).fetchone()
        if enqueue_if_needed(artist, title, (row["url"] if row else "") or "", conn):
            log.info("queued %s - %s for lyric alignment", artist, title)
            self.call_from_thread(
                self.notify, f"Queued {title} for lyric timing")

    def _panel_lyrics(self, artist: str, title: str):
        """Lyrics from the player's own lyrics tab, as a Lyrics object.

        Unsynced by nature -- LyricFind supplies words, not timings -- so these
        are stored as plain text and become a candidate for alignment against a
        transcription, which is what turns them into something singable.
        """
        from . import ytmusic_lyrics
        from .lyrics import Lyrics

        try:
            kind, panel = ytmusic_lyrics.capture_for_playing(artist, title)
        except Exception:
            log.debug("lyrics panel read failed", exc_info=True)
            return None
        if panel is None:
            return None
        if kind == "biography":
            self._store_panel_note(artist, title, panel)
            return None
        log.info("lyrics panel supplied %d lines for %s - %s (%s)",
                 len(panel.lines), artist, title, panel.attribution)
        return Lyrics(plain="\n".join(panel.lines),
                      source=ytmusic_lyrics.lyrics_source(panel))

    def _store_panel_note(self, artist: str, title: str, panel) -> None:
        """Keep the artist biography the panel showed instead of lyrics.

        This text used to be discarded once it had been recognised as not being
        lyrics, which cost a band history for every track that had one. It is
        not singable, so it never touches the lyrics table -- but it is exactly
        what a semantic search for a half-remembered band should find.
        """
        from . import localcache

        try:
            conn = localcache.connect()
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id is None:
                log.debug("no track row for %r - %r; biography not stored",
                          artist, title)
                return
            if localcache.record_note(track_id, "biography", panel.text,
                                      "ytmusic_panel", conn):
                log.info("stored biography note (%d chars) for %s - %s",
                         len(panel.text), artist, title)
        except Exception:
            log.debug("could not store panel note", exc_info=True)

    def _background_autoload_captions(self, url: str, vid: str) -> None:
        """Fetch, stage, and auto-approve YouTube captions in a worker thread.

        Runs via run_worker(thread=True); all UI mutations are marshalled back
        onto the Textual event loop with call_from_thread.
        """
        self.call_from_thread(
            self.notify, f"Autoloading captions for {vid}…", severity="information"
        )
        try:
            from .stage_sources import stage_youtube_captions
            from . import staging

            result = stage_youtube_captions(url)
            # Only auto-approve when captions carry real timing (synced/enhanced).
            if result and caption_is_synced(result.source_kind) and result.lines > 0:
                staging.whitelist_staged(result.staged_id)
                self.call_from_thread(
                    self.notify,
                    f"Auto-loaded synced captions for {result.artist} - {result.title}",
                    severity="information",
                )
                log.info("Autoloaded and approved captions for %s (staged_id=%s)",
                         url, result.staged_id)
                # Re-poll so the newly approved lyrics load into the timeline now.
                self._sync_key = None
                self.call_from_thread(self._poll_detection)
            else:
                log.warning("Autoload: staged captions not synced for %s (kind=%s)",
                            url, result.source_kind if result else "none")
                self.call_from_thread(
                    self.notify,
                    "Autoload: only unsynced captions found; left in staging queue.",
                    severity="warning",
                )
        except Exception:
            log.exception("Background autoload failed for %s", url)
            self.call_from_thread(
                self.notify,
                "Autoload: no usable captions found on YouTube.",
                severity="warning",
            )


# -- pure helpers (unit-tested) ------------------------------------------
def caption_is_synced(source_kind: str) -> bool:
    """True when a staged caption source_kind carries real timing.

    Caption source kinds look like ``youtube_caption_manual_en-US_enhanced``
    or ``..._synced`` (timed) vs ``..._plain`` (no timing). Only timed captions
    are worth auto-approving into the live timeline.
    """
    return "_synced" in source_kind or "_enhanced" in source_kind


def first_nonempty_line(text: str) -> str:
    """Return the first non-empty line from a block of text."""
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def lyric_preview(song: SongMapping, *, max_lines: int = 12) -> str:
    """Build a compact lyric preview from synced or plain cached lyrics."""
    synced = str(song.get("synced_lyrics") or "")
    plain = str(song.get("plain_lyrics") or "")
    source = synced or plain
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[:max_lines])


def mood_for_preview(preview: str) -> str:
    """Pick the first non-neutral mood in a lyric preview."""
    for line in preview.splitlines():
        mood = mood_of(line)
        if mood != "neutral":
            return mood
    return "neutral"


def tui_main() -> int:
    """Run the player-aware karaoke TUI."""
    import os
    KaraokeTui(log_level=os.environ.get("KARAOKE_LOG", "err")).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(tui_main())
