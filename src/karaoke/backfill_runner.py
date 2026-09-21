"""The business logic for the automated lyric backfill system."""
from __future__ import annotations
import re
import time
from typing import Optional

from . import localcache
from . import youtube
from . import web
from .identify import SongRef
from .lyrics import Lyrics, clean_title, fetch_lrclib
from .player import get_synced
from .source_select import select_best_source

# How many YouTube results to weigh before picking. The right upload is often
# not first — for "Kyuss - Apothecaries' Weight" the official "- Topic" audio
# ranks above a guitar cover only once several results are compared.
SEARCH_CANDIDATES = 5

def run(
    *,
    retry_failed: bool = False,
    failed_only: bool = False,
    limit: Optional[int] = None,
) -> None:
    """Run the backfill process.

    By default only ``pending`` gaps are processed. ``retry_failed`` also picks
    up rows previously marked ``failed`` — most such failures are transient
    (YouTube/search throttling), and the recorded ``last_error`` says which.
    ``failed_only`` reprocesses just those. ``invalid`` rows are never selected:
    their metadata cannot resolve, so retrying only burns search quota.
    """
    if failed_only:
        statuses = ("failed",)
    elif retry_failed:
        statuses = ("pending", "failed")
    else:
        statuses = ("pending",)
    with localcache.connect() as conn:
        placeholders = ",".join(["%s"] * len(statuses))
        sql = (
            f"SELECT gap_id, artist, title FROM lyric_gaps "
            f"WHERE status IN ({placeholders}) ORDER BY attempts ASC, gap_id ASC"
        )
        params: list = list(statuses)
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        gaps = conn.execute(sql, params).fetchall()

    ok = failed = 0
    for gap in gaps:
        print(f"Processing gap: {gap['artist']} - {gap['title']}")
        try:
            _process_gap(gap['gap_id'], gap['artist'], gap['title'])
            _update_gap_status(gap['gap_id'], 'processed')
            ok += 1
        except Exception as e:
            print(f"  Failed: {e}")
            _update_gap_status(gap['gap_id'], 'failed', error=f"{type(e).__name__}: {e}")
            failed += 1

    print(f"\nBackfill done: {ok} processed, {failed} failed, {len(gaps)} total.")


# A real song's lyrics run to hundreds of characters. Anything shorter is a
# navigation stub, a cookie banner or a truncated parse — not a usable result.
MIN_LYRICS_CHARS = 250

# Genius song pages are ``/<artist>-<title>-lyrics``; /albums/ and /artists/
# pages (and the bare domain) carry no single song's words.
_GENIUS_SONG_URL = re.compile(r"^https?://genius\.com/[^/]+-lyrics/?$", re.IGNORECASE)


def _is_genius_song_url(url: str) -> bool:
    """True for a Genius single-song lyrics page (not an album/artist index)."""
    return bool(_GENIUS_SONG_URL.match(url.strip()))


