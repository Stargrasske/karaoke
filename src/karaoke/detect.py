"""Active desktop-player detection and mode selection for the karaoke TUI.

The TUI's default is to watch whatever is playing on the desktop (via MPRIS /
playerctl) and pick a mode automatically:

- ``spotify``  — a Spotify player is active; sync/control that.
- ``scan``     — a desktop/browser player is playing (e.g. a YouTube / YT Music
                 tab in Firefox/Chrome, or VLC); sync lyrics to its position.
- ``browse``   — nothing is playing; the user drives the library list and
                 opening a song launches the browser (the old "youtube mode").

Browser MPRIS metadata is unreliable, so when a source URL is present we prefer
looking the track up by URL in the local ``sources`` table (see the
karaoke-app-development skill) before trusting artist/title.
"""
from __future__ import annotations

import shutil
import psycopg
from psycopg import Connection, Cursor
import subprocess
from dataclasses import dataclass
from typing import Optional

from .logger import log
from .lyrics import Lyrics
from .playerctl import PlayerMetadata, current_metadata, normalize_player_track

YOUTUBE_HOSTS = ("music.youtube.com", "youtube.com/", "youtu.be/")


@dataclass(frozen=True)
class Detection:
    """The active player and the karaoke mode chosen for it."""

    mode: str  # spotify | scan | browse
    player: str = ""
    artist: str = ""
    title: str = ""
    url: str = ""
    mpris_name: str = ""
    # Players publish xesam:album and it was parsed all the way to SongRef,
    # then dropped here -- one field short of being stored. Carrying it is
    # what lets the library know which record a track came from.
    album: str = ""
    # Seconds. Duration is what lets the deduplicator tell a demo or a live
    # take from the studio cut; without it that guard abstains on every pair.
    duration: Optional[float] = None
    playlist_id: str = ""

    @property
    def is_active(self) -> bool:
        """Whether a player is actually playing something to sync/control.

        "radio" is mic identification (songrec): there is no MPRIS player to
        control, but there is a song to follow and sync lyrics to.
        """
        return self.mode in ("spotify", "scan", "radio")


def is_youtube_url(url: str) -> bool:
    """True if the URL points at YouTube or YouTube Music."""
    u = (url or "").lower()
    return any(host in u for host in YOUTUBE_HOSTS)


def classify(meta: Optional[PlayerMetadata]) -> Detection:
    """Map raw MPRIS metadata to a karaoke Detection/mode.

    - Spotify players -> ``spotify`` mode.
    - Any other player with a real track -> ``scan`` mode (browser YouTube tab,
      YT Music, VLC, local players).
    - Nothing playing -> ``browse`` mode (library-driven, opens the browser).
    """
    from . import localcache

    if meta is None:
        return Detection(mode="browse")
    player = (meta.mpris_name or meta.player or "").lower()
    mpris = meta.mpris_name or meta.player
    ref = normalize_player_track(meta.artist, meta.title, meta.album, meta.url)
    pl_id = localcache.extract_playlist_id(meta.url) or ""
    if player.startswith("spotify"):
        return Detection("spotify", meta.player, ref.artist, ref.title, meta.url,
                         mpris_name=mpris, album=ref.album,
                         duration=meta.duration, playlist_id=pl_id)
    # A desktop or browser player is active: sync to its position. Trust a
    # YouTube URL over the (often stale) browser artist/title for display.
    if ref.title or ref.artist or meta.url:
        return Detection("scan", meta.player, ref.artist, ref.title, meta.url,
                         mpris_name=mpris, album=ref.album,
                         duration=meta.duration, playlist_id=pl_id)
    return Detection(mode="browse")


def same_track(artist_a: str, title_a: str,
               artist_b: str, title_b: str) -> bool:
    """Whether two (artist, title) pairs name the same recording.

    Used to decide whether an MPRIS player is playing the song the microphone
    just identified — if it is, its exact position beats dead reckoning.

    The two sources disagree constantly: songrec returns "The Mothers of
    Invention" where a player reports "Mothers of Invention", and players append
    edition suffixes ("(Live)", "- Remastered 2011") that songrec omits. The
    normalisation for exactly this already exists in localcache, so it is reused
    rather than reimplemented.

    A blank artist on either side is tolerated — browser MPRIS often has none —
    but a title match is required in every case, or an empty artist would make
    unrelated songs compare equal.
    """
    from .localcache import _artist_key, _title_keys

    if not (title_a or "").strip() or not (title_b or "").strip():
        return False
    if not _title_keys(title_a) & _title_keys(title_b):
        return False
    key_a, key_b = _artist_key(artist_a), _artist_key(artist_b)
    if not key_a or not key_b:
        return True
    return key_a == key_b


def preferred_player(candidates: list[str], mic_artist: str = "",
                     mic_title: str = "") -> str:
    """Choose between several simultaneously-playing MPRIS players.

    Order matters: without a stable rule the choice follows playerctl's listing
    order and the mode flaps between players mid-song.

    1. The player whose track the microphone just identified — it is provably
       the one making the sound in the room.
    2. Spotify, which reports an exact position where a browser tab does not.
    3. Whatever came first, so there is always an answer.
    """
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0]

    if mic_title:
        for name in candidates:
            meta = current_metadata(name)
            if meta and same_track(meta.artist, meta.title, mic_artist, mic_title):
                return name

    for name in candidates:
        # Chrome names its player per instance ("chromium.instance402904"), so
        # only a prefix comparison is stable across restarts.
        if name.lower().startswith("spotify"):
            return name
    return candidates[0]


