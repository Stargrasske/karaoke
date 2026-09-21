"""Backend Operations & Admin TUI for Karaoke Platform.

Provides live Celery worker / Flower dashboard management,
error log diagnostics, and file ingestion / folder scan triggers.
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
from typing import Any
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Header, Footer, DataTable, Static, Button, Input, RichLog
from textual.containers import Vertical, Horizontal, Container
from textual import log as textual_log

from .api_client import ApiClient
from .logger import LOG_FILE, log

__all__ = ["KaraokeAdminApp", "RecordingBrowseModal", "admin_main"]


def markup_text(value: Any) -> str:
    """Escape dynamic text written to RichLog/Static with markup enabled."""
    return str(value).replace("[", r"\[")


class RecordingBrowseModal(ModalScreen[None]):
    """Browse what an audio recording session captured in detail."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("B", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("P", "play_track", "Play in Browser"),
        ("A", "analyse_session", "Analyse Session"),
    ]

    def __init__(self, recording_id: int, *, app_ref: Any = None) -> None:
        super().__init__()
        self.recording_id = recording_id
        self.app_ref = app_ref
        self._rows: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="rec-dialog") as dialog:
            dialog.border_title = f"Recording #{self.recording_id}"
            dialog.border_subtitle = "P: play in browser · A: analyse session · Esc / B / q: close"
            yield DataTable(id="rec-table", cursor_type="row")
            yield Static("", id="rec-hint")
            with Horizontal(id="rec-controls"):
                yield Button("Play in Browser (P)", id="btn-modal-play", variant="primary")
                yield Button("Analyse Session (A)", id="btn-modal-analyse", variant="warning")
                yield Button("Close (Esc)", id="btn-modal-close", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#rec-table", DataTable)
        table.add_columns("#", "At", "Artist", "Title", "Duration", "Status")
        self._load_tracks()

    def _load_tracks(self) -> None:
        from . import recording_audio
        try:
            self._rows = recording_audio.browse_rows(self.recording_id)
        except Exception as exc:
            log.debug("failed to browse rows for rec %s: %s", self.recording_id, exc)
            self._rows = []

        table = self.query_one("#rec-table", DataTable)
        table.clear()
        for r in self._rows:
            mins, secs = divmod(int(max(0.0, r.get("duration_s") or 0.0)), 60)
            dur_str = f"{mins}:{secs:02d}"
            wall = r.get("start_wall") or 0.0
            time_str = time.strftime("%H:%M:%S", time.localtime(wall)) if wall else "—"

            st_parts = []
            if not r.get("playable"):
                st_parts.append("[dim]unplayable[/dim]")
            elif r.get("silent"):
                st_parts.append("[dim yellow]silent[/dim yellow]")
            elif r.get("confident"):
                st_parts.append("[bold green]confident[/bold green]")
            else:
                st_parts.append("[cyan]identified[/cyan]")
            st_str = " · ".join(st_parts)

            table.add_row(
                str(r.get("index", 0) + 1),
                time_str,
                r.get("artist") or "Unknown",
                r.get("title") or "Unknown",
                dur_str,
                st_str,
                key=str(r.get("index", 0)),
            )

        playable = sum(1 for r in self._rows if r.get("playable") and not r.get("silent"))
        hint = self.query_one("#rec-hint", Static)
        hint.update(
            f"Recording #{self.recording_id}: {len(self._rows)} track(s) captured, "
            f"{playable} playable"
        )

    def _play_track(self, index: int) -> None:
        from . import recording_audio, player_open
        row = next((r for r in self._rows if r.get("index") == index), None)
        if row is None:
            return
        if not row.get("playable"):
            self.notify("Track audio is pruned or unavailable", severity="warning")
            return
        url = recording_audio.track_url(self.recording_id, index)
        try:
            player_open.open_song_url(url, "recording")
            self.notify(f"Playing track #{index + 1}: {row.get('artist')} - {row.get('title')}")
        except Exception as exc:
            self.notify(f"Could not open track in player: {exc}", severity="error")

    def action_play_track(self) -> None:
        table = self.query_one("#rec-table", DataTable)
        if 0 <= table.cursor_row < len(self._rows):
            idx = self._rows[table.cursor_row].get("index", table.cursor_row)
            self._play_track(idx)
        else:
            self.notify("Select a track to play", severity="warning")

    def action_analyse_session(self) -> None:
        self.dismiss()
        if self.app_ref and hasattr(self.app_ref, "_process_selected_recording"):
            self.app_ref._process_selected_recording(self.recording_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "rec-table":
            try:
                idx = int(event.row_key.value)
                self._play_track(idx)
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-modal-play":
            self.action_play_track()
        elif bid == "btn-modal-analyse":
            self.action_analyse_session()
        elif bid == "btn-modal-close":
            self.dismiss()


class RadioSessionsModal(ModalScreen):
    """Browse radio listening sessions (with/without recordings) and trigger library import."""

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
        ("I", "import_selected_session", "Import Session"),
        ("i", "import_selected_session", "Import Session"),
    ]

    def __init__(self, app_ref=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.app_ref = app_ref
        self._sessions = []

    def compose(self) -> ComposeResult:
        with Vertical(id="rec-dialog") as dialog:
            dialog.border_title = "Radio Listening Sessions"
            dialog.border_subtitle = "I: import session to library · Esc / q: close"
            yield DataTable(id="radio-table", cursor_type="row")
            yield Static("", id="radio-hint")
            with Horizontal(id="rec-controls"):
                yield Button("Import Session (I)", id="btn-modal-import-radio", variant="success")
                yield Button("Close (Esc)", id="btn-modal-close", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#radio-table", DataTable)
        table.add_columns("ID", "Started", "Duration", "Source", "Tracks", "Audio / REC", "Status")
        self._load_sessions()

    def _load_sessions(self) -> None:
        try:
            from . import localcache
            self._sessions = localcache.get_radio_sessions(limit=50)
        except Exception as exc:
            log.debug("failed to load radio sessions: %s", exc)
            self._sessions = []

        table = self.query_one("#radio-table", DataTable)
        table.clear()
        now = time.time()
        for s in self._sessions:
            started = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
            if s.get("ended_at"):
                elapsed_s = s["ended_at"] - s["started_at"]
                dur = f"{int(elapsed_s // 60)}m {int(elapsed_s % 60)}s"
            else:
                dur = f"{int((now - s['started_at']) // 60)}m (active)"

            rec = f"REC #{s['recording_id']}" if s.get("is_recording") and s.get("recording_id") else "no audio"
            table.add_row(
                str(s["session_id"]),
                started,
                dur,
                str(s.get("source") or "mic"),
                str(s.get("track_count") or 0),
                rec,
                str(s.get("status") or "active"),
                key=str(s["session_id"]),
            )
        hint = self.query_one("#radio-hint", Static)
        hint.update(f"Loaded {len(self._sessions)} radio sessions. Select a session and press I to import to library.")

    def action_import_selected_session(self) -> None:
        table = self.query_one("#radio-table", DataTable)
        if 0 <= table.cursor_row < len(self._sessions):
            sess = self._sessions[table.cursor_row]
            sid = sess["session_id"]
            self.dismiss()
            if self.app_ref and hasattr(self.app_ref, "_import_radio_session_worker"):
                self.app_ref._import_radio_session_worker(sid)
        else:
            self.notify("Select a radio session to import", severity="warning")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-modal-import-radio":
            self.action_import_selected_session()
        elif event.button.id == "btn-modal-close":
            self.dismiss()


class KaraokeAdminApp(App):
    """Admin and Operations Dashboard for Karaoke Backend."""

    TITLE = "Karaoke Platform Operations & Worker Control"
    SUB_TITLE = "Manage Workers · Error Logs · Ingestion Pipeline"

    # Split layout: left column houses controls & workers; right column is a
    # dedicated, full-height scrollable log & diagnostics viewer.
    CSS = """
    #admin-workspace {
        layout: horizontal;
        height: 1fr;
    }

    #controls-column {
        width: 45%;
        height: 1fr;
        overflow-y: auto;
        padding-right: 1;
    }

    #log-container {
        width: 55%;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #job-status {
        height: 3;
        border: round $warning;
        padding: 0 1;
        margin-bottom: 1;
    }

    #event-log {
        height: 9;
        overflow-y: auto;
        border: round $accent;
        padding: 0 1;
        margin-bottom: 1;
    }

    #log-title {
        text-style: bold;
        height: 1;
        margin-bottom: 1;
    }

    #error-log {
        height: 1fr;
        overflow-y: auto;
        border: none;
    }

    #worker-container, #pipeline-container, #align-container,
    #ingest-container, #clients-container, #recordings-container {
        border: round $primary;
        padding: 0 1;
        margin-bottom: 1;
        height: auto;
    }

    #worker-title, #clients-title, #recordings-title { text-style: bold; }
    #worker-controls, #pipeline-controls, #recording-controls { height: auto; margin-bottom: 1; }
    #worker-controls Button, #pipeline-controls Button, #recording-controls Button { margin-right: 1; }

    #worker-table { height: 5; margin-bottom: 1; }
    #worker-summary { height: auto; color: $text-muted; }

    #clients-container { height: auto; }
    #clients-table { height: 5; }
    #clients-summary { height: 1; color: $text-muted; }

    #align-container Horizontal, #ingest-container Horizontal { height: auto; }
    #align-container Input, #ingest-container Input { margin-right: 1; }

    #recordings-table {
        height: 6;
        margin-bottom: 1;
    }

    #recordings-summary {
        height: 1;
        color: $text-muted;
    }

    #record-panel {
        display: none;
        height: auto;
        border: round red;
        padding: 0 1;
        margin-bottom: 1;
        color: $error;
    }
    #record-panel.-on {
        display: block;
    }

    RecordingBrowseModal { align: center middle; }
    #rec-dialog {
        width: 100; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center;
    }
    #rec-table { height: auto; max-height: 20; margin-bottom: 1; }
    #rec-hint { color: $text-muted; height: 1; margin-bottom: 1; }
    #rec-controls { height: auto; }
    #rec-controls Button { margin-right: 1; }
    """



    BINDINGS = [
        ("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("escape", "handle_escape", "Unfocus / Quit", show=False, priority=True),
        ("r", "refresh_all", "Refresh"),
        ("plus", "scale_up", "Worker +1"),
        ("minus", "scale_down", "Worker -1"),
        ("R", "restart_workers", "Restart Workers"),
        ("b", "run_backfill", "Run Audio Backfill"),
        ("v", "rebuild_vectors", "Rebuild Vectors"),
        ("a", "analyse_recordings", "Analyse Recordings"),
        ("w", "align_whisper", "Whisper Align"),
        ("s", "scan_folder", "Scan Folder"),
        ("e", "fetch_errors", "Error Logs"),
        ("c", "refresh_clients", "Refresh Clients"),
        ("X", "shutdown_webui", "Stop Web UI"),
        ("K", "restart_kiosk", "Restart Kiosk Chrome"),
        ("O", "toggle_record", "Toggle Live Recording"),
        ("B", "browse_selected_recording", "Browse Recording"),
        ("I", "show_radio_sessions", "Radio Sessions"),
    ]

    def __init__(self):
        super().__init__()
        self.api = ApiClient()
        self._target_workers = 1
        self._bg_busy = False
        self._bg_lock = None
        self._recording_id = None
        self._record_tick = 0
        self._recording_rows = {}
        self._last_event_ts: float | None = None
        self._last_event_ids: tuple[str, ...] = ()
        self._last_logs_seen: list[str] = []
        self._current_task_label: str | None = None
        self._current_task_started_at: float | None = None

    def _acquire_bg_lock(self) -> bool:
        # In-process guard first (fast path), then a cross-process lockfile so a
        # concurrent run from another karaoke process (CLI, second TUI, cron) is
        # also refused rather than double-writing indices.
        if getattr(self, "_bg_busy", False):
            self.notify("Another task is currently running. Please wait...", severity="warning")
            return False
        from .lockfile import ProcessLock
        lock = ProcessLock("admin_pipeline")
        if not lock.acquire():
            self.notify("A pipeline task is running in another process. Please wait...", severity="warning")
            return False
        self._bg_lock = lock
        self._bg_busy = True
        return True

    def _release_bg_lock(self) -> None:
        self._bg_busy = False
        lock = getattr(self, "_bg_lock", None)
        if lock is not None:
            lock.release()
            self._bg_lock = None

    def _set_job_status(self, message: str) -> None:
        try:
            self.query_one("#job-status", Static).update(message)
        except Exception:
            pass

    def _mark_task_started(self, label: str) -> None:
        self._current_task_label = label
        self._current_task_started_at = time.time()
        self._set_job_status(f"[bold yellow]RUNNING[/bold yellow] {label}\n[dim]Started just now. Other admin pipeline tasks are locked until this finishes.[/dim]")

    def _mark_task_finished(self, label: str, message: str, *, severity: str = "ok") -> None:
        started = self._current_task_started_at
        elapsed = f" after {time.time() - started:.1f}s" if started else ""
        style = "bold green" if severity == "ok" else "bold red"
        self._current_task_label = None
        self._current_task_started_at = None
        self._set_job_status(f"[{style}]DONE[/] {label}{elapsed}\n[dim]{message}[/dim]")

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="admin-workspace"):
            # Left column: Operations, Workers, Clients, Pipeline controls
            with Vertical(id="controls-column"):
                # Top: Worker Pool Status & Scaling Controls
                with Container(id="worker-container"):
                    yield Static("[bold cyan]Post-Processing Workers (Celery + Flower)[/bold cyan]", id="worker-title")
                    with Horizontal(id="worker-controls"):
                        yield Button("Start Worker (+)", id="btn-scale-up", variant="success")
                        yield Button("Stop Worker (-)", id="btn-scale-down", variant="warning")
                        yield Button("Restart Workers (R)", id="btn-restart-workers", variant="primary")
                        yield Button("Stop Web UI (X)", id="btn-stop-webui", variant="default")
                        yield Button("Quit (q)", id="btn-quit", variant="error")
                    yield DataTable(id="worker-table", cursor_type="row")
                    yield Static("Worker Status: Loading...", id="worker-summary")

                # Connected clients: web-UI sessions and active playback sessions.
                with Container(id="clients-container"):
                    yield Static("[bold cyan]Connected Clients (Web UI + Playback)[/bold cyan]", id="clients-title")
                    yield DataTable(id="clients-table", cursor_type="row")
                    yield Static("Clients: Loading...", id="clients-summary")

                # Live Recording & Captured Audio Sessions Panel
                with Container(id="recordings-container"):
                    yield Static("[bold cyan]Live Recording & Session Capture[/bold cyan]", id="recordings-title")
                    with Horizontal(id="recording-controls"):
                        yield Button("Start Live Recording (O)", id="btn-record", variant="success")
                        yield Button("Browse / Inspect (B)", id="btn-browse-recordings", variant="default")
                        yield Button("Analyse All Recordings (a)", id="btn-recordings", variant="primary")
                        yield Button("Radio Sessions (I)", id="btn-radio-sessions", variant="default")
                    yield Static("", id="record-panel")
                    yield DataTable(id="recordings-table", cursor_type="row")
                    yield Static("Recordings: Loading...", id="recordings-summary")

                # Middle: Audio Processing & Vector Ingestion Controls
                with Container(id="pipeline-container"):
                    yield Static("[bold cyan]Audio Processing & Vector Ingestion Pipeline[/bold cyan]")
                    with Horizontal(id="pipeline-controls"):
                        yield Button("Run Audio Backfill (b)", id="btn-backfill", variant="success")
                        yield Button("Rebuild Vectors (v)", id="btn-rebuild-vectors", variant="primary")

                # Whisper Plain Lyrics Alignment Panel
                with Container(id="align-container"):
                    yield Static("[bold cyan]Whisper Lyrics Alignment (Paste Text or Path to lyrics.txt)[/bold cyan]")
                    with Horizontal():
                        yield Input(placeholder="Track ID or Artist - Title...", id="align-track-input")
                        yield Input(placeholder="Plain lyrics text OR path to file.txt...", id="align-text-input")
                        yield Button("Whisper Align (w)", id="btn-align", variant="success")

                # File Upload / Ingestion Panel
                with Container(id="ingest-container"):
                    yield Static("[bold cyan]File Ingestion & Folder Scan[/bold cyan]")
                    with Horizontal():
                        yield Input(placeholder="Path to folder or audio file (e.g. ~/Music)...", id="ingest-input")
                        yield Button("Scan Folder (s)", id="btn-scan", variant="primary")

            # Right column: live task status, latest platform events, diagnostics.
            with Vertical(id="log-container"):
                yield Static("[bold green]No admin pipeline task running.[/bold green]", id="job-status")
                yield RichLog(id="event-log", highlight=True, markup=True, wrap=True)
                yield Static("[bold red]Recent Error Logs & Diagnostics[/bold red]", id="log-title")
                yield RichLog(id="error-log", highlight=True, markup=True, wrap=True)

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#worker-table", DataTable)
        table.add_columns("Worker Unit", "State", "Worker ID")
        clients = self.query_one("#clients-table", DataTable)
        clients.add_columns("Kind", "Client / Session", "Detail", "Status")

        recordings = self.query_one("#recordings-table", DataTable)
        recordings.add_columns("ID", "Date/Time", "Status", "Identified / Total Marks", "Source")

        self.refresh_all()
        self.set_interval(3.0, self.refresh_workers)
        self.set_interval(5.0, self.refresh_clients)
        self.set_interval(2.5, self._poll_events)
        self.set_interval(3.0, self.refresh_record_status)
        self.set_interval(4.0, self.refresh_recordings)

    def refresh_all(self) -> None:
        self.refresh_workers()
        self.refresh_clients()
        self.refresh_events()
        self.refresh_errors()
        self.refresh_record_status()
        self.refresh_recordings()


    def refresh_workers(self) -> None:
        try:
            res = self.api._http_get(self.api.ctrl_url, "/api/workers/status")
            if not res or res.get("status") != "ok":
                res = self._direct_worker_status()
        except Exception:
            res = self._direct_worker_status()

        if not res:
            return

        table = self.query_one("#worker-table", DataTable)
        table.clear()
        units = res.get("worker_details") or res.get("units", [])
        active_count = 0
        worker_active = False
        for u in units:
            uname = u.get("unit", "")
            is_act = bool(u.get("active"))
            st = "[bold green]ACTIVE[/bold green]" if is_act else "[dim]inactive[/dim]"
            if is_act:
                active_count += 1
            if uname == "karaoke-celery-worker.service" and is_act:
                worker_active = True
            table.add_row(uname, st, str(u.get("worker_id", "")))

        self._target_workers = 1 if worker_active else 0
        cpu = res.get("worker_cpu") or 0.0
        ram = res.get("worker_memory_mb") or 0.0
        q_len = res.get("queue_depth") or 0
        dash = res.get("dashboard_url", "http://127.0.0.1:5555")
        summary = f"[bold green]Celery services:[/bold green] {active_count} active | [bold yellow]RAM:[/bold yellow] {ram:.1f} MB | [bold cyan]Queue Depth:[/bold cyan] {q_len} jobs | Flower: {dash}"
        self.query_one("#worker-summary", Static).update(summary)

    def _direct_worker_status(self) -> dict[str, Any]:
        """Direct systemd fallback when ctrl_api HTTP is unreachable."""
        units = []
        active_count = 0
        names = ["karaoke-celery-worker.service", "karaoke-celery-flower.service"]
        for i, uname in enumerate(names, start=1):
            try:
                r = subprocess.run(["systemctl", "--user", "is-active", uname], capture_output=True, text=True, timeout=2)
                act = r.stdout.strip() == "active"
            except Exception:
                act = False
            if act:
                active_count += 1
            units.append({"unit": uname, "worker_id": i, "active": act, "kind": "celery"})
        return {
            "status": "ok",
            "orchestrator": "celery",
            "dashboard_url": "http://127.0.0.1:5555",
            "workers_active": active_count,
            "worker_cpu": 0.0,
            "worker_memory_mb": 0.0,
            "queue_depth": 0,
            "units": units,
        }

    def _format_event_line(self, event: dict[str, Any]) -> str:
        ts = event.get("ts")
        task_name = event.get("task_name") or event.get("type") or "event"
        state = event.get("state") or ""
        task_id = str(event.get("task_id") or event.get("id") or "")[:12]
        when = f"{float(ts):.0f}" if isinstance(ts, int | float) else str(ts or "")
        state_style = "bold green" if str(state).upper() in {"SUCCESS", "SUCCEEDED", "OK"} else "bold yellow"
        return f"[dim]{when}[/dim] [{state_style}]{state or 'event'}[/] {task_name} [dim]{task_id}[/dim]"

    def refresh_events(self) -> None:
        try:
            res = self.api.recent_events(limit=10)
            events = res.get("events", []) if res else []
            event_ids = tuple(str(e.get("task_id") or e.get("id") or e.get("ts") or i) for i, e in enumerate(events))
            if event_ids == self._last_event_ids:
                return
            self._last_event_ids = event_ids
            event_log = self.query_one("#event-log", RichLog)
            event_log.clear()
            event_log.write("[bold cyan]Latest Platform Events[/bold cyan]")
            if not events:
                event_log.write("[dim]No platform events observed yet.[/dim]")
                return
            for event in events:
                event_log.write(self._format_event_line(event))
        except Exception:
            log.debug("refresh_events failed", exc_info=True)

    def refresh_errors(self) -> None:
        try:
            res = self.api._http_get(self.api.ctrl_url, "/api/logs/errors?lines=50")
            error_log = self.query_one("#error-log", RichLog)
            if res and res.get("logs"):
                logs = res["logs"]
                if getattr(self, "_last_logs_seen", None) != logs:
                    self._last_logs_seen = list(logs)
                    error_log.clear()
                    for err in logs:
                        line = str(err)
                        if "ERROR" in line or "CRITICAL" in line:
                            error_log.write(f"[bold red]{line}[/bold red]")
                        elif "Traceback" in line or "Exception" in line:
                            error_log.write(f"[bold yellow]{line}[/bold yellow]")
                        elif "WARNING" in line:
                            error_log.write(f"[yellow]{line}[/yellow]")
                        else:
                            error_log.write(f"[dim]{line}[/dim]")
            else:
                if getattr(self, "_last_logs_seen", None) != []:
                    self._last_logs_seen = []
                    error_log.clear()
                    error_log.write("[dim]No recent system errors found.[/dim]")
        except Exception as exc:
            log.debug("refresh_errors failed", exc_info=True)

    def _poll_events(self) -> None:
        """Poll the platform event ledger. Refresh worker status if new events arrived."""
        last_ts = self._last_event_ts

        def _work() -> None:
            try:
                res = self.api.recent_events(since_ts=last_ts, limit=5)
                items = res.get("events", [])
            except Exception:
                return

            if not items:
                return

            newest_ts = max((e.get("ts") for e in items if e.get("ts") is not None), default=last_ts)
            if newest_ts:
                self._last_event_ts = float(newest_ts)

            # If any postprocess / task event completed, refresh right pane and workers immediately.
            self.call_from_thread(self.refresh_events)
            self.call_from_thread(self.refresh_workers)
            self.call_from_thread(self.refresh_errors)

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            pass

    # -- connected clients ------------------------------------------------
    WEBUI_PORT = int(os.environ.get("PORT", "8001"))

    def _webui_clients(self) -> list[dict[str, Any]]:
        """Web-UI (textual-serve) sessions, discovered from listeners on the port.

        The web TUI is served by scripts/web_serve.py (textual-serve) with one
        websocket per browser tab. There is no HTTP status endpoint, so count
        the ESTABLISHED connections to the serve port off `ss` — best-effort and
        never fatal when `ss` is missing or the server is down.
        """
        rows: list[dict[str, Any]] = []
        try:
            out = subprocess.run(
                ["ss", "-Htn", "state", "established", f"( sport = :{self.WEBUI_PORT} )"],
                capture_output=True, text=True, timeout=2,
            ).stdout
        except Exception:
            return rows
        for i, line in enumerate((out or "").splitlines(), start=1):
            parts = line.split()
            peer = parts[-1] if parts else "?"
            rows.append({"kind": "web-ui", "client": f"tab {i}",
                         "detail": peer, "status": "connected"})
        return rows

    def _playback_clients(self) -> list[dict[str, Any]]:
        """Active playback sessions from the control API (best-effort)."""
        rows: list[dict[str, Any]] = []
        try:
            res = self.api.list_play_sessions()
        except Exception:
            return rows
        for s in (res or {}).get("sessions", []):
            title = s.get("title") or s.get("url") or s.get("session_id", "?")
            rows.append({"kind": "playback",
                         "client": str(s.get("session_id", "?"))[:16],
                         "detail": str(title)[:40],
                         "status": s.get("status", "")})
        return rows

    def refresh_clients(self) -> None:
        try:
            table = self.query_one("#clients-table", DataTable)
        except Exception:
            return
        web = self._webui_clients()
        play = self._playback_clients()
        table.clear()
        for c in web + play:
            table.add_row(c["kind"], c["client"], c["detail"], c["status"])
        summary = (f"[bold cyan]Web UI:[/bold cyan] {len(web)} tab(s) on :{self.WEBUI_PORT}"
                   f" | [bold green]Playback:[/bold green] {len(play)} session(s)")
        self.query_one("#clients-summary", Static).update(summary)

    def action_refresh_clients(self) -> None:
        self.refresh_clients()

    def action_shutdown_webui(self) -> None:
        """`X`: gracefully stop the web-UI (textual-serve) server, if running.

        The web TUI has no systemd unit — it is `scripts/web_serve.py` started by
        hand. Signal it politely (SIGTERM) so open browser tabs get a clean
        socket close rather than a reset. Never touches this admin process.
        """
        killed = 0
        try:
            out = subprocess.run(
                ["pgrep", "-f", "web_serve.py"], capture_output=True, text=True, timeout=2
            ).stdout
            for pid in out.split():
                try:
                    os.kill(int(pid), 15)  # SIGTERM: let textual-serve close sockets
                    killed += 1
                except (ProcessLookupError, ValueError, PermissionError):
                    pass
        except Exception as exc:
            self.notify(f"Web UI shutdown failed: {exc}", severity="error")
            return
        if killed:
            self.notify(f"Web UI stopped ({killed} server process(es) signalled)")
        else:
            self.notify("No running web UI server found", severity="warning")
        self.refresh_clients()

    def action_restart_kiosk(self) -> None:
        """`K`: Restart the kiosk Chrome window via control API."""
        self.notify("Signalling kiosk Chrome restart...")
        ok = self.api.restart_player_window()
        if ok:
            self.notify("Kiosk Chrome window restarted successfully")
        else:
            self.notify("Failed to restart kiosk Chrome window", severity="error")
        self.refresh_clients()

    def refresh_record_status(self) -> None:
        """Update the Record button and state based on active recording."""
        self._record_tick += 1
        try:
            st = self.api.record_status()
            sessions = (st.get("recording") or st.get("sessions") or []) if isinstance(st, dict) else []
            active_id = None
            active_item = None
            for s in sessions:
                if s.get("recording_id") and s.get("status") == "recording":
                    active_id = int(s["recording_id"])
                    active_item = s
                    break
            self._recording_id = active_id

            btn = self.query_one("#btn-record", Button)
            if active_id is not None:
                btn.label = f"Stop Record #{active_id} (O)"
                btn.variant = "error"
            else:
                btn.label = "Start Live Recording (O)"
                btn.variant = "success"

            panel = self.query_one("#record-panel", Static)
            if active_id is not None and active_item is not None:
                panel.set_class(True, "-on")
                panel.update(record_panel(
                    recording_id=active_id,
                    elapsed_s=float(active_item.get("elapsed_s") or 0.0),
                    marks_ok=int(active_item.get("identified") or 0),
                    marks_total=int(active_item.get("marks") or 0),
                    size_bytes=int(active_item.get("audio_bytes") or 0),
                    source=str(active_item.get("source") or ""),
                    blink=self._record_tick % 2 == 1,
                ))
            else:
                panel.set_class(False, "-on")
                panel.update("")
        except Exception:
            pass

    def action_toggle_record(self) -> None:
        """`O`: record the output continuously, marking what plays on it."""
        if self._recording_id is None:
            try:
                st = self.api.record_status()
                sessions = (st.get("recording") or st.get("sessions") or []) if isinstance(st, dict) else []
                for s in sessions:
                    if s.get("recording_id"):
                        self._recording_id = int(s["recording_id"])
                        break
            except Exception:
                pass

        if self._recording_id is not None:
            stopped_id = self._recording_id
            try:
                from . import recorder
                recorded, total = recorder.mark_count(stopped_id)
            except Exception:
                recorded, total = 0, 0
            self.api.record_stop(stopped_id)
            self.notify(f"Recording {stopped_id} stopped ({recorded}/{total} tracks identified)")
            self._recording_id = None
            self.refresh_record_status()
            self.refresh_recordings()
            if recorded > 0:
                self._process_selected_recording(stopped_id)
            return

        result = self.api.record_start()
        if result.get("status") != "recording":
            detail = result.get("detail", "unknown error")
            self.notify(f"Cannot record: {detail}", severity="error")
            return
        self._recording_id = result["recording_id"]
        from pathlib import Path
        directory = Path(result.get("dir", "")).name
        if result.get("reused"):
            self.notify(f"Reattached to active recording {self._recording_id}")
        else:
            self.notify(f"Recording {self._recording_id} to {directory}")
        self.refresh_record_status()
        self.refresh_recordings()

    def action_scale_up(self) -> None:
        self._target_workers = 1
        self._scale_workers(self._target_workers)

    def action_scale_down(self) -> None:
        self._target_workers = 0
        self._scale_workers(self._target_workers)

    def _scale_workers(self, target: int) -> None:
        res = self.api._http_post(self.api.ctrl_url, "/api/workers/scale", {"target": target})
        if res and res.get("status") == "ok":
            self.notify("Celery worker started" if target else "Celery worker stopped")
            try:
                self.refresh_workers()
            except Exception:
                pass
        else:
            self.notify("Failed to update worker scaling", severity="error")

    def action_restart_workers(self) -> None:
        self._scale_workers(self._target_workers or 1)
        self.notify("Restarted Celery postprocess worker")

    def _append_event_log(self, line: str) -> None:
        try:
            self.query_one("#event-log", RichLog).write(line)
        except Exception:
            log.debug("Could not append admin event log line", exc_info=True)

    def _folder_scan_progress_line(self, event: str, payload: dict[str, Any]) -> str:
        index = payload.get("index")
        total = payload.get("total")
        prefix = f"[{index}/{total}] " if index and total else ""
        name = markup_text(payload.get("name") or payload.get("root") or "")
        artist = markup_text(payload.get("artist") or "")
        title = markup_text(payload.get("title") or "")
        track = f" — {artist} - {title}" if artist or title else ""
        if event == "found":
            return f"[bold cyan]Folder scan[/] found {payload.get('total', 0)} audio file(s) in {name}"
        if event == "item_start":
            return f"[bold yellow]{prefix}Scanning[/] {name}"
        if event == "tagged":
            return f"[dim]{prefix}Tags[/dim] {name}{track}"
        if event == "analysis_start":
            return f"[yellow]{prefix}Analysing key/BPM[/] {name}"
        if event == "analysis_done":
            key = payload.get("key") or "?"
            bpm = payload.get("bpm") or "?"
            return f"[green]{prefix}Audio analysis[/] key={markup_text(key)} bpm={markup_text(bpm)}{track}"
        if event == "clap_start":
            return f"[yellow]{prefix}Embedding/classifying audio[/] {name}"
        if event == "clap_done":
            genre = payload.get("genre") or "?"
            return f"[green]{prefix}Genre[/] {markup_text(genre)}{track}"
        if event == "source_start":
            return f"[yellow]{prefix}Resolving YouTube/Spotify sources[/]{track}"
        if event == "source_done":
            sources = []
            if payload.get("yt_url"):
                sources.append("YouTube")
            if payload.get("spotify_uri"):
                sources.append("Spotify")
            source_text = ", ".join(sources) or "no streaming source"
            return f"[green]{prefix}Sources[/] {source_text}{track}"
        if event == "ingest_start":
            return f"[yellow]{prefix}Ingesting into SQLite[/]{track}"
        if event == "item_done":
            processed = payload.get("processed")
            errors = payload.get("errors", 0)
            return f"[bold green]{prefix}Done[/] track_id={payload.get('track_id', '?')} processed={processed} errors={errors}{track}"
        if event == "skip":
            return f"[bold yellow]{prefix}Skipped[/] {name}: {markup_text(payload.get('reason') or 'unknown')}"
        if event == "error":
            return f"[bold red]{prefix}Error[/] {name}: {markup_text(payload.get('error') or 'unknown')}"
        if event == "done":
            return (f"[bold green]Folder scan done[/] seen={payload.get('seen', 0)} "
                    f"processed={payload.get('processed', 0)} classified={payload.get('classified', 0)} "
                    f"sourced={payload.get('sourced', 0)} errors={payload.get('errors', 0)}")
        return f"[dim]Folder scan {markup_text(event)}[/dim] {markup_text(payload)}"

    def _folder_scan_progress(self, event: str, payload: dict[str, Any]) -> None:
        line = self._folder_scan_progress_line(event, payload)
        self.call_from_thread(self._append_event_log, line)
        if event in {"found", "item_start", "analysis_start", "clap_start", "source_start", "ingest_start"}:
            root_or_name = payload.get("name") or payload.get("root") or ""
            index = payload.get("index")
            total = payload.get("total")
            step = event.replace("_", " ")
            suffix = f" ({index}/{total})" if index and total else ""
            self.call_from_thread(
                self._set_job_status,
                f"[bold yellow]RUNNING[/bold yellow] Folder scan{suffix}: {markup_text(root_or_name)}\n[dim]{markup_text(step)}[/dim]",
            )

    def action_scan_folder(self) -> None:
        """Scan a folder directly inside the TUI with visual progress and lock."""
        inp = self.query_one("#ingest-input", Input)
        folder = inp.value.strip() or "~/Music"

        if not self._acquire_bg_lock():
            return

        label = f"Folder scan: {folder}"
        self._mark_task_started(label)
        self.notify(f"Scanning folder {folder}...")

        def _bg():
            try:
                from . import folder_scan, vector_index
                from pathlib import Path
                root = Path(folder).expanduser()
                if not root.is_dir():
                    raise ValueError(f"Directory not found: {root}")

                stats = folder_scan.scan_and_ingest_folder(
                    root,
                    use_fingerprint=True,
                    classify_audio=True,
                    resolve_streaming=True,
                    dry_run=False,
                    progress=self._folder_scan_progress,
                )

                self.call_from_thread(self._append_event_log, "[bold yellow]Rebuilding OpenSearch vectors for scanned files...[/]")
                # Rebuild vectors for newly ingested files
                vector_index.rebuild_from_sqlite(embed=True, include_lines=True)

                msg = (f"Scan complete. Seen: {stats.get('seen', 0)}, "
                       f"Processed: {stats.get('processed', 0)}, "
                       f"Classified: {stats.get('classified', 0)}, "
                       f"Sourced: {stats.get('sourced', 0)}, "
                       f"Errors: {stats.get('errors', 0)}")
                self.call_from_thread(self.notify, msg)
                self.call_from_thread(self._mark_task_finished, label, msg)
            except Exception as exc:
                msg = f"Scan failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)

        self.run_worker(_bg, thread=True)

    def action_run_backfill(self) -> None:
        """`b`: Trigger audio gap-fill and zero-shot genre backfill."""
        if not self._acquire_bg_lock():
            return
        label = "Audio backfill"
        self._mark_task_started(label)
        self.notify("Started audio gap-fill and classification backfill...")
        def _bg():
            try:
                import subprocess
                p = Path(__file__).resolve().parent.parent.parent / "scripts" / "fill_analysis_and_vector_gaps.py"
                if p.is_file():
                    subprocess.run([sys.executable, str(p)], check=True, timeout=600)
                    msg = "Audio backfill completed successfully!"
                    self.call_from_thread(self.notify, msg)
                    self.call_from_thread(self._mark_task_finished, label, msg)
            except Exception as exc:
                msg = f"Backfill failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def action_rebuild_vectors(self) -> None:
        """`v`: Rebuild OpenSearch vector indices."""
        if not self._acquire_bg_lock():
            return
        label = "Vector rebuild"
        self._mark_task_started(label)
        self.notify("Rebuilding OpenSearch vector indices...")
        def _bg():
            try:
                from . import vector_index, localcache
                with localcache.connect() as conn:
                    st = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
                msg = f"Vector indices updated: {st.indexed} tracks, {st.line_docs} lines"
                self.call_from_thread(self.notify, msg)
                self.call_from_thread(self._mark_task_finished, label, msg)
            except Exception as exc:
                msg = f"Vector rebuild failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def action_analyse_recordings(self) -> None:
        """`a`: Process, decompile, and ingest detected song vectors from recordings."""
        if not self._acquire_bg_lock():
            return
        label = "Recording analysis"
        self._mark_task_started(label)
        self.notify("Analysing captured audio recordings and ingesting song vectors...")
        def _bg():
            try:
                from . import recording_worker, localcache, vector_index
                with localcache.connect() as conn:
                    rows = conn.execute("SELECT recording_id FROM recordings WHERE status != 'recording' ORDER BY recording_id DESC LIMIT 20").fetchall()
                    processed = 0
                    for r in rows:
                        rid = int(r["recording_id"])
                        res = recording_worker.analyse(rid)
                        if res:
                            processed += 1
                    if processed:
                        vector_index.rebuild_from_sqlite(embed=True, include_lines=True, include_notes=True)
                msg = f"Processed {processed} recording(s) and ingested song vectors"
                self.call_from_thread(self.notify, msg)
                self.call_from_thread(self._mark_task_finished, label, msg)
            except Exception as exc:
                msg = f"Recording analysis failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def action_align_whisper(self) -> None:
        """`w`: Align plain lyrics text or uploaded lyrics file using Whisper."""
        track_input = self.query_one("#align-track-input", Input).value.strip()
        text_input = self.query_one("#align-text-input", Input).value.strip()
        if not track_input or not text_input:
            self.notify("Provide track identifier and lyrics text/file path", severity="warning")
            return
        if not self._acquire_bg_lock():
            return
        label = "Whisper alignment"
        self._mark_task_started(label)
        self.notify(f"Aligning lyrics for '{track_input}' with Whisper...")
        def _bg():
            try:
                res = align_plain_text_for_track(track_input, text_input)
                self.call_from_thread(self.notify, res)
                self.call_from_thread(self._mark_task_finished, label, str(res))
            except Exception as exc:
                msg = f"Alignment failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-scale-up":
            self.action_scale_up()
        elif bid == "btn-scale-down":
            self.action_scale_down()
        elif bid == "btn-restart-workers":
            self.action_restart_workers()
        elif bid == "btn-backfill":
            self.action_run_backfill()
        elif bid == "btn-rebuild-vectors":
            self.action_rebuild_vectors()
        elif bid == "btn-recordings":
            self.action_analyse_recordings()
        elif bid == "btn-record":
            self.action_toggle_record()
        elif bid == "btn-browse-recordings":
            self.action_browse_selected_recording()
        elif bid == "btn-radio-sessions":
            self.action_show_radio_sessions()
        elif bid == "btn-align":
            self.action_align_whisper()
        elif bid == "btn-scan":
            self.action_scan_folder()
        elif bid == "btn-stop-webui":
            self.action_shutdown_webui()
        elif bid == "btn-quit":
            self.exit()

    def action_handle_escape(self) -> None:
        """Escape: drop focus from an input first, otherwise quit.

        A bare `q` cannot quit while a text Input (Whisper track / lyrics /
        folder path) holds focus — it types the letter instead. Escape unfocuses
        so `q` works again, and quits outright when nothing is focused.
        """
        focused = self.focused
        if focused is not None and isinstance(focused, Input):
            self.set_focus(None)
            return
        self.exit()

    def refresh_recordings(self) -> None:
        """Fetch the last 15 recordings from SQLite and update recordings-table."""
        try:
            table = self.query_one("#recordings-table", DataTable)
        except Exception:
            return

        try:
            from . import localcache
            import datetime

            with localcache.connect() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT r.recording_id, r.started_at, r.ended_at, r.source, r.status, r.note,
                           COUNT(m.mark_id) AS total_marks,
                           SUM(CASE WHEN m.ok = 1 THEN 1 ELSE 0 END) AS ok_marks
                    FROM recordings r
                    LEFT JOIN recording_marks m ON m.recording_id = r.recording_id
                    GROUP BY r.recording_id
                    ORDER BY r.recording_id DESC
                    LIMIT 15
                """)
                rows = cur.fetchall()

            table.clear()
            active_count = 0
            complete_count = 0
            analysed_count = 0

            self._recording_rows = {}

            for row in rows:
                rid = row["recording_id"]
                status = row["status"]

                try:
                    dt = datetime.datetime.fromtimestamp(row["started_at"])
                    started_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    started_str = str(row["started_at"])

                if status == "recording":
                    status_fmt = "[bold red]RECORDING[/bold red]"
                    active_count += 1
                elif status == "complete":
                    status_fmt = "[bold yellow]complete[/bold yellow]"
                    complete_count += 1
                elif status == "analysed":
                    status_fmt = "[dim green]analysed[/dim green]"
                    analysed_count += 1
                else:
                    status_fmt = status

                marks_fmt = f"{row['ok_marks'] or 0} / {row['total_marks'] or 0}"
                src_fmt = short_source(row["source"]) if row["source"] else ""

                row_key = table.add_row(
                    str(rid),
                    started_str,
                    status_fmt,
                    marks_fmt,
                    src_fmt
                )
                self._recording_rows[row_key] = rid

            summary = (
                f"[bold cyan]Recordings:[/bold cyan] {active_count} active | "
                f"[bold yellow]{complete_count} complete (ready to process)[/bold yellow] | "
                f"[dim]{analysed_count} analysed[/dim]"
            )
            self.query_one("#recordings-summary", Static).update(summary)
        except Exception as exc:
            log.debug("refresh_recordings failed", exc_info=True)

    def _process_selected_recording(self, rid: int) -> None:
        """Process, decompile, and ingest detected song vectors for a single recording ID."""
        if not self._acquire_bg_lock():
            return
        label = f"Decompile & analyse recording #{rid}"
        self._mark_task_started(label)
        self.notify(f"Analysing captured audio recording #{rid}...")

        def _bg():
            try:
                from . import recording_worker, vector_index
                res = recording_worker.analyse(rid)
                msg = f"Successfully analysed recording #{rid} and ingested song vectors"
                self.call_from_thread(self.notify, msg)
                self.call_from_thread(self._mark_task_finished, label, msg)
                # Rebuild vectors to make sure OpenSearch is refreshed
                vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
                self.call_from_thread(self.refresh_recordings)
            except Exception as exc:
                msg = f"Analysis of recording #{rid} failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        try:
            if hasattr(self, "_thread_id"):
                self.run_worker(_bg, thread=True)
            else:
                self._release_bg_lock()
        except Exception as exc:
            log.debug("Could not start background worker for recording %s: %s", rid, exc)
            self._release_bg_lock()

    def action_browse_selected_recording(self) -> None:
        """`B`: Browse the selected or latest recording session in detail."""
        rid = None
        try:
            table = self.query_one("#recordings-table", DataTable)
            if table.row_count > 0:
                row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
                rid = getattr(self, "_recording_rows", {}).get(row_key)
        except Exception:
            pass

        if rid is None:
            if self._recording_id is not None:
                rid = self._recording_id
            elif getattr(self, "_recording_rows", None):
                rid = next(iter(self._recording_rows.values()))

        if rid is None:
            self.notify("No recording session available to browse", severity="warning")
            return

        self.push_screen(RecordingBrowseModal(rid, app_ref=self))

    def action_show_radio_sessions(self) -> None:
        """`I`: Open the radio listening sessions manager & import modal."""
        self.push_screen(RadioSessionsModal(app_ref=self))

    def _import_radio_session_worker(self, session_id: int) -> None:
        """Import tracks and audio from a radio session in a background worker."""
        if not self._acquire_bg_lock():
            return
        label = f"Import radio session #{session_id}"
        self._mark_task_started(label)
        self.notify(f"Importing radio session #{session_id} into library...")

        def _bg():
            try:
                from . import radio_pipeline, vector_index
                res = radio_pipeline.import_radio_session(session_id)
                msg = f"Imported {res.get('imported_tracks', 0)} tracks ({res.get('audio_slices', 0)} audio slices) from radio session #{session_id}"
                self.call_from_thread(self.notify, msg)
                self.call_from_thread(self._mark_task_finished, label, msg)
                vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
                self.call_from_thread(self.refresh_recordings)
            except Exception as exc:
                msg = f"Import of radio session #{session_id} failed: {exc}"
                self.call_from_thread(self.notify, msg, severity="error")
                self.call_from_thread(self._mark_task_finished, label, msg, severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)

        try:
            if hasattr(self, "_thread_id"):
                self.run_worker(_bg, thread=True)
            else:
                self._release_bg_lock()
        except Exception as exc:
            log.debug("Could not run radio import worker for session %s: %s", session_id, exc)
            self._release_bg_lock()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection inside any DataTable."""
        table_id = event.data_table.id
        if table_id == "recordings-table":
            row_key = event.row_key
            rid = getattr(self, "_recording_rows", {}).get(row_key)
            if rid is not None:
                self.push_screen(RecordingBrowseModal(rid, app_ref=self))


def short_source(name: str, width: int = 26) -> str:
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


def align_plain_text_for_track(track_identifier: str, lyrics_or_file_path: str) -> str:
    """Save plain lyrics text or read a lyrics file, run Whisper alignment, and store time-synced LRC."""
    from pathlib import Path
    from . import localcache, postprocess_worker, youtube, vector_index

    p = Path(lyrics_or_file_path.strip()).expanduser()
    if p.is_file():
        plain_text = p.read_text(encoding="utf-8", errors="replace")
    else:
        plain_text = lyrics_or_file_path.strip()

    if not plain_text:
        raise ValueError("No lyrics text or valid file path provided.")

    with localcache.connect() as conn:
        track_id = None
        ident = track_identifier.strip()
        if ident.isdigit():
            track_id = int(ident)
        else:
            if " - " in ident:
                a, t = ident.split(" - ", 1)
                track_id = localcache.find_track_id(a, t, conn)
            if track_id is None:
                row = conn.execute(
                    "SELECT track_id FROM tracks WHERE artist ILIKE %s OR title ILIKE %s LIMIT 1",
                    (f"%{ident}%", f"%{ident}%")
                ).fetchone()
                if row:
                    track_id = row["track_id"]

        if not track_id:
            raise ValueError(f"Could not find track matching '{track_identifier}'")

        track_row = conn.execute("SELECT artist, title FROM tracks WHERE track_id = %s", (track_id,)).fetchone()
        artist, title = track_row["artist"], track_row["title"]

        localcache.add_track_and_lyrics(
            artist, title, localcache.Lyrics(synced_raw="", plain=plain_text, source="user_input"), conn=conn
        )

        source_row = conn.execute(
            "SELECT url, kind FROM sources WHERE track_id = %s ORDER BY source_id LIMIT 1",
            (track_id,)
        ).fetchone()

        audio_path = None
        if source_row:
            url, kind = source_row["url"], source_row["kind"]
            if kind == "local" and Path(url).is_file():
                audio_path = Path(url)
            elif url and ("youtube" in kind or "http" in url):
                audio_path = youtube.download(url)

        if not audio_path or not Path(audio_path).is_file():
            raise ValueError(f"Could not locate or download audio for '{artist} - {title}' to align against.")

        ok = postprocess_worker._run_sync(track_id, Path(audio_path), conn)
        if not ok:
            raise RuntimeError(f"Whisper alignment produced no output for '{artist} - {title}'")

        vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
        return f"Successfully aligned lyrics for #{track_id}: {artist} - {title}"


def admin_main() -> None:
    """CLI entrypoint for running the admin operations TUI."""
    app = KaraokeAdminApp()
    app.run()


if __name__ == "__main__":
    admin_main()
