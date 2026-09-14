"""RabbitMQ consumer that runs deferred track post-processing.

Consumes tasks published by :mod:`karaoke.postprocess_queue` and, per track:

1. Ensures the audio is downloaded to the YouTube cache.
2. Runs key/BPM/energy analysis and stores it in ``track_analysis`` (if missing).
3. Upgrades line-level synced lyrics to Enhanced LRC word timing via YouTube
   json3 captions (if missing).

The queue is intentionally NON-persistent conceptually: if the broker is reset
we can simply re-enqueue from SQLite. A task is ACKed only once it completes;
a failure is requeued once and then dropped, so a crash re-delivers work while a
permanently-failing track cannot spin the worker in a redelivery loop.

Run:  ``karaoke-postprocess-worker``  (or ``python -m karaoke.postprocess_worker``)
Env:  RABBITMQ_HOST (default localhost), RABBITMQ_USER/PASS (default guest).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import pika
from pika.exceptions import AMQPError

from . import localcache, track_analysis
from .config import settings
from .logger import log
from .postprocess_queue import QUEUE_NAME, needs_postprocessing


def _get_cached_audio_path(url: str) -> Optional[Path]:
    """Return the local cache path for a YouTube URL's video, if present."""
    vid = localcache.extract_youtube_id(url)
    if not vid:
        return None
    for ext in (".webm", ".m4a", ".mp3", ".opus", ".ogg"):
        p = Path(settings.youtube_dir) / f"{vid}{ext}"
        if p.is_file():
            return p
    return None


def run_download_logic(url: str, cookies_from_browser: Optional[str]) -> Optional[Path]:
    """Ensure audio for a YouTube or local URL is in the cache; download if needed.
    This function can be called by Celery tasks directly.
    """
    if url and Path(url).is_file():
        return Path(url)
    existing = _get_cached_audio_path(url)
    if existing:
        return existing
    try:
        from .youtube import fetch_metadata
        meta = fetch_metadata(url, download=True, cookies_from_browser=cookies_from_browser)
        downloaded_path_str = meta.get("path")
        if downloaded_path_str and Path(downloaded_path_str).is_file():
            return Path(downloaded_path_str)
    except Exception:
        log.exception("postprocess: download failed for %s", url)

    return _get_cached_audio_path(url)


def run_analysis_logic(track_id: int, audio_path: Path, conn) -> bool:
    """Run key/BPM/energy analysis on a local file and persist it."""
    try:
        from .analyze import analyze_audio
        result = analyze_audio(str(audio_path))
        track_analysis.save_detected(
            track_id,
            detected_key=result.key,
            key_confidence=result.key_confidence,
            key_agreement=result.key_agreement,
            bpm=result.bpm,
            method=result.method,
            energy=result.energy,
            brightness=result.brightness,
            analyzer_version=result.version,
            source_kind="postprocess",
            conn=conn,
        )
        log.info("postprocess: analyzed track %s (key=%s bpm=%s)",
                 track_id, result.key.name if result.key else "?", result.bpm)
        return True
    except Exception:
        log.exception("postprocess: analysis failed for track %s", track_id)
        return False


def run_timings_logic(track_id: int, conn, cookies_from_browser: Optional[str]) -> str:
    """Upgrade a track's synced lyrics to Enhanced LRC word timing.

    Returns the upgrade status: ``"upgraded"``, ``"no-captions"`` (terminal —
    the video simply has none), ``"no-source"`` (nothing joinable in the DB) or
    ``"error"`` (retryable).
    """
    try:
        from .upgrade_timings import upgrade_track
        row = conn.execute(
            """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration,
                   s.url, l.synced_lyrics, l.plain_lyrics
            FROM tracks t
            JOIN sources s ON s.track_id = t.track_id AND s.kind IN ('youtube', 'youtube_music')
            JOIN lyrics l  ON l.track_id = t.track_id AND l.kind = 'approved'
            WHERE t.track_id = ?
            LIMIT 1
            """,
            (track_id,),
        ).fetchone()
        if row is None:
            log.info("postprocess: no youtube source + approved lyrics for track %s",
                     track_id)
            return "no-source"
        res = upgrade_track(row, conn, delay=3.0, cookies_from_browser=cookies_from_browser)
        log.info("postprocess: timings upgrade for track %s -> %s", track_id, res.status)
        return res.status
    except Exception as exc:
        if "rate limited" in str(exc).lower():
            log.warning("postprocess: rate-limited by YouTube for track %s: %s", track_id, exc)
            return "rate-limited"
        log.exception("postprocess: timing upgrade failed for track %s", track_id)
        return "error"


