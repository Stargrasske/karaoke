"""Asynchronous post-processing task publisher using RabbitMQ.

The post-processing pipeline fills in two derived assets for a track that the
foreground app doesn't compute inline:

- **Audio analysis** (musical key + tempo/BPM + energy/brightness), stored in
  ``track_analysis``. Requires the downloaded audio file in the YouTube cache.
- **Word-level timing** (Enhanced LRC), upgrading line-level synced lyrics using
  YouTube json3 captions.

``needs_postprocessing`` reports what is missing; ``enqueue_if_needed`` publishes
a task to RabbitMQ when anything is; the ``postprocess_worker`` consumes them.
"""
from __future__ import annotations

import json
import os
import psycopg
from psycopg import Connection, Cursor
from typing import Optional

import pika

from .logger import log

QUEUE_NAME = "karaoke-postprocess"
ORCHESTRATOR_ENV = "KARAOKE_ORCHESTRATOR"


def orchestrator() -> str:
    """Selected background orchestrator (`celery` by default, `legacy` fallback)."""
    return os.environ.get(ORCHESTRATOR_ENV, "celery").strip().lower() or "celery"


def needs_postprocessing(track_id: int, conn: Connection) -> list[str]:
    """Return the list of pending post-processing tasks for a track.

    Possible values: ``"analysis"`` (no key/BPM row), ``"timings"`` (approved
    synced lyrics lack Enhanced LRC word tags) and ``"sync"`` (approved lyrics
    are plain text with no timings at all). Empty list = nothing to do.
    """
    from . import track_analysis
    from .upgrade_timings import has_word_timings

    pending: list[str] = []
    cur = conn.cursor()

    # 1. Audio analysis (key/BPM). Missing if there's no track_analysis row.
    try:
        track_analysis.ensure_schema(conn)
        analysis = track_analysis.get_analysis(track_id, conn)
        if analysis is None or not analysis.bpm:
            pending.append("analysis")
    except Exception:
        pending.append("analysis")

    # 2. Word-level timing. Missing if approved synced lyrics carry no word tags.
    cur.execute(
        "SELECT synced_lyrics, plain_lyrics FROM lyrics"
        " WHERE track_id = %s AND kind = 'approved'",
        (track_id,),
    )
    row = cur.fetchone()
    if row and row["synced_lyrics"] and not has_word_timings(row["synced_lyrics"]):
        pending.append("timings")

    # 3. Any timing at all. Words with no timestamps cannot drive a karaoke
    # session -- and they are now arriving routinely from the player's own
    # lyrics panel, which supplies text and nothing else. Whisper can give
    # them a rhythm without being trusted for the words themselves.
    if row and not row["synced_lyrics"] and row["plain_lyrics"]:
        pending.append("sync")

    return pending


# What the worker can actually fetch audio (or captions) for. Both task kinds
# need it: analysis needs the file to examine, and the word-timing upgrade needs
# YouTube captions.
_DOWNLOADABLE_MARKERS = ("watch?v=", "youtu.be/", "youtube.com/embed/")


def is_downloadable(url: str) -> bool:
    """Whether the worker could obtain audio from this URL."""
    return any(marker in (url or "").lower() for marker in _DOWNLOADABLE_MARKERS)


def has_downloadable_source(track_id: int, conn: Connection) -> bool:
    """Whether any stored source for this track yields audio.

    A Spotify URL does not: there is no file behind it, which is the whole
    reason ``karaoke-sample`` and record mode exist.
    """
    try:
        rows = conn.execute(
            "SELECT url, kind FROM sources WHERE track_id = %s", (track_id,)
        ).fetchall()
    except psycopg.Error:
        return False
    for row in rows:
        if row["kind"] == "local":
            return True
        if is_downloadable(row["url"] or ""):
            return True
    return False


def enqueue_if_needed(
    artist: str, title: str, url: str = "",
    conn: Optional[Connection] = None,
) -> bool:
    """Publish a post-processing task only if the track actually needs work.

    Looks up the track by artist/title, checks ``needs_postprocessing``, and
    enqueues when non-empty. Best-effort; never raises to the caller.
    """
    from . import localcache

    own = conn is None
    c = conn or localcache.connect()
    try:
        track_id = localcache.find_track_id(artist, title, c)
        if track_id is None:
            # Unknown track: still enqueue so the worker can resolve+download
            # it -- but only if there is something to download.
            if url and not is_downloadable(url):
                log.debug("not enqueuing %s - %s: no downloadable source",
                          artist, title)
                return False
            return publish_postprocess_task(artist, title, url)
        pending = needs_postprocessing(track_id, c)
        if not pending:
            return False
        # The worker cannot analyse what it cannot fetch. However, if the track
        # has plain lyrics needing sync or timing upgrades, we allow enqueuing
        # because Celery will automatically search YouTube to find a source!
        if not (is_downloadable(url) or has_downloadable_source(track_id, c)):
            if "sync" not in pending and "timings" not in pending:
                log.info("skipping post-process for %s - %s: no downloadable audio"
                         " (sample it instead)", artist, title)
                return False
        return publish_postprocess_task(artist, title, url)
    except Exception as exc:
        log.debug("enqueue_if_needed skipped: %s", exc)
        return False
    finally:
        if own:
            c.close()


def publish_postprocess_task(artist: str, title: str, url: str = "") -> bool:
    """Publish a track post-processing task to RabbitMQ.

    Returns True if successfully published, False otherwise.
    Safe: catches pika exceptions so the TUI/app never crashes.
    """
    if not (artist or title):
        return False

    payload = {
        "artist": artist.strip(),
        "title": title.strip(),
        "url": url.strip() if url else ""
    }

    if orchestrator() != "legacy":
        try:
            from .tasks import enqueue_postprocess
            task_id = enqueue_postprocess(payload)
            log.info("Published post-processing Celery task %s: %s - %s",
                     task_id, artist, title)
            return task_id
        except Exception as exc:
            log.debug("Celery publish skipped (unreachable/failed): %s", exc)
            return None

    host = os.environ.get("RABBITMQ_HOST", "localhost")
    user = os.environ.get("RABBITMQ_USER", "guest")
    password = os.environ.get("RABBITMQ_PASS", "guest")

    try:
        credentials = pika.PlainCredentials(user, password)
        # Bounded 3s connection timeout so we don't hang the TUI thread if unreachable
        parameters = pika.ConnectionParameters(
            host=host,
            credentials=credentials,
            connection_attempts=1,
            retry_delay=1,
            socket_timeout=3,
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=QUEUE_NAME, durable=True)

        channel.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=json.dumps(payload),
            properties=pika.BasicProperties(
                delivery_mode=2
            )
        )
        connection.close()
        log.info("Published post-processing task to RabbitMQ: %s - %s", artist, title)
        return True
    except Exception as exc:
        log.debug("RabbitMQ publish skipped (unreachable/failed): %s", exc)
        return False
