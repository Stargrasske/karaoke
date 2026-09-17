"""Decompile a recording into the database.

:mod:`karaoke.recorder` captures audio and markers; :mod:`karaoke.recording_slice`
turns the markers into a track list. This is the step that acts on it: cut each
confident segment out of the captured audio, analyse it for key and tempo, index
an audio feature vector, and store the result against the track.

Runs offline at full speed, so an evening's listening analyses in minutes rather
than in real time.

Two things make the cutting non-obvious:

- **Segments are dated on the track's timeline, not the recording's.** A marker
  says "we were 290s into this song", which dates the song's start to 290s
  before the recording may even have begun. Slices are therefore clamped to the
  span actually captured, and a segment with too little audio left after
  clamping is skipped rather than analysed from a fragment.
- **Audio is split across segment files.** Capture writes a new FLAC every ten
  minutes, so a track routinely straddles two of them and has to be concatenated
  before it can be cut.

Analysis needs librosa and essentia, which live in the isolated audio venv. As
elsewhere, their absence degrades to "unavailable" rather than raising.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import localcache
from .logger import log
from .recorder import SEGMENT_SECONDS
from .recording_slice import Segment, is_confident, segments

# Marks the analysis as recording-derived, so it is never mistaken for one done
# on a downloaded master.
METHOD_SUFFIX = "+recording"

# Below this there is not enough audio left after clamping for a key estimate
# to mean anything. Matches sample_audio's floor for the same reason.
MIN_AUDIO_S = 20.0

# Audio is kept after analysis rather than deleted, because analysis is not the
# only thing that needs it: lyric alignment happens later, when a track has
# words but no timings, and a Spotify-only track has no other source of audio
# at all. Deleting immediately made those two steps mutually exclusive.
#
# Retention is time first, then size. A week covers the gap between hearing a
# track and getting round to its lyrics; the cap is the backstop for a week of
# heavy listening.
RETAIN_DAYS = 7.0
MAX_TOTAL_BYTES = 10 * 1024 * 1024 * 1024

_SEG_NAME = re.compile(r"seg-(\d{8})-(\d{6})\.flac$")


@dataclass(frozen=True)
class SegmentFile:
    """One captured file and where it sits on the wall clock."""

    path: Path
    start_wall: float
    duration: float

    @property
    def end_wall(self) -> float:
        return self.start_wall + self.duration


def probe_duration(path: Path) -> Optional[float]:
    """Length of an audio file in seconds.

    ffprobe's ``format=duration`` is not usable on its own here: the segment
    muxer streams FLAC without seeking back to patch the header, so every
    finished segment reports ``N/A`` and the final one reports the *session*
    length rather than its own. Falling back to decoding is slow but exact, and
    only ever needed for one file per recording — see :func:`segment_files`.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        if out and out.upper() != "N/A":
            return float(out)
    except (subprocess.SubprocessError, FileNotFoundError, ValueError):
        return None
    return decoded_duration(path)


