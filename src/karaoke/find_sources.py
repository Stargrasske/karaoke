"""Find a playable source for tracks that have lyrics but no URL.

Lyrics and sources arrive by different routes. Radio discovery and backfill
resolve a track by artist/title against LRCLIB, which fills in the *words* and
records no *URL*. Those tracks then look complete but cannot be played, and
post-processing cannot analyse them either — key/BPM needs the actual audio, so
the worker reports "no watchable URL" and drops the task.

This closes that gap: search YouTube per track and store the best match, using
the same verified picker the backfill uses so a wrong video is not saved.

Run:  ``karaoke-find-sources --dry-run``   to see what it would store
      ``karaoke-find-sources --limit 50``  to store them
"""
from __future__ import annotations

import argparse
import psycopg
from psycopg import Connection, Cursor
import time
from dataclasses import dataclass
from typing import Optional

from . import localcache, youtube
from .logger import log
from .lyrics import parse_lrc
from .source_select import select_best_source

# YouTube search is the rate-limited part; a short pause keeps a long run from
# looking like a scraper.
REQUEST_PAUSE_S = 1.0

# How many candidates to weigh per track. The right upload is often not first.
SEARCH_CANDIDATES = 5

# With no stored duration, the last synced lyric is a floor on the song's
# length: the track cannot be shorter than its own words.
#
# The ceiling needs more care than a bare multiple. A song whose last lyric
# lands early — an intro-only vocal, a long outro — would get an absurdly tight
# limit (a lyric at 1s would cap the song at 3s), so the allowance is whichever
# is larger of the multiple and a generous instrumental tail, and never more
# than any single track plausibly runs.
MAX_DURATION_FACTOR = 3.0
OUTRO_PAD_S = 300.0      # a very long instrumental tail
MAX_SONG_S = 1200.0      # 20 min; beyond this it is an album rip or a mix


@dataclass(frozen=True)
class Candidate:
    """A track needing a source, with whatever length evidence exists."""

    track_id: int
    artist: str
    title: str
    duration: Optional[float]       # exact, from the tracks table
    lyric_end: Optional[float]      # last synced lyric timestamp, a floor


