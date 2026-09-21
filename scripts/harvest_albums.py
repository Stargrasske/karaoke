"""Learn album metadata from YouTube Music's *song* entries.

The cheap sources are exhausted: ffprobe measured the cached audio, Spotify
answered for tracks it knows, and yt-dlp filled in durations from the stored
video ids. Album was still missing for most rows, and the reason turns out to
be *which id is stored*.

The library stores the id that was played, which is normally the music video.
YouTube Music treats a video and a song as different entries, and only the song
carries an album — its menu even offers "go to album", which a video's does not.
Asking yt-dlp about the stored id gets ``album=NA``; asking YouTube Music for
the same song gets the album::

    stored video  naW6-WxmMiU  -> album=NA          (label's video upload)
    YT Music song iZ_SwfLlpHo  -> album=Nevermind   (the song entry)

So this searches YouTube Music per track and reads the first song's metadata:
one yt-dlp call, about 1.6s, no playback and no audio. An earlier version drove
the player and read MPRIS instead, which was slower, made noise, and returned
nothing — a browser tab showing a video has no album to report.

The result is **verified against what was asked for** before being stored. A
search can drift to a different song, and writing its album against this track
would be a silent, permanent mistake. Nothing overwrites a stored value.

Run:  ``python scripts/harvest_albums.py --limit 5``
      ``python scripts/harvest_albums.py --limit 400 --apply``
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import detect, localcache                   # noqa: E402

# db_cleanup already knows how to compare credited artists -- "Feint ft Veela"
# against "Feint, Veela" -- which detect.same_track deliberately does not, since
# there strictness is what keeps the wrong player's track from being followed.
_DBC = importlib.util.spec_from_file_location(
    "db_cleanup", Path(__file__).resolve().parent / "db_cleanup.py")
db_cleanup = importlib.util.module_from_spec(_DBC)
_DBC.loader.exec_module(db_cleanup)

SEARCH_URL = "https://music.youtube.com/search?q={query}"

# How many search results to consider. The first is usually right, but when it
# is not, the song is often second or third -- 53 rows had no album purely
# because result one was a different track. Verification decides which is ours,
# so widening costs a little time and no accuracy.
CANDIDATES = 3

# yt-dlp resolves a search in about 1.6s. A short pause keeps a long run from
# looking like a scraper without making it materially slower.
PAUSE_S = 0.4
TIMEOUT_S = 60.0

# Commit this often. SQLite holds a write lock from a transaction's first write
# until it commits, so batching the whole run into one transaction locks the
# database out for the entire time it takes -- which for 400 tracks is over ten
# minutes of the TUI and everything else being unable to write.
COMMIT_EVERY = 10

_FIELDS = "%(album)s|%(artist)s|%(track)s|%(duration)s"

# A featured credit migrates between the artist and title fields depending on
# the source: "Kungs - I FEEL SO BAD ft. Ephemerals" here,
# "Kungs, Ephemerals - I Feel So Bad" there. Comparing bare titles keeps the
# same song from being rejected over which side the credit landed on.
_FEAT = re.compile(
    r"\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring|w/|with)\s+[^)\]]*[\)\]]?\s*$",
    re.IGNORECASE)


def bare_title(title: str) -> str:
    """A title without its featured-artist credit."""
    return _FEAT.sub("", title or "").strip() or (title or "").strip()


def rows_missing_album(conn, limit: Optional[int] = None) -> list:
    """Tracks with no album, named well enough to search for."""
    sql = """
        SELECT track_id, artist, title, duration
        FROM tracks
        WHERE COALESCE(album, '') = ''
          AND length(TRIM(COALESCE(artist, ''))) > 0
          AND length(TRIM(COALESCE(title, ''))) > 0
        ORDER BY track_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def _clean(value: str) -> str:
    value = (value or "").strip()
    return "" if value in ("NA", "None") else value


def _parse(line: str) -> Optional[dict]:
    """One yt-dlp output line into a metadata dict."""
    if not line.strip():
        return None
    album, _, rest = line.partition("|")
    found_artist, _, rest = rest.partition("|")
    track, _, raw_duration = rest.partition("|")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    return {"album": _clean(album), "artist": _clean(found_artist),
            "track": _clean(track), "duration": duration or None}


# yt-dlp's wording when an upload needs a signed-in session. Those cannot be
# fetched headlessly at all, so they are collected rather than retried.
_AGE_GATED = re.compile(r"confirm your age|age.?restricted|Sign in to confirm",
                        re.IGNORECASE)


