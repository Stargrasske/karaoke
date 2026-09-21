"""Radio Session Library Ingestion Pipeline.

Processes radio listening sessions (both with and without audio recordings),
slices recorded audio tracks from disk, enriches with tags, Key/BPM analysis,
OpenSearch audio vectors, LRCLIB lyrics, and YouTube/Spotify streaming links,
and ingests them into the library.
"""
from __future__ import annotations

import re
import psycopg
from psycopg import Connection, Cursor
import time
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

from . import localcache
from .config import settings
from .logger import log


def _clean_filename(text: str) -> str:
    """Sanitize strings for safe file naming."""
    clean = re.sub(r'[\\/*?:"<>|]', "", text).strip()
    return clean or "unknown"


def import_radio_session(
    session_id: int,
    *,
    save_audio: bool = True,
    resolve_streaming: bool = True,
    conn: Optional[Connection] = None,
) -> dict[str, Any]:
    """Import all tracks from a radio session into the karaoke library.

    - Slices FLAC audio if a recording is attached and writes it to
      `<data_dir>/radio_imports/session-<id>/`.
    - Creates track rows and attaches audio sources in SQLite.
    - Resolves lyrics (LRCLIB) and logs gaps if missing.
    - Resolves streaming YouTube/Spotify sources for karaoke playback.
    - Updates `radio_session_tracks.imported = 1`.
    """
    own = conn is None
    c = conn or localcache.connect()
    try:
        session = localcache.get_radio_session(session_id, conn=c)
        if not session:
            raise ValueError(f"Radio session {session_id} not found")

        tracks = localcache.get_radio_session_tracks(session_id, conn=c)
        if not tracks:
            return {
                "session_id": session_id,
                "imported_tracks": 0,
                "audio_slices": 0,
                "tracks": [],
                "detail": "No tracks detected in this session",
            }

        rec_id = session.get("recording_id")
        rec_dir_str = session.get("recording_dir")
        rec_dir = Path(rec_dir_str) if rec_dir_str else None

        # Prepare audio destination
        import_root = Path(settings.data_dir) / "radio_imports" / f"session-{session_id}"
        if save_audio and rec_id and rec_dir and rec_dir.is_dir():
            import_root.mkdir(parents=True, exist_ok=True)

        # Pre-load segments if recording is available
        available_segments = []
        segment_files = []
        if rec_id and rec_dir and rec_dir.is_dir():
            try:
                from . import recorder, recording_slice, recording_worker
                marks = recorder.load_marks(rec_id, conn=c)
                available_segments = recording_slice.segments(marks)
                segment_files = recording_worker.segment_files(rec_dir)
            except Exception as exc:
                log.debug("Failed loading segments for recording %s: %s", rec_id, exc)

        results = []
        slices_created = 0

        for t in tracks:
            artist = t["artist"].strip()
            title = t["title"].strip()
            if not artist or not title:
                continue

            dest_audio_path: Optional[Path] = None

            # 1. Slice audio if matching segment exists
            if save_audio and segment_files and available_segments:
                for seg in available_segments:
                    if seg.artist.lower() == artist.lower() and seg.title.lower() == title.lower():
                        clean_name = f"{_clean_filename(artist)} - {_clean_filename(title)}.flac"
                        candidate = import_root / clean_name
                        try:
                            from . import recording_worker
                            if recording_worker.cut(segment_files, seg.start_wall, seg.end_wall, candidate):
                                dest_audio_path = candidate
                                slices_created += 1
                                # Write metadata tags
                                try:
                                    from . import tags
                                    tags.write_tags(candidate, artist=artist, title=title,
                                                    album=f"Radio Session #{session_id}")
                                except Exception:
                                    pass
                        except Exception as exc:
                            log.debug("Failed cutting slice for %s - %s: %s", artist, title, exc)
                        break

            # 2. Register track in database
            duration = t.get("offset_s")
            track_id = localcache.find_track_id(artist, title, c)
            if dest_audio_path and dest_audio_path.is_file():
                track_id = localcache.add_track_source(
                    artist, title, album=f"Radio Session #{session_id}",
                    duration=duration, url=str(dest_audio_path), kind="radio_capture", conn=c
                )
            elif track_id is None:
                track_id = localcache.add_track_source(
                    artist, title, album="Radio Discovery", duration=duration,
                    url="", kind="radio_discovery", conn=c
                )

            # 3. Analyze Audio (Key, BPM, Vectors) if sliced audio exists
            analysis_info = {}
            if dest_audio_path and dest_audio_path.is_file():
                try:
                    from .analyze import analyze_audio
                    res = analyze_audio(str(dest_audio_path))
                    if res and (res.key or res.bpm):
                        from .track_analysis import save_detected
                        save_detected(
                            track_id,
                            detected_key=res.key,
                            key_confidence=res.key_confidence,
                            key_agreement=res.key_agreement,
                            bpm=res.bpm,
                            method=f"{res.method}+radio_import",
                            energy=res.energy,
                            brightness=res.brightness,
                            source_kind="recording",
                            conn=c,
                        )
                        analysis_info = {
                            "key": getattr(res.key, "name", None),
                            "bpm": round(res.bpm, 1) if res.bpm else None,
                        }
                except Exception as exc:
                    log.debug("Audio analysis skipped for %s: %s", dest_audio_path, exc)

            # 4. Fetch & attach lyrics
            ly_attached = False
            try:
                from . import lyrics
                ly = lyrics.fetch_lrclib(artist, title)
                if ly and (ly.synced_raw or ly.plain):
                    localcache.add_track_and_lyrics(artist, title, ly, conn=c)
                    ly_attached = True
                else:
                    localcache.log_lyric_gap(artist, title, c)
            except Exception as exc:
                log.debug("Lyric fetch failed for %s - %s: %s", artist, title, exc)

            # 5. Streaming Resolution (YouTube / Spotify)
            yt_attached = False
            if resolve_streaming:
                try:
                    from . import youtube
                    hits = youtube.search(f"{artist} - {title}", limit=1)
                    if hits and hits[0].get("url"):
                        localcache.add_track_source(artist, title, url=hits[0]["url"], kind="youtube", conn=c)
                        yt_attached = True
                except Exception:
                    pass

            # 6. Mark track as imported
            localcache.mark_radio_track_imported(t["id"], track_id, conn=c)

            results.append({
                "session_track_id": t["id"],
                "track_id": track_id,
                "artist": artist,
                "title": title,
                "audio_slice": str(dest_audio_path) if dest_audio_path else None,
                "lyrics": ly_attached,
                "youtube": yt_attached,
                "analysis": analysis_info,
            })

        c.commit()
        note = f"Imported {len(results)} tracks ({slices_created} audio slices) to library"
        localcache.finish_radio_session(session_id, notes=note, conn=c)

        return {
            "session_id": session_id,
            "imported_tracks": len(results),
            "audio_slices": slices_created,
            "tracks": results,
        }
    finally:
        if own:
            c.close()


