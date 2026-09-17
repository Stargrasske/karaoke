"""Fill in a track's genre and tone when it has neither, without being asked.

Both labels existed only where someone had done something: genre from pressing
`k` or from a bulk script, tone from a bulk script. So a track played for the
first time stayed unlabelled indefinitely, which is exactly the track a listener
is looking at.

**What it will and will not do is decided by cost.**

- *Tone* is free: the lyrics are already in the database and the sentence
  embedding is already loaded for lyric search, so it runs on any track with
  real words and no stored tone.
- *Genre* needs audio. Where a CLAP embedding exists, or the audio is already
  in the cache, that is a few seconds in a worker and it runs. Where it is not
  -- a Spotify track with nothing downloadable -- the only way to get audio is
  to **record 45 seconds of it**, and starting a recording because a song came
  on is not something a program should decide by itself. `k` stays the way to
  ask for that.

Never re-labels. A stored label is a decision, and quietly replacing it on
every play would make the library unstable in a way nobody asked for; the bulk
scripts take `--overwrite` for when a re-label is actually wanted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .logger import log


def _has_cached_audio(track_id: int, conn) -> Optional[Path]:
    """The downloaded audio for a track, if it is already on disk."""
    from . import localcache
    from .config import settings

    rows = conn.execute(
        "SELECT url FROM sources WHERE track_id = %s AND url LIKE '%%youtu%%'",
        (track_id,)).fetchall()
    directory = Path(settings.youtube_dir)
    if not directory.is_dir():
        return None
    for row in rows:
        vid = localcache.extract_youtube_id(row["url"] or "")
        if not vid:
            continue
        for path in directory.glob(f"{vid}.*"):
            if path.is_file():
                return path
    return None


def missing(track_id: int, conn) -> set[str]:
    """Which labels this track could gain right now, without recording.

    Returns a subset of ``{"tone", "genre"}``. Empty means either it already
    has them or there is nothing to work from.
    """
    from . import clap_vector, localcache, tone as tone_mod

    want: set[str] = set()

    if localcache.tone_for(track_id, conn) is None:
        row = conn.execute(
            "SELECT plain_lyrics, source FROM lyrics"
            " WHERE track_id = %s AND kind = 'approved'", (track_id,)).fetchone()
        if row and len((row["plain_lyrics"] or "").strip()) >= tone_mod.MIN_CHARS:
            from .librarysearch import is_transcribed

            if not is_transcribed(row["source"] or ""):
                want.add("tone")

    if localcache.genre_for(track_id, conn) is None and clap_vector.available():
        # Either an embedding already exists, or there is audio to make one
        # from. Recording is deliberately not counted as "available audio".
        from .search import clap_vector_for

        if clap_vector_for(track_id) is not None or _has_cached_audio(track_id, conn):
            want.add("genre")
    return want


def label_tone(track_id: int, conn) -> Optional[str]:
    """Read and store this track's lyric tone. Returns the label, or None."""
    from . import localcache, tone as tone_mod

    row = conn.execute(
        "SELECT plain_lyrics, source FROM lyrics"
        " WHERE track_id = %s AND kind = 'approved'", (track_id,)).fetchone()
    if not row:
        return None
    verdict = tone_mod.classify_lyrics(row["plain_lyrics"] or "",
                                       row["source"] or "",
                                       tone_mod.label_vectors())
    if verdict is None:
        return None
    localcache.ensure_tone_table(conn)
    localcache.record_tone(track_id, verdict, conn)
    return verdict.short


def label_genre(track_id: int, conn) -> Optional[str]:
    """Embed the track's audio if needed, then store its genre."""
    from . import clap_vector, genre as genre_mod, localcache
    from .search import clap_vector_for

    vector = clap_vector_for(track_id)
    if vector is None:
        audio = _has_cached_audio(track_id, conn)
        if audio is None:
            return None
        vector = clap_vector.embed_audio(str(audio))
        if vector is None:
            return None
        _store_embedding(track_id, vector, conn)

    verdict = genre_mod.classify(vector, genre_mod.label_vectors())
    if verdict is None:
        return None
    localcache.ensure_genre_table(conn)
    localcache.record_genre(track_id, verdict, conn)
    return verdict.genre


def _store_embedding(track_id: int, vector: list[float], conn) -> None:
    """Keep the embedding, so the next track like this one costs nothing."""
    from datetime import datetime, timezone

    from . import clap_vector

    try:
        from .osclient import client

        os_client = client()
        clap_vector.ensure_index(os_client)
        row = conn.execute(
            "SELECT artist, title, COALESCE(album, '') AS album"
            "  FROM tracks WHERE track_id = %s", (track_id,)).fetchone()
        os_client.index(
            index=clap_vector.CLAP_INDEX, id=clap_vector.doc_id(track_id),
            body=clap_vector.build_doc(
                track_id=track_id, artist=row["artist"] if row else "",
                title=row["title"] if row else "", album=row["album"] if row else "",
                vector=vector,
                embedded_at=datetime.now(timezone.utc).isoformat()))
    except Exception:
        # The label is the point; the index is a cache. Losing it costs a few
        # seconds next time, not the result.
        log.debug("could not store the embedding for %s", track_id, exc_info=True)


def run(track_id: int, conn) -> dict[str, str]:
    """Fill in whatever is missing and cheap. Returns what was added.

    Every step is best-effort: this runs behind a track that is playing, and
    nothing here is worth interrupting that for.
    """
    added: dict[str, str] = {}
    wanted = missing(track_id, conn)
    if "tone" in wanted:
        try:
            label = label_tone(track_id, conn)
            if label:
                added["tone"] = label
        except Exception:
            log.debug("auto tone failed for %s", track_id, exc_info=True)
    if "genre" in wanted:
        try:
            label = label_genre(track_id, conn)
            if label:
                added["genre"] = label
        except Exception:
            log.debug("auto genre failed for %s", track_id, exc_info=True)
    if added:
        log.info("auto-classified track %s: %s", track_id,
                 ", ".join(f"{k}={v}" for k, v in sorted(added.items())))
    return added