def decoded_duration(path: Path) -> Optional[float]:
    """Exact length, by decoding. Used when the header carries no duration."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-stats", "-i", str(path), "-f", "null", "-"],
            capture_output=True, text=True, timeout=300)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    # -stats writes "time=HH:MM:SS.ms" progress to stderr; the last one is the end.
    matches = re.findall(r"time=(\d+):(\d\d):(\d\d(?:\.\d+)?)", proc.stderr or "")
    if not matches:
        return None
    hours, minutes, seconds = matches[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def segment_files(directory: Path) -> list[SegmentFile]:
    """The captured files, in order, placed on the wall clock.

    Capture names each file for the instant it was opened (``-strftime``), which
    is what lets the audio be located from a marker with no extra bookkeeping and
    with no dependence on the recorder still running.
    """
    starts: list[tuple[float, Path]] = []
    for path in sorted(directory.glob("seg-*.flac")):
        match = _SEG_NAME.search(path.name)
        if not match:
            continue
        try:
            stamp = time.mktime(time.strptime(f"{match.group(1)}{match.group(2)}",
                                              "%Y%m%d%H%M%S"))
        except ValueError:
            continue
        starts.append((stamp, path))
    if not starts:
        return []
    starts.sort()

    # Each segment runs until the next one begins. That is exact, costs nothing,
    # and sidesteps the missing duration headers entirely -- only the final
    # segment has no successor and has to be measured.
    found: list[SegmentFile] = []
    for i, (stamp, path) in enumerate(starts):
        if i + 1 < len(starts):
            duration = starts[i + 1][0] - stamp
        else:
            # Decoded, not probed: the final segment's header carries the whole
            # session's length rather than its own, so it is wrong without
            # being N/A and would silently stretch the span by hours.
            duration = decoded_duration(path) or 0.0
            # A segment cannot outlast the muxer's own cut point; anything
            # longer means the measurement is wrong, not the recording.
            ceiling = SEGMENT_SECONDS * 1.5
            if duration > ceiling:
                log.warning("final segment %s measured %.0fs; capping at %.0fs",
                            path.name, duration, ceiling)
                duration = ceiling
            if duration <= 0:
                log.debug("could not measure final segment %s", path)
                continue
        found.append(SegmentFile(path=path, start_wall=stamp, duration=duration))
    return found


def recording_span(files: list[SegmentFile]) -> Optional[tuple[float, float]]:
    """The wall-clock range actually captured, or None if nothing was."""
    if not files:
        return None
    return (min(f.start_wall for f in files), max(f.end_wall for f in files))


def clamp(segment: Segment, span: tuple[float, float]) -> Optional[tuple[float, float]]:
    """Trim a segment to the audio that exists, or None if too little does.

    A track identified partway through is dated from before the recording
    started, so without this the cut would silently begin at whatever the
    earliest audio happened to be and the analysis would describe the wrong part
    of the song.
    """
    start = max(segment.start_wall, span[0])
    end = min(segment.end_wall, span[1])
    if (end - start) < MIN_AUDIO_S:
        return None
    return (start, end)


def cut(files: list[SegmentFile], start_wall: float, end_wall: float,
        dest: Path) -> bool:
    """Extract [start_wall, end_wall) from the captured files into ``dest``."""
    overlapping = [f for f in files
                   if f.end_wall > start_wall and f.start_wall < end_wall]
    if not overlapping:
        return False
    overlapping.sort(key=lambda f: f.start_wall)
    offset = max(0.0, start_wall - overlapping[0].start_wall)
    duration = max(0.0, end_wall - start_wall)
    if duration <= 0:
        return False

    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "concat.txt"
        listing.write_text("".join(
            f"file '{f.path}'\n" for f in overlapping))
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            # After -i so the seek is accurate rather than keyframe-aligned;
            # these are short cuts, so the decode cost is irrelevant.
            "-ss", f"{offset:.3f}", "-t", f"{duration:.3f}",
            "-ac", "2", "-ar", "44100", str(dest),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True,
                           timeout=max(120.0, duration * 2))
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            log.debug("cut failed: %s", exc)
            return False
    return dest.is_file() and dest.stat().st_size > 0


def _index_audio_vector(track_id: int, recording_id: int, segment: Segment,
                        wav: Path, analysis) -> bool:
    """Index the segment's audio feature vector, if OpenSearch is reachable."""
    from . import audio_vector

    vector = audio_vector.extract(str(wav))
    if vector is None:
        return False
    try:
        from .osclient import client as os_client
        client = os_client()
        audio_vector.ensure_audio_index(client)
        key = getattr(getattr(analysis, "key", None), "name", "") or ""
        client.index(
            index=audio_vector.AUDIO_INDEX,
            id=audio_vector.audio_doc_id(track_id, segment.start_wall),
            body=audio_vector.build_audio_doc(
                track_id=track_id, recording_id=recording_id,
                artist=segment.artist, title=segment.title, vector=vector,
                recorded_at=datetime.fromtimestamp(
                    segment.start_wall, timezone.utc).isoformat(),
                duration_s=segment.duration,
                detected_key=key,
                bpm=getattr(analysis, "bpm", None)),
        )
        return True
    except Exception:
        # OpenSearch is optional infrastructure; the SQLite analysis is the
        # part that must not be lost.
        log.debug("audio vector indexing failed", exc_info=True)
        return False


