"""Cross-platform song identification fallback using shazamio.

``songrec`` (see :mod:`karaoke.identify`) has no Windows build, so live
identification there falls back to `shazamio <https://pypi.org/project/shazamio/>`_
— a pure-Python client that reimplements the same fingerprint-then-query-Shazam
flow songrec uses, over plain HTTP. It works anywhere Python does (Windows,
macOS, Linux), needs no external binary of its own, and returns the same
``{"matches": [...], "tagid": ...}`` response shape songrec's ``-j`` output
does, which is why :func:`_parse_response` mirrors :mod:`karaoke.identify`'s
songrec parsing.

It still needs an audio *file* to analyse, so this module captures a short
clip via ffmpeg (through :mod:`karaoke.audio_backend` for the platform-correct
input args) before handing it to shazamio.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .logger import log

DEFAULT_CAPTURE_SECONDS = 8.0


def available() -> bool:
    """Whether shazamio is importable and ffmpeg is on PATH."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        import shazamio  # noqa: F401
    except ImportError:
        return False
    return True


def _capture_clip(source: str, seconds: float, dest: Path) -> bool:
    from .audio_backend import IS_WINDOWS, ffmpeg_input_args

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *ffmpeg_input_args(source, windows_device=IS_WINDOWS),
        "-t", str(seconds), "-ac", "1", "-ar", "44100",
        str(dest),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=seconds + 15, check=True)
    except (subprocess.SubprocessError, OSError):
        return False
    return dest.is_file() and dest.stat().st_size > 0


def _recognize(path: Path) -> Optional[dict[str, Any]]:
    from shazamio import Shazam

    async def _run() -> dict[str, Any]:
        return await Shazam().recognize(str(path))

    try:
        return asyncio.run(_run())
    except Exception:
        log.debug("shazamio recognize failed", exc_info=True)
        return None


def _parse_response(data: dict[str, Any], mono: float) -> Optional["SongRef"]:
    from .identify import SongRef, robust_offset

    track = data.get("track")
    if not track:
        return None
    offset = robust_offset(data.get("matches", []))
    return SongRef(
        artist=track.get("subtitle", ""),
        title=track.get("title", ""),
        source="shazamio",
        offset=offset,
        offset_mono=mono if offset is not None else None,
    )


def identify_live_shazamio(
    source: str, seconds: float = DEFAULT_CAPTURE_SECONDS, timeout: float = 30.0
) -> Optional["SongRef"]:
    """Capture ``seconds`` of ``source`` and identify it via shazamio.

    ``source`` is whatever :mod:`karaoke.audio_backend` expects on this
    platform (a Windows DirectShow device name, or a PulseAudio source on
    Linux — though Linux normally has ``songrec`` and never reaches here).
    Returns None on any failure (no network, no match, capture error) so
    callers can treat it exactly like a songrec miss.
    """
    if not available():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        clip = Path(tmp) / "clip.wav"
        if not _capture_clip(source, seconds, clip):
            return None
        mono = time.monotonic()
        data = _recognize(clip)
    if not data:
        return None
    return _parse_response(data, mono)