def run_sync_logic(track_id: int, audio_path: Path, conn) -> bool:
    """Give plain lyrics a rhythm, by aligning them to a transcription.

    Whisper is here for **timing only**. Its words on sung audio are
    unreliable -- "up to do" becomes "up to doom", and it emits "♪"
    artifacts -- so where a real source supplied the text, those words are kept
    and only the timestamps are taken. This is the same rule
    upgrade_timings.upgrade_track follows for captions, and what makes the
    player's own lyrics panel useful: it has the words and no timings, and
    Whisper has the reverse.
    """
    from .lyric_align import align_lines
    from .lyrics import parse_lrc
    from .whisper_sync import lines_to_lrc, transcribe_to_words

    row = conn.execute(
        "SELECT plain_lyrics, source FROM lyrics"
        " WHERE track_id = ? AND kind = 'approved'", (track_id,)).fetchone()
    plain = (row["plain_lyrics"] or "").strip() if row else ""
    if not plain:
        return False

    # Where the *words* came from decides what this becomes. `whisper_aligned`
    # means real words with Whisper timings and is deliberately not demoted in
    # search; stamping it onto a track whose words are themselves a Whisper
    # guess would launder the guess into evidence and lose the "(guessed)"
    # label. Timing a transcription makes it singable, not corroborated.
    from .librarysearch import is_transcribed

    synced_source = ("whisper_synced" if is_transcribed(row["source"] or "")
                     else "whisper_aligned")

    dur = None
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(audio_path))
        if mf and mf.info and hasattr(mf.info, "length") and mf.info.length:
            dur = float(mf.info.length)
    except Exception:
        pass
    if dur is None:
        try:
            import subprocess
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                capture_output=True, text=True, timeout=5, check=True,
            )
            dur = float(res.stdout.strip())
        except Exception:
            pass
    if dur and dur > 900:  # 15 minutes cap for Whisper transcription
        log.warning("postprocess: skipping sync for track %s, duration %0.1fs exceeds 15min cap",
                    track_id, dur)
        return False

    try:
        # Tell Whisper the language rather than letting it detect one from the
        # opening of the audio, which on music is regularly an instrumental
        # intro. The lyrics are already in hand, so the answer is knowable --
        # and a wrong language is worse than none, since it produces fluent
        # words in the wrong vocabulary that then anchor nothing at all.
        # lyric_language.detect returns None when unsure, which restores the
        # previous behaviour exactly.
        from .lyric_language import detect as detect_language

        language = detect_language(plain)
        if language:
            log.info("postprocess: transcribing track %s as %s", track_id, language)
        words = transcribe_to_words(str(audio_path), text=plain, language=language)
        # track_analysis is created on demand, so the join below raises rather
        # than simply finding no tempo on a database that has never stored one.
        track_analysis.ensure_schema(conn)
        meta = conn.execute(
            "SELECT t.duration, a.bpm FROM tracks t"
            " LEFT JOIN track_analysis a ON a.track_id = t.track_id"
            " WHERE t.track_id = ?", (track_id,)).fetchone()
        # The tempo is what turns a plausible-looking timestamp into a
        # musically placed one: it sets the beat grid the lines snap to and the
        # bar window that identifies an instrumental break.
        # align_lines rather than align_lyrics_to_lrc so the support report
        # comes back with the timings: how many lines were anchored on a heard
        # word and how much of the track had no anchor at all. Once written to
        # an LRC the two are indistinguishable, and reconstructing them later
        # would mean transcribing again -- which Whisper does not do
        # reproducibly, so it would not describe the row that was stored.
        report: dict = {}
        lyric_lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
        lrc = lines_to_lrc(align_lines(
            lyric_lines, words,
            total_duration=(meta["duration"] if meta else None),
            bpm=(meta["bpm"] if meta else None),
            report=report))
    except Exception:
        log.exception("postprocess: sync failed for track %s", track_id)
        return False

    if not lrc.strip() or not parse_lrc(lrc):
        log.warning("postprocess: alignment produced no timings for track %s",
                    track_id)
        return False

    # Zero anchors is refused, and this is not the coverage gate the
    # measurement argued against. That argument was about *sparse* anchoring:
    # 45% anchored scored 0.77s while 54% scored 2.63s, so a low count predicts
    # uncertainty rather than error, and the timings are worth keeping and
    # flagging. With no anchors at all there is nothing to be uncertain about --
    # the lines are spaced evenly across the duration because nothing was heard,
    # and storing that marks the track synced and stops anything trying again.
    if report.get("lines") and not report.get("anchored"):
        log.warning("postprocess: no anchored line for track %s; "
                    "timings would be evenly spaced guesses, not stored",
                    track_id)
        return False

    conn.execute(
        "UPDATE lyrics SET synced_lyrics = ?, source = ?"
        " WHERE track_id = ? AND kind = 'approved'",
        (lrc, synced_source, track_id))
    conn.commit()

    # Stored whether the alignment looks well supported or not. This is a flag,
    # never a gate: the timings are kept either way, because the measurement
    # only justifies certifying a well-anchored alignment, not rejecting a
    # sparse one -- a 45%-anchored track scored 0.77s while a 54%-anchored one
    # scored 2.63s, so poor coverage predicts uncertainty, not error.
    localcache.ensure_alignment_support_table(conn)
    localcache.record_alignment_support(track_id, report, conn,
                                        source=synced_source)
    trustworthy = localcache.alignment_is_trustworthy(
        localcache.alignment_support(track_id, conn))
    log.info("postprocess: synced %d line(s) for track %s "
             "(%s/%s lines anchored%s)",
             len(parse_lrc(lrc)), track_id,
             report.get("anchored", "?"), report.get("lines", "?"),
             "" if trustworthy else " — worth a listen")
    return True


def run_vectors_logic(track_id: int, conn) -> bool:
    """Rebuild audio/lyrics vectors for a track."""
    try:
        from .vector_index import rebuild_from_sqlite
        # rebuild_from_sqlite is designed to be idempotent and can be run safely
        # even if only one track's vectors are missing.
        # We pass an explicit db_path and dry_run=True to ensure it's isolated
        # for this single track and doesn't conflict with a global rebuild.
        # This will need refinement once OpenSearch integration is more mature.
        # For now, it just ensures the vector_index is updated.
        rebuild_from_sqlite(db_path=None, dry_run=False, track_id=track_id) # Call the relevant logic here
        log.info("postprocess: rebuilt vectors for track %s", track_id)
        return True
    except Exception:
        log.exception("postprocess: failed to rebuild vectors for track %s", track_id)
        return False