def analyse_segment(segment: Segment, files: list[SegmentFile],
                    span: tuple[float, float], recording_id: int,
                    *, conn=None) -> Optional[str]:
    """Analyse one segment. Returns a status line for the caller to print."""
    from . import track_analysis
    from .analyze import analyze_audio

    window = clamp(segment, span)
    if window is None:
        return f"  skip  {segment.artist} - {segment.title} (too little audio)"

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "segment.wav"
        if not cut(files, window[0], window[1], wav):
            return f"  fail  {segment.artist} - {segment.title} (cut failed)"

        result = analyze_audio(str(wav))
        if result.key is None and result.bpm is None:
            return (f"  fail  {segment.artist} - {segment.title} "
                    f"(analysis unavailable; is the audio venv installed?)")

        own = conn is None
        c = conn or localcache.connect()
        try:
            track_id = localcache.find_track_id(segment.artist, segment.title, c)
            if track_id is None:
                from .lyrics import Lyrics
                localcache.add_track_and_lyrics(segment.artist, segment.title,
                                                Lyrics(), conn=c)
                track_id = localcache.find_track_id(segment.artist, segment.title, c)
            if track_id is None:
                return f"  fail  {segment.artist} - {segment.title} (no track row)"
            track_analysis.save_detected(
                track_id,
                detected_key=result.key,
                key_confidence=result.key_confidence,
                key_agreement=result.key_agreement,
                bpm=result.bpm,
                method=f"{result.method}{METHOD_SUFFIX}",
                energy=result.energy,
                brightness=result.brightness,
                analyzer_version=result.version,
                source_kind="recording",
                conn=c,
            )
        finally:
            if own:
                c.close()

        indexed = _index_audio_vector(track_id, recording_id, segment, wav, result)

    key_name = result.key.name if result.key else "unknown"
    bpm = f"{result.bpm:.0f}" if result.bpm else "?"
    vec = " +vector" if indexed else ""
    return f"  ok    {segment.artist} - {segment.title}: {key_name}, {bpm} bpm{vec}"