def print_radio_sessions(limit: int = 30) -> int:
    """Print a Rich formatted table of radio listening sessions."""
    console = Console()
    sessions = localcache.get_radio_sessions(limit=limit)
    if not sessions:
        console.print("[dim]No radio listening sessions recorded yet.[/dim]")
        return 0

    table = Table(title="Radio Listening Sessions", header_style="bold cyan")
    table.add_column("ID", justify="right", style="bold")
    table.add_column("Started", style="dim")
    table.add_column("Duration", justify="right")
    table.add_column("Source")
    table.add_column("Tracks", justify="right")
    table.add_column("Recording", style="dim")
    table.add_column("Status")
    table.add_column("Notes", style="italic")

    now = time.time()
    for s in sessions:
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
        if s.get("ended_at"):
            elapsed_s = s["ended_at"] - s["started_at"]
            dur = f"{int(elapsed_s // 60)}m {int(elapsed_s % 60)}s"
        else:
            dur = f"{int((now - s['started_at']) // 60)}m (active)"

        rec = f"REC #{s['recording_id']}" if s.get("recording_id") else "no audio"
        status_style = "bold green" if s["status"] == "completed" else "bold yellow" if s["status"] == "active" else "dim"

        table.add_row(
            str(s["session_id"]),
            started,
            dur,
            s["source"],
            str(s["track_count"]),
            rec,
            f"[{status_style}]{s['status']}[/]",
            s.get("notes") or "",
        )

    console.print(table)
    console.print("[dim]Use `karaoke --import-radio <ID>` to import session tracks to library.[/dim]")
    return 0


def run_cli_import_radio_session(session_id: int) -> int:
    """CLI handler to import a radio session."""
    console = Console()
    console.print(f"[bold cyan]Importing radio session #{session_id} into library...[/bold cyan]")
    try:
        res = import_radio_session(session_id)
        imported = res.get("imported_tracks", 0)
        slices = res.get("audio_slices", 0)
        console.print(f"[bold green]Successfully imported {imported} tracks ({slices} audio slices)![/bold green]")
        for t in res.get("tracks", []):
            audio_info = f"[dim](audio: {Path(t['audio_slice']).name})[/dim]" if t.get("audio_slice") else "[dim](metadata only)[/dim]"
            lyrics_info = "[green]✓ lyrics[/green]" if t.get("lyrics") else "[dim]no lyrics[/dim]"
            yt_info = "[cyan]✓ yt[/cyan]" if t.get("youtube") else ""
            console.print(f"  • {t['artist']} - {t['title']} {audio_info} {lyrics_info} {yt_info}")
        return 0
    except Exception as exc:
        console.print(f"[bold red]Import failed: {exc}[/bold red]")
        return 1