def needs_source(conn: Connection,
                 limit: Optional[int] = None) -> list[Candidate]:
    """Tracks that have lyrics but no playable YouTube URL."""
    sql = """
        SELECT t.track_id, t.artist, t.title, t.duration,
               (SELECT l.synced_lyrics FROM lyrics l
                 WHERE l.track_id = t.track_id AND l.kind = 'approved'
                 LIMIT 1) AS synced
        FROM tracks t
        WHERE NOT EXISTS (
                -- Both YouTube URL forms count as sourced. Matching only
                -- "watch%sv=" would re-search a track already stored as a
                -- youtu.be short link, every run, forever.
                SELECT 1 FROM sources s
                 WHERE s.track_id = t.track_id
                   AND (s.url LIKE '%%watch%%v=%%' OR s.url LIKE '%%youtu.be/%%'))
          AND EXISTS (
                SELECT 1 FROM lyrics l
                 WHERE l.track_id = t.track_id AND l.kind = 'approved'
                   AND (length(COALESCE(l.synced_lyrics, '')) > 0
                        OR length(COALESCE(l.plain_lyrics, '')) > 0))
          AND length(TRIM(COALESCE(t.artist, ''))) > 0
          AND length(TRIM(COALESCE(t.title, ''))) > 0
        ORDER BY t.track_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    out = []
    for row in conn.execute(sql):
        lines = parse_lrc(row["synced"] or "")
        out.append(Candidate(
            track_id=int(row["track_id"]),
            artist=row["artist"], title=row["title"],
            duration=row["duration"] if row["duration"] else None,
            lyric_end=lines[-1][0] if lines else None,
        ))
    return out


def plausible_length(seconds: Optional[float], cand: Candidate) -> bool:
    """Whether a candidate's length is credible for this track.

    With an exact duration, ``select_best_source`` already applies a tight
    tolerance and this is not needed. Without one, the last synced lyric is
    still real evidence: the song runs at least that long, and not several
    times longer — which is what rejects album rips and compilations.
    """
    if seconds is None or cand.lyric_end is None:
        return True
    ceiling = min(MAX_SONG_S,
                  max(cand.lyric_end * MAX_DURATION_FACTOR,
                      cand.lyric_end + OUTRO_PAD_S))
    return cand.lyric_end <= seconds <= ceiling


def find_for(cand: Candidate) -> Optional[dict]:
    """Search YouTube and return the best acceptable result, or None."""
    query = f"{cand.artist} - {cand.title}"
    try:
        results = youtube.search(query, limit=SEARCH_CANDIDATES)
    except Exception as exc:
        log.debug("source search failed for %s: %s", query, exc)
        return None
    if not results:
        return None

    if cand.duration:
        # Exact length known: the picker's own tolerance is the right gate.
        return select_best_source(results, cand.artist, cand.title, cand.duration)

    usable = [r for r in results if plausible_length(r.get("duration"), cand)]
    return select_best_source(usable, cand.artist, cand.title, None)


def run(*, limit: Optional[int] = None, dry_run: bool = False,
        pause: float = REQUEST_PAUSE_S,
        conn: Optional[Connection] = None) -> tuple[int, int]:
    """Find and store sources. Returns (found, missed)."""
    own = conn is None
    c = conn or localcache.connect()
    try:
        candidates = needs_source(c, limit)
        print(f"{len(candidates)} tracks have lyrics but no playable source\n")
        found = missed = 0
        for cand in candidates:
            best = find_for(cand)
            label = f"{cand.artist} - {cand.title}"[:52]
            if best is None:
                print(f"  ---   {label}")
                missed += 1
            else:
                dur = best.get("duration")
                print(f"  ok    {label:<54}"
                      f"{best.get('uploader', '')[:22]:<23}"
                      f"{f'{dur:.0f}s' if dur else ''}")
                found += 1
                if not dry_run:
                    localcache.add_track_source(
                        cand.artist, cand.title, url=best["url"],
                        kind="youtube", conn=c)
            if pause:
                time.sleep(pause)
        print(f"\n{found} sourced, {missed} not found"
              f"{' (dry run — nothing stored)' if dry_run else ''}")
        return found, missed
    finally:
        if own:
            c.close()


def find_sources_main(argv: Optional[list[str]] = None) -> int:
    """Run the ``karaoke-find-sources`` CLI."""
    ap = argparse.ArgumentParser(
        prog="karaoke-find-sources",
        description="Find a playable YouTube source for tracks that have "
                    "lyrics but no URL",
    )
    ap.add_argument("--limit", type=int, default=None,
                    help="process at most N tracks")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be stored, store nothing")
    ap.add_argument("--pause", type=float, default=REQUEST_PAUSE_S,
                    help=f"seconds between searches (default {REQUEST_PAUSE_S})")
    args = ap.parse_args(argv)

    run(limit=args.limit, dry_run=args.dry_run, pause=args.pause)
    return 0


def resolve_and_save_source(track_id: int, conn: Connection) -> Optional[str]:
    """Find and save a YouTube source for a specific track by its track_id."""
    row = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, t.duration,
               (SELECT l.synced_lyrics FROM lyrics l
                 WHERE l.track_id = t.track_id AND l.kind = 'approved'
                 LIMIT 1) AS synced
        FROM tracks t
        WHERE t.track_id = %s
        """,
        (track_id,),
    ).fetchone()
    if not row:
        return None

    from .lyrics import parse_lrc
    lines = parse_lrc(row["synced"] or "")
    cand = Candidate(
        track_id=int(row["track_id"]),
        artist=row["artist"], title=row["title"],
        duration=row["duration"] if row["duration"] else None,
        lyric_end=lines[-1][0] if lines else None,
    )
    best = find_for(cand)
    if best and best.get("url"):
        localcache.add_track_source(
            cand.artist, cand.title, url=best["url"],
            kind="youtube", conn=conn)
        return best["url"]
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(find_sources_main())
