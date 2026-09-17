"""Record mode: capture the output continuously and mark what is playing.

``karaoke-sample`` analyses one track at a time, in real time, on demand. This
is the unattended version: leave it running for an evening and it captures
everything coming out of the speakers while, in parallel, asking songrec every
so often what is playing.

Each identification is written as a **marker** — "at this wall-clock instant we
were N seconds into track X". :mod:`karaoke.recording_slice` turns those back
into a track list afterwards, which is what lets hours of audio be cut into
songs and analysed offline at full speed instead of one track per listen.

Two independent readers of the same monitor source is fine: PipeWire allows it,
so ffmpeg and songrec do not contend. The monitor is also a different device
from the microphone, so record mode composes with radio mode rather than
fighting it for an input.

The recording is a means to metadata, not a library. ``keep_audio`` is off by
default and the analysis worker deletes the audio once it has extracted what it
needs.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import localcache
from .logger import log

# One segment per ten minutes: a crash costs a segment rather than the session,
# and slicing can address whole files instead of seeking a multi-gigabyte one.
SEGMENT_SECONDS = 600

# How often to ask what is playing. Songs are minutes long and songrec needs a
# few seconds of audio, so this is frequent enough to catch every track and to
# give each one several corroborating markers.
IDENTIFY_EVERY_S = 45.0

# songrec's own listening window.
IDENTIFY_TIMEOUT_S = 20

# Caps, so a session forgotten overnight cannot fill the disk. FLAC of stereo
# 44.1k runs about 300 MB/hour, so 8 hours is roughly 2.4 GB.
MAX_HOURS = 8.0
MAX_BYTES = 6 * 1024 * 1024 * 1024

# After this many consecutive failed identifications, slow down. Silence, a
# podcast or a paused player should not mean hammering Shazam all night.
BACKOFF_AFTER = 3
BACKOFF_FACTOR = 4.0


@dataclass
class Session:
    """A running recording: the ffmpeg process and its identification thread."""

    recording_id: int
    directory: Path
    source: str
    process: subprocess.Popen
    stop: threading.Event
    started_mono: float


_sessions: dict[int, Session] = {}
_lock = threading.Lock()


class RecorderError(RuntimeError):
    """Raised when a recording cannot be started."""


def recordings_dir() -> Path:
    from .config import settings
    return Path(settings.data_dir) / "recordings"


def active_sessions() -> list[int]:
    """Recording ids currently running in this process."""
    with _lock:
        return sorted(_sessions)


def is_running(recording_id: int) -> bool:
    with _lock:
        session = _sessions.get(recording_id)
    return session is not None and session.process.poll() is None


def elapsed(recording_id: int) -> Optional[float]:
    """Seconds this session has been recording, or None if it is not running."""
    with _lock:
        session = _sessions.get(recording_id)
    if session is None:
        return None
    return time.monotonic() - session.started_mono


def session_source(recording_id: int) -> Optional[str]:
    """The PipeWire source a running session is recording."""
    with _lock:
        session = _sessions.get(recording_id)
    return session.source if session else None


def session_directory(recording_id: int) -> Optional[Path]:
    with _lock:
        session = _sessions.get(recording_id)
    return session.directory if session else None


def directory_size(directory: Path) -> int:
    """Bytes currently written for a recording."""
    try:
        return sum(f.stat().st_size for f in directory.glob("*") if f.is_file())
    except OSError:
        return 0


def _ffmpeg_cmd(source: str, directory: Path) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "pulse", "-i", source,
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
        # Wall-clock names, so the timeline survives a crash: the segments can
        # be placed on the same clock the markers use without any state.
        "-strftime", "1",
        "-c:a", "flac",
        str(directory / "seg-%Y%m%d-%H%M%S.flac"),
    ]


def start(source: str = "", *, keep_audio: bool = False,
          note: Optional[str] = None,
          conn: Optional[object] = None) -> Session:
    """Begin recording the playing output and marking what is on it."""
    from .sample_audio import monitor_source

    if not shutil.which("ffmpeg"):
        raise RecorderError("ffmpeg is not installed")
    src = source or monitor_source()
    if not src:
        raise RecorderError("nothing is playing; no output to record")

    own = conn is None
    c = conn or localcache.connect()
    try:
        started = time.time()
        directory = recordings_dir() / time.strftime("%Y%m%d-%H%M%S",
                                                     time.localtime(started))
        directory.mkdir(parents=True, exist_ok=True)
        cur = c.execute(
            "INSERT INTO recordings (started_at, source, dir, status, keep_audio, note)"
            " VALUES (%s, %s, %s, 'recording', %s, %s) RETURNING recording_id",
            (started, src, str(directory), 1 if keep_audio else 0, note),
        )
        recording_id = int(cur.fetchone()["recording_id"])
        c.commit()
    finally:
        if own:
            c.close()

    try:
        proc = subprocess.Popen(_ffmpeg_cmd(src, directory),
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    except OSError as exc:
        _finish(recording_id, "failed", note=str(exc)[:200])
        raise RecorderError(f"could not start ffmpeg: {exc}") from exc

    session = Session(recording_id=recording_id, directory=directory, source=src,
                      process=proc, stop=threading.Event(),
                      started_mono=time.monotonic())
    with _lock:
        _sessions[recording_id] = session

    thread = threading.Thread(target=_identify_loop, args=(session,), daemon=True)
    thread.start()
    log.info("recording %d started: %s -> %s", recording_id, src, directory)
    return session


def _identify_loop(session: Session) -> None:
    """Ask what is playing, on a loop, until the session stops."""
    from . import identify

    misses = 0
    while not session.stop.is_set():
        # A dead recorder makes further markers meaningless: they would
        # describe audio nobody captured.
        if session.process.poll() is not None:
            log.warning("recording %d: ffmpeg exited", session.recording_id)
            _finish(session.recording_id, "failed", note="ffmpeg exited early")
            _forget(session.recording_id)
            return
        if _over_limit(session):
            stop(session.recording_id)
            return

        ref = None
        try:
            ref = identify.identify_live(mic=False, timeout=IDENTIFY_TIMEOUT_S,
                                         source=session.source)
        except Exception:
            log.debug("recording %d: identify failed", session.recording_id,
                      exc_info=True)

        add_mark(session.recording_id, ref)
        misses = 0 if (ref and ref.title) else misses + 1

        # Back off rather than hammer Shazam through a podcast or silence.
        wait = IDENTIFY_EVERY_S
        if misses >= BACKOFF_AFTER:
            wait *= BACKOFF_FACTOR
        session.stop.wait(wait)


def _over_limit(session: Session) -> bool:
    """Whether the session has hit its time or disk cap."""
    if (time.monotonic() - session.started_mono) > MAX_HOURS * 3600.0:
        log.warning("recording %d: %.0fh cap reached", session.recording_id,
                    MAX_HOURS)
        return True
    if directory_size(session.directory) > MAX_BYTES:
        log.warning("recording %d: size cap reached", session.recording_id)
        return True
    return False


def add_mark(recording_id: int, ref: Optional[object], *,
             conn: Optional[object] = None) -> None:
    """Record one identification attempt, successful or not.

    Failures are stored rather than dropped: a gap in the markers is evidence
    about the recording — silence, speech, an unknown track — and discarding it
    would make the timeline look continuous when it is not.
    """
    own = conn is None
    c = conn or localcache.connect()
    try:
        ok = bool(ref is not None and getattr(ref, "title", ""))
        c.execute(
            "INSERT INTO recording_marks"
            " (recording_id, at_wall, at_mono, at_offset, artist, title, ok)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (recording_id, time.time(), time.monotonic(),
             getattr(ref, "offset", None) if ok else None,
             getattr(ref, "artist", "") if ok else "",
             getattr(ref, "title", "") if ok else "",
             1 if ok else 0),
        )
        c.commit()
    except Exception:
        log.debug("could not store mark for recording %d", recording_id,
                  exc_info=True)
    finally:
        if own:
            c.close()


def _finish(recording_id: int, status: str, *, note: str = "") -> None:
    try:
        with localcache.connect() as c:
            c.execute(
                "UPDATE recordings SET ended_at = %s, status = %s,"
                " note = COALESCE(NULLIF(%s, ''), note) WHERE recording_id = %s",
                (time.time(), status, note, recording_id),
            )
            c.commit()
    except Exception:
        log.debug("could not finish recording %d", recording_id, exc_info=True)


def _forget(recording_id: int) -> None:
    with _lock:
        _sessions.pop(recording_id, None)


def stop(recording_id: int) -> None:
    """Stop a running recording and close out its row."""
    with _lock:
        session = _sessions.get(recording_id)
    if session is None:
        _finish(recording_id, "complete")
        return

    session.stop.set()
    proc = session.process
    if proc.poll() is None:
        # SIGTERM, not SIGKILL: ffmpeg handles it by finalising the segment it
        # is writing. Killing outright leaves the last FLAC without its header,
        # losing the tail of the session.
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        except OSError:
            pass
    _finish(recording_id, "complete")
    _forget(recording_id)
    log.info("recording %d stopped", recording_id)


def reconcile_stale(conn: Optional[object] = None) -> list[int]:
    """Close out rows left at 'recording' by a process that did not exit cleanly.

    A session only lives in the process that started it, so a row still marked
    'recording' with nothing running is a crash or a kill -- not a live capture.
    Leaving it makes the listing lie about what is happening, and hides the fact
    that its audio is complete and analysable.

    Returns the ids that were closed out.
    """
    own = conn is None
    c = conn or localcache.connect()
    try:
        rows = c.execute(
            "SELECT recording_id FROM recordings WHERE status = 'recording'"
        ).fetchall()
        running = set(active_sessions())
        stale = [int(r["recording_id"]) for r in rows
                 if int(r["recording_id"]) not in running]
        for recording_id in stale:
            c.execute(
                "UPDATE recordings SET status = 'complete', ended_at = %s,"
                " note = COALESCE(note, 'closed out: capture was not running')"
                " WHERE recording_id = %s",
                (time.time(), recording_id))
        if stale:
            c.commit()
            log.info("closed out %d stale recording(s): %s", len(stale), stale)
        return stale
    finally:
        if own:
            c.close()


def stop_all() -> None:
    """Stop every running session — used when the app exits."""
    for recording_id in active_sessions():
        stop(recording_id)


def mark_count(recording_id: int, conn: Optional[object] = None) -> tuple[int, int]:
    """(identified, total) markers stored for a recording."""
    own = conn is None
    c = conn or localcache.connect()
    try:
        row = c.execute(
            "SELECT count(*) AS total, COALESCE(sum(ok), 0) AS ok"
            " FROM recording_marks WHERE recording_id = %s",
            (recording_id,),
        ).fetchone()
        return (int(row["ok"]), int(row["total"])) if row else (0, 0)
    finally:
        if own:
            c.close()


def load_marks(recording_id: int, conn: Optional[object] = None) -> list:
    """Markers for a recording, as :class:`recording_slice.Mark` objects."""
    from .recording_slice import Mark

    own = conn is None
    c = conn or localcache.connect()
    try:
        rows = c.execute(
            "SELECT at_wall, at_offset, artist, title, ok FROM recording_marks"
            " WHERE recording_id = %s ORDER BY at_wall",
            (recording_id,),
        ).fetchall()
    finally:
        if own:
            c.close()
    return [Mark(at_wall=float(r["at_wall"]), artist=r["artist"] or "",
                 title=r["title"] or "", at_offset=r["at_offset"],
                 ok=bool(r["ok"])) for r in rows]