def detect_active(mic_artist: str = "", mic_title: str = "") -> Detection:
    """Detect the currently-playing desktop player and its karaoke mode.

    The mic hints are used only to break a tie between players that are *all*
    playing; they never invent a detection on their own.
    """
    from . import playerctl, player_open, localcache

    chosen = preferred_player(playerctl.playing_players(), mic_artist, mic_title)
    det = classify(current_metadata(chosen))

    # If URL lacks playlist_id and playback is in browser kiosk, enrich with CDP location.href
    if det.is_active and not det.playlist_id:
        try:
            b_state = player_open.browser_playback(timeout=0.08)
            if b_state and b_state.get("url"):
                pl_id = localcache.extract_playlist_id(b_state["url"])
                if pl_id:
                    det = Detection(
                        mode=det.mode,
                        player=det.player,
                        artist=det.artist,
                        title=det.title,
                        url=det.url or b_state["url"],
                        mpris_name=det.mpris_name,
                        album=det.album,
                        duration=det.duration,
                        playlist_id=pl_id,
                    )
        except Exception:
            pass
    return det


def spotify_running() -> bool:
    """True if a Spotify MPRIS player is currently visible to playerctl."""
    try:
        out = subprocess.run(
            ["playerctl", "--list-all"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return any(line.strip().lower().startswith("spotify") for line in out.splitlines())


def launch_spotify() -> tuple[bool, str]:
    """Deprecated: Spotify playback is unified into the browser window."""
    return False, "disabled (Spotify plays in unified browser window)"


def resolve_lyrics(
    det: Detection, conn: Connection
) -> tuple[str, str, Optional[Lyrics]]:
    """Resolve (artist, title, lyrics) for a detection from the local cache.

    Prefers a URL match against the ``sources`` table (canonical for browser
    tabs whose MPRIS artist/title is unreliable), then falls back to the
    normalized artist/title. Returns ``lyrics=None`` on a miss.
    """
    from . import localcache

    if det.url:
        found = localcache.find_track_by_url(det.url, conn)
        if found:
            track_id, artist, title = found
            return artist, title, localcache.get_lyrics_by_track_id(track_id, conn)
    if det.artist or det.title:
        track_id = localcache.find_track_id(det.artist, det.title, conn)
        if track_id is not None:
            return (
                det.artist,
                det.title,
                localcache.get_lyrics_by_track_id(track_id, conn),
            )
        # Browser/player titles often carry decorations the cached track lacks
        # (e.g. "(2019 Remastered)", "(Official Video)"). Retry with a cleaned
        # title before giving up so remaster/live tabs still resolve.
        from .lyrics import clean_title

        cleaned = clean_title(det.title)
        if cleaned and cleaned != det.title:
            track_id = localcache.find_track_id(det.artist, cleaned, conn)
            if track_id is not None:
                row = conn.execute(
                    "SELECT artist, title FROM tracks WHERE track_id = %s",
                    (track_id,),
                ).fetchone()
                return (
                    row["artist"] if row else det.artist,
                    row["title"] if row else cleaned,
                    localcache.get_lyrics_by_track_id(track_id, conn),
                )
        # Relaxed match before giving up. Radio mode (songrec) caches tracks
        # under the full credit and a decorated title — "James Brown & The
        # Famous Flames", "[2020 Remaster]" — so a song it already fetched is
        # invisible to an exact lookup on the browser's plainer spelling.
        relaxed = localcache.find_track_id_relaxed(det.artist, det.title, conn)
        if relaxed is not None:
            row = conn.execute(
                "SELECT artist, title FROM tracks WHERE track_id = %s",
                (relaxed,),
            ).fetchone()
            return (
                row["artist"] if row else det.artist,
                row["title"] if row else det.title,
                localcache.get_lyrics_by_track_id(relaxed, conn),
            )
        # Fallback for empty artist (e.g. browser tab playing YouTube Music without artist tag)
        if not det.artist and det.title:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.track_id, t.artist, t.title FROM tracks t
                WHERE lower(t.title) = lower(%s)
                ORDER BY EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '') DESC, t.track_id DESC
                LIMIT 1
                """,
                (det.title,),
            )
            row = cur.fetchone()
            if row:
                return (
                    row["artist"],
                    row["title"],
                    localcache.get_lyrics_by_track_id(row["track_id"], conn),
                )
    return det.artist, det.title, None


def record_gap(det: Detection, conn: Connection) -> None:
    """Persist a missing-lyrics detection so it can be backfilled/staged.

    Stores the source (track + URL) when we have a real title, and logs a
    lyric gap so ``karaoke-backfill`` / ``karaoke-stage`` can pick it up later.
    Best-effort: never raises to the caller.

    A gap is only logged when BOTH artist and title are known. YouTube Music
    reports a title with no ``xesam:artist``, and browsers expose no
    ``xesam:url`` to resolve one from, so an artist-less row could never be
    matched against LRCLIB — it would sit in the queue being retried forever.
    Dropping it beats recording a gap nothing can ever fill.
    """
    from . import localcache

    if not (det.artist or det.title):
        return
    try:
        if det.url:
            kind = "youtube" if is_youtube_url(det.url) else (det.player or "player")
            localcache.add_track_source(
                det.artist or "Unknown",
                det.title or det.url,
                album=det.album,
                duration=det.duration,
                url=det.url,
                kind=kind,
                player_name=det.player,
                conn=conn,
            )
        if not det.artist.strip():
            log.debug("gap not logged, player gave no artist for %r", det.title)
            return
        localcache.log_lyric_gap(det.artist, det.title, conn)
    except Exception:
        pass