def _slug_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric word tokens, for loose slug comparison."""
    return {w for w in re.split(r"[^a-z0-9]+", text.casefold()) if len(w) > 2}


def _genius_url_matches(url: str, artist: str, title: str) -> bool:
    """Check a Genius slug plausibly refers to this artist AND title.

    Web search happily returns a different artist's song of the same name —
    J. Cole's "Life Sentence" for a band's track, Reese Lansangan's "Mall Rats"
    for "Dead Mall - RATS". Genius slugs are ``<artist>-<title>-lyrics``, so the
    artist is required either to lead the slug or to appear in full; the title
    must share a real word. That rejects those two without demanding an exact
    match on spelling variants.
    """
    slug_text = url.rstrip("/").rsplit("/", 1)[-1]
    slug_words = [w for w in re.split(r"[^a-z0-9]+", slug_text.casefold()) if len(w) > 2]
    slug = set(slug_words)

    title_tokens = _slug_tokens(title)
    if not title_tokens or not (title_tokens & slug):
        return False

    artist_tokens = _slug_tokens(artist)
    if not artist_tokens:
        return True  # nothing to check the artist against
    leads = bool(slug_words) and slug_words[0] in artist_tokens
    return leads or artist_tokens <= slug


def _find_lyrics(artist: str, title: str) -> Lyrics:
    """Find lyrics for a track: LRCLIB first, then Genius scrape.

    LRCLIB is the primary, reliable source (no scraping); Genius is the
    fallback. The full :class:`Lyrics` is returned — crucially including
    ``synced_raw`` — so callers can use LRCLIB's own timings instead of
    re-deriving them with Whisper. Returns empty ``Lyrics`` when nothing
    usable is found.
    """
    # 1. LRCLIB (also try a cleaned title without "- Remastered" etc.).
    # Prefer a synced result over a plain-only one from a different spelling.
    fallback = Lyrics()
    for t in {title, clean_title(title)}:
        ly = fetch_lrclib(artist, t)
        if ly.synced_raw:
            print(f"    LRCLIB synced hit for '{artist} - {t}'")
            return ly
        if ly.plain and not fallback.plain:
            print(f"    LRCLIB plain hit for '{artist} - {t}'")
            fallback = ly
    if fallback.plain:
        return fallback

    # 2. Genius fallback via web search + container parse
    print("  LRCLIB miss; searching Genius...")
    web_results = web.search(f"{artist} {title} lyrics genius")
    for result in web_results:
        url = result["url"]
        if "genius.com" not in url or not _is_genius_song_url(url):
            continue
        if not _genius_url_matches(url, artist, title):
            print(f"    Skipping unrelated Genius link: {url}")
            continue
        print(f"    Trying Genius link: {url}")
        text = web.fetch_genius_lyrics(url)
        if len(text) < MIN_LYRICS_CHARS:
            print(f"    Rejected: only {len(text)} chars (min {MIN_LYRICS_CHARS})")
            continue
        # Genius is plain text only — no timings to preserve.
        return Lyrics(plain=text, source="genius")
    return Lyrics()


def _process_gap(gap_id: int, artist: str, title: str) -> None:
    """Process a single lyric gap: find lyrics, then sync them to the track.

    When the source already carries timings (LRCLIB synced/LRC), they are stored
    as-is: downloading the audio to re-derive them with Whisper is slow, and
    Whisper's alignment is worse than LRCLIB's. Whisper is the fallback for
    plain-text-only sources.
    """
    # 1. Find lyrics FIRST (cheap) — no point downloading audio without them.
    print("  Searching for lyrics...")
    lyrics = _find_lyrics(artist, title)
    if not (lyrics.synced_raw or lyrics.plain):
        raise RuntimeError("No lyrics found (LRCLIB or Genius)")

    # 2. Already timed? Store and stop — no download, no transcription.
    if lyrics.synced_raw:
        print("  Source lyrics are already synced; storing directly.")
        with localcache.connect() as conn:
            localcache.add_track_and_lyrics(artist, title, lyrics, conn=conn)
        _verify_stored(artist, title)
        print("    Done.")
        return

    lyrics_text = lyrics.plain

    # 3. Plain text only: find + download audio on YouTube to align against.
    #    Several candidates, then pick deliberately: the top hit is regularly a
    #    cover, a live cut or a whole-album rip, and Whisper cannot produce
    #    usable timings from any of those.
    print(f"  Searching YouTube for '{artist} - {title}'...")
    yt_results = youtube.search(f"{artist} - {title}", limit=SEARCH_CANDIDATES)
    if not yt_results:
        raise RuntimeError("No YouTube results found")

    best = select_best_source(yt_results, artist, title, lyrics.duration)
    if best is None:
        raise RuntimeError(
            f"No YouTube result matched (checked {len(yt_results)}; "
            f"expected ~{lyrics.duration:.0f}s)" if lyrics.duration
            else f"No usable YouTube result (checked {len(yt_results)})"
        )
    yt_url = best["url"]
    dur = best.get("duration")
    print(f"    Picked: {best.get('title', '')[:60]!r}"
          f" [{best.get('uploader') or '?'}"
          f"{f', {dur:.0f}s' if dur else ''}]")
    print(f"    {yt_url}")

    print("  Downloading audio...")
    audio_path = youtube.download(yt_url)
    print(f"    Downloaded to: {audio_path}")

    # 4. Generate synced lyrics by aligning the plain text to the audio.
    print("  Generating synced lyrics (Whisper)...")
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix=".txt") as f:
        f.write(lyrics_text)
        lyrics_file_path = f.name

    try:
        ref = SongRef(
            artist=artist,
            title=title,
            path=str(audio_path),
            source="backfill",
            url=yt_url,
        )
        get_synced(
            ref,
            force_transcribe=True,
            lyrics_file=lyrics_file_path,
        )
    finally:
        os.remove(lyrics_file_path)

    _verify_stored(artist, title)
    print("    Done.")


def _verify_stored(artist: str, title: str) -> None:
    """Raise unless the track now carries lyrics in the local cache.

    Both store paths can silently no-op (Whisper can return an empty transcript),
    and a gap marked 'processed' with nothing stored would never be retried.
    """
    with localcache.connect() as conn:
        cached = localcache.get_cached_lyrics(artist, title, conn=conn)
    if not (cached and (cached.synced_raw or cached.plain)):
        raise RuntimeError("sync produced no stored lyrics")


def _update_gap_status(gap_id: int, status: str, *, error: Optional[str] = None) -> None:
    """Update the status of a lyric gap, recording the attempt and any error."""
    with localcache.connect() as conn:
        conn.execute(
            """
            UPDATE lyric_gaps
            SET status = %s, processed_at = %s, attempts = attempts + 1, last_error = %s
            WHERE gap_id = %s
            """,
            (status, time.time(), error, gap_id),
        )
        conn.commit()