def lookup(artist: str, title: str, *,
           candidates: int = CANDIDATES) -> tuple[list[dict], str]:
    """Ask YouTube Music for a song.

    Returns the top results' metadata and any blocking reason -- currently only
    an age gate, which no amount of retrying will get past without cookies.
    """
    url = SEARCH_URL.format(query=quote_plus(f"{artist} {title}"))
    try:
        # No check=True: yt-dlp exits non-zero if *any* item fails, and an
        # age-restricted result among the hits would otherwise discard every
        # good line that came back with it.
        out = subprocess.run(
            ["yt-dlp", "--skip-download", "--ignore-errors",
             "--playlist-items", f"1:{max(1, candidates)}",
             "--print", _FIELDS, url],
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ([], "")
    found = [_parse(line) for line in (out.stdout or "").splitlines()]
    blocked = ""
    if _AGE_GATED.search(out.stderr or ""):
        blocked = "age-restricted; needs a signed-in session"
    return ([f for f in found if f], blocked)


def _is_same_song(found: dict, row) -> bool:
    """Whether the search result really is the track we asked about.

    The title must match; the artist only has to be *compatible*. Credited
    artists differ constantly between sources -- "Feint ft Veela" here,
    "Feint, Veela" there -- and rejecting those loses real albums, while
    accepting a different title would attribute the wrong record entirely.
    """
    if not detect.same_track("", bare_title(found["track"]), "",
                             bare_title(row["title"])):
        return False
    return db_cleanup.are_artists_compatible(found["artist"], row["artist"])


def artist_used_elsewhere(conn, artist: str, track_id: int) -> bool:
    """Whether this artist names other tracks in the library.

    The corroboration a swap needs. There is a real band called Mighty Ships
    with a song called "Tom Waits", so YouTube Music answering with the fields
    exchanged is not proof on its own -- but an artist that names nine other
    tracks here is an artist, and that row is not swapped.
    """
    row = conn.execute(
        "SELECT count(*) n FROM tracks WHERE artist = %s AND track_id != %s",
        (artist, track_id)).fetchone()
    return bool(row and int(row["n"]) > 0)


def looks_swapped(found: dict, row) -> bool:
    """Whether this row has its artist and title the wrong way round.

    Not guessed from the strings -- "Faint" and "Linkin Park" carry no clue
    about which is which. The evidence is that YouTube Music, asked about this
    row, returns the two fields *exchanged*: our title matches its artist and
    our artist matches its track.
    """
    if not (found.get("artist") and found.get("track")):
        return False
    return (detect.same_track("", bare_title(found["track"]), "",
                              bare_title(row["artist"]))
            and db_cleanup.are_artists_compatible(found["artist"], row["title"]))


def store(conn, track_id: int, album: str, duration: Optional[float]) -> None:
    """Fill absence only; never overwrite what is already known."""
    conn.execute(
        """
        UPDATE tracks
        SET album = COALESCE(NULLIF(album, ''), NULLIF(?, '')),
            duration = COALESCE(duration, ?)
        WHERE track_id = ?
        """,
        (album, duration, track_id),
    )


def harvest(conn, rows, *, apply: bool,
            pause: float = PAUSE_S) -> tuple[int, int, int]:
    """Look each track up. Returns (stored, unavailable, wrong song, swapped)."""
    stored = absent = mismatched = swapped = restricted = 0
    for index, row in enumerate(rows, 1):
        label = f"{row['artist']} - {row['title']}"[:46]
        results, blocked = lookup(row["artist"], row["title"])
        if blocked:
            # Collected rather than retried: a headless fetch can never get
            # past this, but the signed-in player window can.
            print(f"[{index}/{len(rows)}] {label:<48} ! {blocked}", flush=True)
            if apply:
                localcache.record_restricted(
                    row["track_id"], f"{row['artist']} {row['title']}",
                    blocked, conn)
            restricted += 1
            if pause:
                time.sleep(pause)
            continue
        # First result that verifies as ours; the right song is often second or
        # third when the first is a different track by the same artist.
        found = next((f for f in results if _is_same_song(f, row) and f["album"]),
                     None)
        if found is None:
            # Nothing verified: keep the first result, so the swap check and the
            # "got X instead" reporting still have something to work with.
            found = results[0] if results else None
        if not found:
            print(f"[{index}/{len(rows)}] {label:<48} - no result", flush=True)
            absent += 1
        elif (looks_swapped(found, row)
              and not artist_used_elsewhere(conn, row["artist"], row["track_id"])):
            # The row itself is wrong, not the search: repair it, and take the
            # album while we are here.
            print(f"[{index}/{len(rows)}] {label:<48} SWAPPED -> "
                  f"{found['artist']} - {found['track']}"[:110], flush=True)
            if apply:
                conn.execute(
                    "UPDATE tracks SET artist = ?, title = ? WHERE track_id = ?",
                    (found["artist"], found["track"], row["track_id"]))
                store(conn, row["track_id"], found["album"], found["duration"])
            swapped += 1
        elif not _is_same_song(found, row):
            # The search drifted to another song; its album is not this one's.
            print(f"[{index}/{len(rows)}] {label:<48} ~ got "
                  f"{found['artist']} - {found['track']}"[:110], flush=True)
            mismatched += 1
        elif not found["album"]:
            print(f"[{index}/{len(rows)}] {label:<48} - no album", flush=True)
            absent += 1
        else:
            print(f"[{index}/{len(rows)}] {label:<48} {found['album'][:30]}",
                  flush=True)
            if apply:
                store(conn, row["track_id"], found["album"], found["duration"])
            stored += 1
        # Release the write lock regularly rather than holding it for the whole
        # run; also means an interruption loses at most COMMIT_EVERY rows.
        if apply and index % COMMIT_EVERY == 0:
            conn.commit()
        if pause:
            time.sleep(pause)
    return stored, absent, mismatched, swapped, restricted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write what is learned (default is a dry run)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--pause", type=float, default=PAUSE_S)
    args = ap.parse_args()

    with localcache.connect() as conn:
        rows = rows_missing_album(conn, args.limit)
        if not rows:
            print("no tracks are missing an album")
            return 0
        print(f"looking up {len(rows)} track(s) on YouTube Music\n")
        try:
            stored, absent, mismatched, swapped, restricted = harvest(
                conn, rows, apply=args.apply, pause=args.pause)
        except KeyboardInterrupt:
            if args.apply:
                conn.commit()
            print("\nstopped; what was learned up to here is saved")
            return 130
        if args.apply:
            conn.commit()
        print(f"\n{stored} album(s) learned, {swapped} row(s) un-swapped, "
              f"{absent} unavailable, {mismatched} skipped as a different song, "
              f"{restricted} age-restricted"
              f"{'' if args.apply else '  (dry run — nothing written)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
