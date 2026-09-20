"""Song identification: resolve a track from a file, a text query, or live audio.

Live identification reuses songrec (the same engine behind the `whatsong` tool):
it fingerprints microphone or monitor audio against Shazam and returns artist +
title, which we then use to look up lyrics.
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tags import extract_tags


@dataclass
class SongRef:
    """A resolved song plus optional sync metadata.

    `offset` and `offset_mono` are populated by live songrec identification so
    the player can anchor lyric time to the position heard in the room/output.
    """

    artist: str
    title: str
    album: str = ""
    duration: Optional[float] = None
    path: Optional[str] = None       # local file, if any
    url: Optional[str] = None        # origin URL, if any
    source: str = "unknown"          # file | query | songrec | index
    offset: Optional[float] = None   # position in the track (s) at match time
    offset_mono: Optional[float] = None  # time.monotonic() when offset was valid


def from_file(path: str | Path) -> SongRef:
    """Resolve a song reference from a local audio file's metadata tags."""
    t = extract_tags(path)
    return SongRef(
        artist=t.artist, title=t.title, album=t.album,
        duration=t.duration, path=t.path, source="file",
    )


def parse_query(text: str) -> SongRef:
    """Parse 'Artist - Title' (or just a title) into a SongRef."""
    if " - " in text:
        artist, title = text.split(" - ", 1)
        return SongRef(artist=artist.strip(), title=title.strip(), source="query")
    return SongRef(artist="", title=text.strip(), source="query")


def _default_source(mic: bool) -> Optional[str]:
    """Resolve the pactl source name: mic (default) or output monitor."""
    if not shutil.which("pactl"):
        return None
    if not mic:
        # Deliberately not `pactl get-default-sink`: with a Bluetooth speaker
        # paired alongside built-in output, the default sink is regularly not
        # where the music is routed, and listening to the wrong monitor records
        # silence. sample_audio picks the sink actually carrying a stream.
        from .sample_audio import monitor_source
        return monitor_source() or None
    try:
        out = subprocess.run(["pactl", "get-default-source"],
                             capture_output=True, text=True, timeout=5)
        name = out.stdout.strip()
        return name or None
    except (subprocess.SubprocessError, OSError):
        return None


def robust_offset(matches: list[dict]) -> Optional[float]:
    """Best position-in-track estimate from songrec matches.

    Shazam returns several candidate matches, each with an `offset` (seconds
    into the track). Outliers occur (e.g. a repeated chorus matching a distant
    position), so cluster the offsets and take the median of the largest tight
    cluster (within 5s of each other) rather than a single value.
    """
    offs = sorted(
        float(m["offset"]) for m in matches if m.get("offset") is not None
    )
    if not offs:
        return None
    if len(offs) == 1:
        return float(offs[0])
    # Greedy cluster: group offsets within 5s of the running cluster start.
    best: list[float] = []
    cur: list[float] = [offs[0]]
    for o in offs[1:]:
        if o - cur[0] <= 5.0:
            cur.append(o)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [o]
    if len(cur) > len(best):
        best = cur
    return float(statistics.median(best))


def identify_live(mic: bool = True, timeout: int = 30,
                  source: str = "") -> Optional[SongRef]:
    """Listen and identify the currently playing song via songrec.

    mic=True listens to the microphone (room audio); mic=False uses the
    output sink monitor (audio playing through this machine).
    Returns None if nothing is recognized. Populates `offset` (position in the
    track) and `offset_mono` (monotonic clock at that instant) for sync.
    """
    if not shutil.which("songrec"):
        raise RuntimeError("songrec not installed (emerge media-sound/songrec)")
    # An explicit source wins: a caller already recording a specific monitor
    # must identify against that same one, not whatever the default resolves to.
    src = source or _default_source(mic)
    cmd = ["songrec", "recognize", "-j"]
    if src:
        cmd = ["songrec", "recognize", "-d", src, "-j"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    mono = time.monotonic()  # capture the clock right after the recognizer returns
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        data = json.loads(out)
        track = data["track"]
    except (json.JSONDecodeError, KeyError):
        return None
    offset = robust_offset(data.get("matches", []))
    return SongRef(
        artist=track.get("subtitle", ""),
        title=track.get("title", ""),
        source="songrec",
        offset=offset,
        offset_mono=mono if offset is not None else None,
    )


def identify_file_fingerprint(path: str | Path, duration_s: float = 10.0) -> Optional[SongRef]:
    """Fingerprint a local audio file using songrec / Shazam.

    Extracts a short audio slice via ffmpeg and feeds it to `songrec recognize -j`.
    Returns None if songrec/ffmpeg is not installed, or if the file cannot be recognized.
    """
    if not shutil.which("songrec") or not shutil.which("ffmpeg"):
        return None

    p = Path(path)
    if not p.is_file():
        return None

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        slice_wav = Path(tmp) / "sample.wav"
        cmd_cut = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(p), "-ss", "10", "-t", str(duration_s),
            "-ac", "1", "-ar", "16000", str(slice_wav),
        ]
        try:
            subprocess.run(cmd_cut, capture_output=True, check=True, timeout=15)
        except Exception:
            return None

        # The file is a positional argument. `-d` is --audio-device, so the
        # old form asked songrec to treat the path as a microphone name: it
        # printed the device list to stdout, json.loads failed, and every
        # fingerprint attempt returned None. Whole-library scans reported
        # `fingerprinted: 0` for months without ever erroring.
        cmd_rec = ["songrec", "recognize", str(slice_wav), "-j"]
        try:
            # Shazam round-trip, not local work: 20s was tight enough to
            # time out on a slow response and look like a failed match.
            out = subprocess.run(cmd_rec, capture_output=True, text=True, timeout=60)
            data = json.loads(out.stdout.strip())
            track = data["track"]
            offset = robust_offset(data.get("matches", []))
            album = ""
            sections = track.get("sections", [])
            if sections and "metadata" in sections[0]:
                for meta in sections[0]["metadata"]:
                    if meta.get("title", "").lower() == "album":
                        album = meta.get("text", "")
                        break
            return SongRef(
                artist=track.get("subtitle", ""),
                title=track.get("title", ""),
                album=album,
                path=str(p),
                source="fingerprint",
                offset=offset,
            )
        except Exception:
            return None