def load_recording(recording_id: int, conn=None) -> Optional[dict]:
    own = conn is None
    c = conn or localcache.connect()
    try:
        row = c.execute("SELECT * FROM recordings WHERE recording_id = %s",
                        (recording_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            c.close()


def discard_audio(recording_id: int, *, conn=None) -> int:
    """Delete a recording's audio, keeping its markers. Returns bytes freed."""
    record = load_recording(recording_id, conn)
    if not record:
        return 0
    directory = Path(record["dir"])
    freed = 0
    for path in directory.glob("seg-*.flac"):
        try:
            freed += path.stat().st_size
            path.unlink()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass
    own = conn is None
    c = conn or localcache.connect()
    try:
        c.execute("UPDATE recordings SET status = 'discarded' WHERE recording_id = %s"
                  " AND status != 'recording'", (recording_id,))
        c.commit()
    finally:
        if own:
            c.close()
    return freed


def audio_bytes(record) -> int:
    """Bytes of captured audio still on disk for a recording row."""
    directory = Path(record["dir"])
    if not directory.is_dir():
        return 0
    try:
        return sum(f.stat().st_size for f in directory.glob("seg-*.flac")
                   if f.is_file())
    except OSError:
        return 0


def prune_recordings(*, retain_days: float = RETAIN_DAYS,
                     max_bytes: int = MAX_TOTAL_BYTES,
                     conn=None) -> list[str]:
    """Drop old or excess recording audio, keeping every marker.

    Markers are tiny and are what the track list is derived from, so they are
    never deleted -- only the audio goes. A pruned recording can still be
    inspected with ``--show``; it just cannot be re-analysed.

    Age first, then size, oldest last-played first. ``keep_audio`` pins a
    recording against the age rule, since that is an explicit choice, but not
    against the size cap: a pinned session cannot be allowed to fill the disk.
    """
    own = conn is None
    c = conn or localcache.connect()
    notes: list[str] = []
    try:
        rows = c.execute(
            "SELECT recording_id, dir, status, keep_audio, started_at"
            " FROM recordings WHERE status != 'recording'"
            " ORDER BY started_at"
        ).fetchall()
        live = [(r, audio_bytes(r)) for r in rows]
        live = [(r, n) for r, n in live if n > 0]
        if not live:
            return notes

        cutoff = time.time() - retain_days * 86400.0
        for record, size in list(live):
            if record["keep_audio"]:
                continue
            if float(record["started_at"] or 0.0) < cutoff:
                freed = discard_audio(int(record["recording_id"]), conn=c)
                notes.append(f"  pruned recording {record['recording_id']}: "
                             f"{freed / 1e6:.0f} MB, older than "
                             f"{retain_days:.0f} days")
                live = [(r, n) for r, n in live
                        if r["recording_id"] != record["recording_id"]]

        total = sum(n for _, n in live)
        # Oldest first: the newest session is the one most likely to still be
        # wanted for the lyrics of something just heard.
        for record, size in list(live):
            if total <= max_bytes:
                break
            freed = discard_audio(int(record["recording_id"]), conn=c)
            total -= freed
            notes.append(f"  pruned recording {record['recording_id']}: "
                         f"{freed / 1e6:.0f} MB, over the "
                         f"{max_bytes / 1e9:.0f} GB cap")
    finally:
        if own:
            c.close()
    return notes


def analyse(recording_id: int, *, keep: Optional[bool] = None) -> list[str]:
    """Analyse every confident segment of a recording. Returns status lines."""
    from . import recorder

    record = load_recording(recording_id)
    if record is None:
        return [f"no such recording: {recording_id}"]
    if record["status"] == "recording":
        return [f"recording {recording_id} is still running; stop it first"]

    files = segment_files(Path(record["dir"]))
    span = recording_span(files)
    if span is None:
        return [f"recording {recording_id} has no readable audio"]

    marks = recorder.load_marks(recording_id)
    found = segments(marks)
    if not found:
        return [f"recording {recording_id} has no identified tracks"]

    lines = [f"recording {recording_id}: {len(found)} track(s) from "
             f"{len(files)} segment file(s)"]
    analysed = 0
    for segment in found:
        if not is_confident(segment):
            lines.append(f"  skip  {segment.artist} - {segment.title} "
                         f"(unreliable: {segment.marks} marks, "
                         f"spread {segment.spread:.1f}s)")
            continue
        status = analyse_segment(segment, files, span, recording_id)
        lines.append(status)
        if status and status.strip().startswith("ok"):
            analysed += 1

    with localcache.connect() as c:
        c.execute("UPDATE recordings SET status = 'analysed' WHERE recording_id = %s",
                  (recording_id,))
        c.commit()

    # The audio stays. Lyric alignment needs it later, and for a Spotify-only
    # track there is no other source of it -- deleting here made analysis and
    # alignment mutually exclusive. Retention handles the disk instead.
    lines.append(f"  {analysed} analysed; audio kept in {record['dir']}")
    if keep is not True:
        lines.extend(prune_recordings())
    return lines


def show(recording_id: int) -> list[str]:
    """The derived track list, without analysing anything."""
    from . import recorder
    from .recording_slice import describe

    record = load_recording(recording_id)
    if record is None:
        return [f"no such recording: {recording_id}"]
    marks = recorder.load_marks(recording_id)
    found = segments(marks)
    ok, total = recorder.mark_count(recording_id)
    lines = [f"recording {recording_id}  status={record['status']}  "
             f"{ok}/{total} marks identified"]
    files = segment_files(Path(record["dir"]))
    span = recording_span(files)
    if span:
        lines.append(f"  audio: {len(files)} file(s), "
                     f"{(span[1] - span[0]) / 60:.1f} min captured")
    if not found:
        lines.append("  (no tracks identified)")
    for segment in found:
        line = f"  {describe(segment)}"
        if span and clamp(segment, span) is None:
            line += "  [not enough audio captured]"
        lines.append(line)
    return lines


def listing() -> list[str]:
    """One line per recording."""
    with localcache.connect() as c:
        rows = c.execute(
            "SELECT r.recording_id, r.started_at, r.status, r.dir,"
            "       (SELECT count(*) FROM recording_marks m"
            "         WHERE m.recording_id = r.recording_id) AS marks"
            " FROM recordings r ORDER BY r.recording_id"
        ).fetchall()
    if not rows:
        return ["no recordings yet"]
    out = []
    for row in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["started_at"]))
        directory = Path(row["dir"])
        size = sum(f.stat().st_size for f in directory.glob("seg-*.flac")
                   if f.is_file()) if directory.is_dir() else 0
        out.append(f"{row['recording_id']:>4}  {when}  {row['status']:<10} "
                   f"{row['marks']:>3} marks  {size / 1e6:>6.0f} MB")
    return out


