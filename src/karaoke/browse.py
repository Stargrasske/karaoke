"""An interactive, terminal-based browser for the karaoke song library with Mood Slider."""
from __future__ import annotations
import subprocess
from typing import Any
from urllib.parse import quote_plus
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable, Static
from textual import log as textual_log

from . import localcache
from .logger import LOG_FILE, OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log
from .player_open import open_song_url

__all__ = ["open_song_url", "KaraokeBrowser", "browse_main"]


MOOD_PRESETS = [
    ("All Moods (0%)", 0.0, 1.0),
    ("Chill / Mellow (0% - 40%)", 0.0, 0.4),
    ("Groovy / Upbeat (40% - 75%)", 0.4, 0.75),
    ("High Energy / Anthem (75% - 100%)", 0.75, 1.0),
]


class KaraokeBrowser(App):
    """A Textual app to browse the karaoke song library with mood filtering."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("enter", "select_song", "Play Song"),
        ("[", "decrease_mood", "Mood -"),
        ("]", "increase_mood", "Mood +"),
        ("m", "cycle_mood_mode", "Preset"),
    ]

    def __init__(self):
        super().__init__()
        from .api_client import ApiClient
        self.api = ApiClient()
        self._all_songs = []
        self._visible_songs = []
        self._mood_level = 0.0  # 0.0 to 1.0
        self._mood_preset_idx = 0  # 0 = All

    @property
    def _song_data(self) -> list[dict[str, Any]]:
        """Backwards compatibility property for tests and external callers."""
        return self._visible_songs or self._all_songs

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        yield Static("[bold cyan]Mood Slider:[/bold cyan] Use [bold yellow]'[' / ']'[/bold yellow] to adjust Energy threshold | [bold yellow]'m'[/bold yellow] to cycle presets", id="mood-bar")
        yield Static("Energy Threshold: 0% (Showing All Tracks)", id="mood-label")
        yield DataTable(cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app is first mounted."""
        table = self.query_one(DataTable)
        table.add_columns("Artist", "Title", "Key", "BPM", "Energy")
        self.load_songs()

    def load_songs(self) -> None:
        """Load songs using ApiClient or SQLite fallback."""
        tracks = self.api.list_tracks(limit=5000)
        self._all_songs = []
        if tracks:
            for t in tracks:
                energy = t.get('energy')
                if energy is None and t.get('bpm'):
                    bpm = float(t.get('bpm') or 120)
                    energy = min(1.0, max(0.2, (bpm - 60.0) / 100.0))
                elif energy is None:
                    energy = 0.5
                self._all_songs.append({
                    'artist': t.get('artist', ''),
                    'title': t.get('title', ''),
                    'url': t.get('url', ''),
                    'kind': t.get('kind', ''),
                    'key': t.get('key', '') or '—',
                    'bpm': f"{t.get('bpm'):.0f}" if t.get('bpm') else '—',
                    'energy': float(energy or 0.5),
                })
        else:
            with localcache.connect() as conn:
                from .track_analysis import ensure_schema
                ensure_schema(conn)
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT t.artist, t.title, s.url, s.kind, a.detected_key AS key, a.bpm, a.energy
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
                    LEFT JOIN track_analysis a ON a.track_id = t.track_id
                    ORDER BY t.artist, t.title
                    """
                )
                for row in cur.fetchall():
                    energy = row['energy']
                    if energy is None and row['bpm']:
                        bpm = float(row['bpm'] or 120)
                        energy = min(1.0, max(0.2, (bpm - 60.0) / 100.0))
                    elif energy is None:
                        energy = 0.5
                    self._all_songs.append({
                        'artist': row['artist'],
                        'title': row['title'],
                        'url': row['url'],
                        'kind': row['kind'],
                        'key': row['key'] or '—',
                        'bpm': f"{row['bpm']:.0f}" if row['bpm'] else '—',
                        'energy': float(energy or 0.5),
                    })

        self._filter_and_update_table()

    def _filter_and_update_table(self) -> None:
        """Filter tracks in real-time based on mood threshold/preset and refresh table."""
        table = self.query_one(DataTable)
        table.clear()
        self._visible_songs = []

        min_e, max_e = 0.0, 1.0
        if self._mood_preset_idx > 0:
            _, min_e, max_e = MOOD_PRESETS[self._mood_preset_idx]
        else:
            min_e = self._mood_level

        pct = int(self._mood_level * 100)
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))

        try:
            label_widget = self.query_one("#mood-label", Static)
            if hasattr(label_widget, "update"):
                if self._mood_preset_idx > 0:
                    preset_name = MOOD_PRESETS[self._mood_preset_idx][0]
                    label_widget.update(f"[bold green]Preset:[/bold green] {preset_name} [{bar}]")
                else:
                    label_widget.update(f"[bold yellow]Energy Threshold:[/bold yellow] {pct}% [{bar}] (Showing songs >= {pct}% energy)")
        except Exception:
            pass

        for song in self._all_songs:
            e = song['energy']
            if min_e <= e <= max_e:
                self._visible_songs.append(song)
                e_pct = f"{int(e * 100)}%"
                e_bar = "🔥" if e >= 0.75 else "⚡" if e >= 0.4 else "🌙"
                table.add_row(
                    song['artist'],
                    song['title'],
                    song['key'],
                    song['bpm'],
                    f"{e_bar} {e_pct}",
                )

    def action_decrease_mood(self) -> None:
        """Decrease mood energy threshold by 10%."""
        self._mood_preset_idx = 0
        self._mood_level = max(0.0, round(self._mood_level - 0.1, 1))
        self._filter_and_update_table()

    def action_increase_mood(self) -> None:
        """Increase mood energy threshold by 10%."""
        self._mood_preset_idx = 0
        self._mood_level = min(0.9, round(self._mood_level + 0.1, 1))
        self._filter_and_update_table()

    def action_cycle_mood_mode(self) -> None:
        """Cycle through preset mood modes."""
        self._mood_preset_idx = (self._mood_preset_idx + 1) % len(MOOD_PRESETS)
        self._filter_and_update_table()

    def action_select_song(self) -> None:
        """Called when the user presses Enter on a song."""
        table = self.query_one(DataTable)
        try:
            song = self._visible_songs[table.cursor_row]
        except (IndexError, TypeError):
            log.error(f"action_select_song: No song data at row {table.cursor_row}")
            return

        url, kind = song.get('url'), song.get('kind')
        artist = song.get('artist') or ''
        title = song.get('title') or ''
        log.info("action_select_song: artist=%r title=%r url=%r kind=%r", artist, title, url, kind)

        res = self.api.play(url=url, kind=kind, artist=artist, title=title)
        if res.get("status") in ("launched", "ok"):
            self.notify(f"Opening {artist} - {title}")
        else:
            self.notify(f"Play failed for {artist} - {title}", severity="warning")


def browse_main() -> int:
    """Run the karaoke browser TUI."""
    app = KaraokeBrowser()
    app.run()
    return 0

if __name__ == "__main__":
    browse_main()
