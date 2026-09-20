"""Analyse what is playing by recording it, when there is no file to analyse.

Post-processing needs audio. For a YouTube-sourced track that is fine — the
audio can be downloaded. For anything played through Spotify it is not: there
is no file, so key/BPM analysis has nothing to work on and those tracks sit in
the backlog permanently.

The audio is right there, though — it is coming out of the speakers. Capturing
the sink *monitor* gives a clean digital copy of exactly what is playing: no
microphone, no room noise, no second recogniser competing for an input device.
The monitor is a different source from the microphone, so this runs happily
alongside radio mode rather than fighting it for the mic.

What comes back is an excerpt, not the track, and the numbers should be read
that way. Key is a global property and survives excerpting well. Tempo is
reliable for steady material and less so where the track changes tempo — the
stored ``method`` records that the analysis came from a sample.

Capture is real time: 45 seconds of audio takes 45 seconds. There is no way to
batch this, which is why it is driven from the TUI for the track in front of
you rather than run over a backlog.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .audio_backend import IS_WINDOWS, ffmpeg_input_args
from .logger import log

# Long enough for a stable key estimate and a few tempo periods, short enough
# not to be tedious: the user is waiting through it in real time.
DEFAULT_SECONDS = 45.0

# Below this an excerpt is too short for the key vote to mean anything.
# How often the abort check runs while recording.
POLL_SECONDS = 0.5

MIN_SECONDS = 20.0

# Marks the analysis as excerpt-derived wherever it is displayed, so a sampled
# result is never mistaken for a full-track one.
METHOD_SUFFIX = "+sample"


class CaptureAborted(RuntimeError):
    """The capture was stopped deliberately, not by a failure.

    Separate from CaptureError because it is not a problem to report: the
    caller asked to stop, usually because the track changed.
    """


class CaptureError(RuntimeError):
    """Raised when audio could not be captured."""


class AnalysisUnavailable(RuntimeError):
    """Raised when the audio analysis stack is not installed.

    Distinct from a failed analysis: the audio is fine, this interpreter simply
    cannot examine it.
    """


@dataclass(frozen=True)
class Sample:
    """A captured excerpt and where it came from."""

    path: Path
    seconds: float
    source: str


def _pactl(*args: str) -> str:
    try:
        proc = subprocess.run(["pactl", *args], capture_output=True, text=True,
                              timeout=5, check=True)
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""
    return proc.stdout.strip()


def playing_sink() -> str:
    """The sink audio is actually being played to, or '' if none is.

    Not simply the default sink: with a Bluetooth speaker paired alongside
    built-in speakers and a USB interface, the default is regularly not where
    the music is routed. The sink carrying a stream is the one that matters, so
    the first sink-input's sink wins, and the default is only the fallback.
    """
    first = _pactl("list", "short", "sink-inputs").splitlines()
    if first:
        fields = first[0].split()
        if len(fields) > 1:
            index = fields[1]
            for line in _pactl("list", "short", "sinks").splitlines():
                cols = line.split()
                if len(cols) > 1 and cols[0] == index:
                    return cols[1]
    default = _pactl("get-default-sink")
    return "" if default in ("", "@DEFAULT_SINK@") else default


def monitor_source(sink: str = "") -> str:
    """The monitor source for a sink — what that sink is outputting."""
    target = sink or playing_sink()
    return f"{target}.monitor" if target else ""


def capture(seconds: float = DEFAULT_SECONDS, *, dest: Optional[Path] = None,
            source: str = "",
            should_continue: "Callable[[], bool] | None" = None) -> Sample:
    """Record ``seconds`` of the given (or currently playing) monitor source.

    ``should_continue`` is polled while recording; returning False stops the
    capture and raises :class:`CaptureAborted`. That is what a caller passes to
    mean "the track changed" -- a 45-second sample easily spans a song
    boundary, and one that does would have its key, tempo and genre stored
    against whichever track happened to be playing when it began.
    """
    if seconds < MIN_SECONDS:
        raise CaptureError(
            f"need at least {MIN_SECONDS:.0f}s for a usable estimate")
    if not shutil.which("ffmpeg"):
        raise CaptureError("ffmpeg is not installed")

    windows_device = IS_WINDOWS and bool(source)
    src = source or monitor_source()
    if not src:
        raise CaptureError("nothing is playing; no output to record")

    target = Path(dest) if dest else Path(
        tempfile.mkstemp(prefix="karaoke-sample-", suffix=".wav")[1])
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *ffmpeg_input_args(src, windows_device=windows_device),
        "-t", str(seconds), "-ac", "2", "-ar", "44100",
        str(target),
    ]
    log.info("sampling %.0fs from %s", seconds, src)
    try:
        # Polled rather than waited on, so a track change can cut it short.
        # Forty-five seconds is long enough to span a song boundary, and a
        # sample that does spans two songs: its key, tempo and genre would be
        # stored against whichever track was named when it started.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise CaptureError("ffmpeg is not installed") from exc

    deadline = time.monotonic() + seconds + 30
    try:
        while proc.poll() is None:
            if time.monotonic() > deadline:
                proc.kill()
                raise CaptureError(f"capture timed out after {seconds:.0f}s")
            if should_continue is not None and not should_continue():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                target.unlink(missing_ok=True)
                raise CaptureAborted("the track changed while sampling")
            time.sleep(POLL_SECONDS)
    finally:
        if proc.poll() is None:                 # never leave ffmpeg orphaned
            proc.kill()
    if proc.returncode not in (0, None):
        stderr = (proc.stderr.read() if proc.stderr else "") or ""
        raise CaptureError(f"ffmpeg failed: {stderr.strip()[:200]}")

    if not target.is_file() or target.stat().st_size == 0:
        raise CaptureError("captured nothing; is the sink silent?")
    return Sample(path=target, seconds=seconds, source=src)


def _label_genre(track_id: int, wav_path, conn) -> bool:
    """Label the sampled excerpt's genre, if CLAP is available.

    Sampling is where this earns the most: a Spotify-only track has no
    downloadable audio, so the excerpt recorded off the monitor is the only
    copy that exists, and without it such a track can never be labelled at all.

    Best-effort throughout. A missing model or an unreachable index must not
    cost the key and tempo that were the point of sampling, so every failure
    returns False rather than raising.
    """
    from . import clap_vector, genre, localcache

    if not clap_vector.available():
        log.debug("CLAP unavailable; sample not labelled")
        return False
    try:
        vector = clap_vector.embed_audio(str(wav_path))
        if vector is None:
            return False
        verdict = genre.classify(vector, genre.label_vectors())
        if verdict is None:
            log.info("sample for track %s matched no genre label", track_id)
            return False
        localcache.ensure_genre_table(conn)
        localcache.record_genre(track_id, verdict, conn,
                                method=f"clap-zero-shot{METHOD_SUFFIX}")
        log.info("track %s labelled %s", track_id, genre.describe(verdict))
        return True
    except Exception:
        log.debug("genre labelling failed for track %s", track_id, exc_info=True)
        return False


def analyse_sample(sample: Sample, artist: str = "", title: str = "",
                   *, conn=None) -> "object":
    """Analyse a captured excerpt and, given a track, store the result.

    Returns the analyzer's own result object. Storage mirrors what
    ``karaoke-analyze --file`` does, including creating the track row when it
    does not exist yet — a Spotify-only track often has no row at all.
    """
    from . import localcache, track_analysis
    from .analyze import analyze_audio

    result = analyze_audio(str(sample.path))
    # analyze_audio degrades to method="unavailable" when librosa/essentia are
    # missing rather than raising. Storing that would write a row with a NULL
    # key and BPM that looks analysed, and would then be skipped by every
    # "needs analysis" query -- worse than no row at all.
    if result.key is None and result.bpm is None:
        raise AnalysisUnavailable(
            "audio analysis stack not installed (make install-audio)")
    if not (artist and title):
        return result

    own = conn is None
    c = conn or localcache.connect()
    try:
        track_id = localcache.find_track_id(artist, title, c)
        if track_id is None:
            from .lyrics import Lyrics
            localcache.add_track_and_lyrics(artist, title, Lyrics(), conn=c)
            track_id = localcache.find_track_id(artist, title, c)
        if track_id is not None:
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
                source_kind="sample",
                conn=c,
            )
            _label_genre(track_id, sample.path, c)
    finally:
        if own:
            c.close()
    return result


def sample_and_analyse(artist: str = "", title: str = "",
                       seconds: float = DEFAULT_SECONDS,
                       *, source: str = "", keep: bool = False, conn=None,
                       should_continue: "Callable[[], bool] | None" = None) -> "object":
    """Record the playing output and analyse it. Deletes the file unless kept."""
    sample = capture(seconds, source=source, should_continue=should_continue)
    try:
        return analyse_sample(sample, artist, title, conn=conn)
    finally:
        if not keep:
            sample.path.unlink(missing_ok=True)


def sample_main(argv: Optional[list[str]] = None) -> int:
    """Run the ``karaoke-sample`` CLI."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="karaoke-sample",
        description="Detect key/BPM by recording what is currently playing "
                    "(for Spotify and anything else with no downloadable audio)",
    )
    ap.add_argument("--seconds", "-t", type=float, default=DEFAULT_SECONDS,
                    help=f"how long to record (default {DEFAULT_SECONDS:.0f})")
    ap.add_argument("--artist", default="", help="track artist (to store)")
    ap.add_argument("--title", default="", help="track title (to store)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the recorded wav instead of deleting it")
    ap.add_argument("--list-sinks", action="store_true",
                    help="show which output would be recorded, and exit")
    args = ap.parse_args(argv)

    if args.list_sinks:
        sink = playing_sink()
        print(f"playing sink : {sink or '(nothing playing)'}")
        print(f"would record : {monitor_source(sink) or '-'}")
        return 0

    try:
        sample = capture(args.seconds)
    except CaptureError as exc:
        print(f"karaoke-sample: {exc}", file=__import__("sys").stderr)
        return 1

    print(f"Recorded {sample.seconds:.0f}s from {sample.source}")
    try:
        result = analyse_sample(sample, args.artist, args.title)
    finally:
        if not args.keep:
            sample.path.unlink(missing_ok=True)
        else:
            print(f"kept {sample.path}")

    key = result.key
    print(f"key: {key.name if key else 'unknown'} "
          f"(conf {result.key_confidence:.0%}, {result.key_agreement})")
    print(f"bpm: {result.bpm if result.bpm else 'unknown'}")
    if args.artist and args.title:
        print(f"stored for {args.artist} - {args.title}")
    else:
        print("(not stored: give --artist and --title)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(sample_main())