def recording_main(argv: Optional[list[str]] = None) -> int:
    """Run the ``karaoke-recording`` CLI."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="karaoke-recording",
        description="Inspect and decompile record-mode sessions")
    ap.add_argument("--list", action="store_true", help="list all recordings")
    ap.add_argument("--show", type=int, metavar="ID",
                    help="show the derived track list without analysing")
    ap.add_argument("--analyse", "--analyze", type=int, metavar="ID",
                    dest="analyse", help="analyse a recording into the database")
    ap.add_argument("--discard", type=int, metavar="ID",
                    help="delete a recording's audio, keeping its markers")
    ap.add_argument("--prune", action="store_true",
                    help=f"drop audio older than {RETAIN_DAYS:.0f} days or over "
                         f"{MAX_TOTAL_BYTES / 1e9:.0f} GB, keeping all markers")
    ap.add_argument("--keep", action="store_true",
                    help="with --analyse, keep the audio afterwards")
    args = ap.parse_args(argv)

    if args.list:
        print("\n".join(listing()))
        return 0
    if args.show is not None:
        print("\n".join(show(args.show)))
        return 0
    if args.analyse is not None:
        # Route the mutating action through the control API so the CLI, TUI and
        # a future web UI all trigger analysis the same way; falls back to an
        # in-process call when no server is running.
        from .api_client import ApiClient
        res = ApiClient().record_analyse(args.analyse, keep=bool(args.keep))
        status = res.get("status", "unknown")
        if status == "analysed":
            # Ran synchronously in-process (no server up).
            print("\n".join(res.get("lines", [])))
        elif status == "accepted":
            print(f"recording {args.analyse}: analysis dispatched")
        elif status == "unreachable":
            print("\n".join(analyse(args.analyse, keep=args.keep or None)))
        else:
            print(f"recording {args.analyse}: {res.get('detail', status)}")
        return 0
    if args.prune:
        notes = prune_recordings()
        print("\n".join(notes) if notes else "nothing to prune")
        return 0
    if args.discard is not None:
        from .api_client import ApiClient
        res = ApiClient().record_discard_audio(args.discard)
        freed = res.get("freed_bytes")
        if freed is not None:
            print(f"freed {freed / 1e6:.0f} MB")
        else:
            print(f"recording {args.discard}: {res.get('detail', res.get('status', 'unknown'))}")
        return 0
    ap.error("give --list, --show, --analyse, --discard or --prune")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(recording_main())
