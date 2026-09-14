"""MPRIS/playerctl helpers for desktop media metadata.

This module keeps playerctl subprocess details and metadata cleanup out of the
CLI. MPRIS is the common Linux desktop media-player API used by Cosmic/GNOME/KDE
players, browsers, VLC and Spotify.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from .identify import SongRef, parse_query


@dataclass(frozen=True)
class PlayerMetadata:
    """Metadata returned by playerctl/MPRIS for the active player."""

    artist: str = ""
    title: str = ""
    album: str = ""
    url: str = ""
    player: str = ""
    mpris_name: str = ""
    # Seconds. MPRIS reports mpris:length in *microseconds*; converted on the
    # way in so nothing downstream has to know that.
    duration: Optional[float] = None


def _clean_piece(text: str) -> str:
    """Trim whitespace and common quote wrappers from player metadata."""
    return (text or "").strip().strip('"\u201c\u201d\u2018\u2019')


# Site suffixes browsers append to the tab title, e.g. " - YouTube".
_SITE_SUFFIX = re.compile(
    r"\s*[-–—|]\s*(?:youtube(?:\s+music)?|vimeo|soundcloud|dailymotion|"
    r"bandcamp)\s*$",
    re.IGNORECASE,
)

# Video-descriptor parentheticals/brackets that aren't part of the song title,
# e.g. "(Official Video)", "[Official Music Video]", "(Lyric Video)", "(HD)".
_VIDEO_DESCRIPTOR = re.compile(
    r"""\s*[\(\[]\s*
        (?:
            official\s+(?:music\s+)?(?:video|audio|lyric[s]?\s*video|visualizer|
                                     hd\s+video)
            | official\s+(?:audio|video|visualizer|lyrics?)
            | (?:full\s+)?(?:music\s+)?video
            | lyrics?(?:\s+video)?
            | visuali[sz]er
            | audio(?:\s+only)?
            | m/?v
            | hd | 4k | hq
            | color\s+coded\s+lyrics?[^\)\]]*
        )
        \s*[\)\]]\s*""",
    re.IGNORECASE | re.VERBOSE,
)

# Trailing view-count / metadata fragments some browsers append after a bullet.
_TRAILING_BULLET = re.compile(r"\s*[•·]\s*.*$")


def clean_browser_title(title: str) -> str:
    """Strip browser/video cruft from an MPRIS tab title.

    Browsers report a tab title, not clean metadata: e.g.
    ``"Jain - Come (Official Video) - YouTube"``. This removes the site suffix
    (" - YouTube"), video-descriptor parentheticals ("(Official Video)",
    "[Lyric Video]", "(HD)"), and trailing bullet fragments, so downstream
    artist/title parsing and lyric lookup have a chance. Applied repeatedly so
    stacked descriptors collapse. Returns the input unchanged if nothing matches.
    """
    out = _clean_piece(title)
    if not out:
        return ""
    prev = None
    while out and out != prev:
        prev = out
        out = _SITE_SUFFIX.sub("", out).strip()
        out = _TRAILING_BULLET.sub("", out).strip()
        out = _VIDEO_DESCRIPTOR.sub(" ", out).strip()
        out = re.sub(r"\s{2,}", " ", out).strip()
    # Never return empty from over-eager stripping; fall back to the original.
    return out or _clean_piece(title)


def normalize_player_track(artist: str, title: str, album: str = "", url: str = "") -> SongRef:
    """Normalize noisy MPRIS metadata into a SongRef.

    Some players (notably VLC for files with imperfect tags) expose artist in
    both artist and title, e.g. artist="Tom Waits" and title='Tom Waits - "Watch
    Her Disappear"'. Strip that duplicated artist before doing lyric lookup.

    Browser tabs report a page title rather than clean metadata, so the title is
    first run through ``clean_browser_title`` to drop "- YouTube" and
    "(Official Video)"-style cruft.
    """
    a = _clean_piece(artist)
    t = clean_browser_title(title)
    if a and t.casefold().startswith((a + " - ").casefold()):
        t = _clean_piece(t[len(a) + 3:])
    elif a and t.casefold().startswith((a + " – ").casefold()):
        t = _clean_piece(t[len(a) + 3:])
    elif a and t.casefold().startswith((a + " — ").casefold()):
        t = _clean_piece(t[len(a) + 3:])
    elif not a and (" - " in t or " – " in t or " — " in t):
        parsed = parse_query(t.replace(" – ", " - ").replace(" — ", " - "))
        a, t = parsed.artist, parsed.title
    return SongRef(artist=a, title=t, album=_clean_piece(album), url=url, source="player")


def _length_seconds(raw: str) -> Optional[float]:
    """Convert an ``mpris:length`` value to seconds, or None.

    The spec puts it in microseconds. Players that do not know the length omit
    it or report zero -- both mean "unknown", and returning 0.0 would be read
    downstream as a real, absurdly short track.
    """
    try:
        micros = float(_clean_piece(raw))
    except (TypeError, ValueError):
        return None
    seconds = micros / 1_000_000.0
    return seconds if seconds > 0 else None


def _metadata_format() -> str:
    # Unit Separator between fields; unlikely in song metadata and easy to split.
    return ("{{artist}}\x1f{{title}}\x1f{{album}}\x1f{{xesam:url}}"
            "\x1f{{playerName}}\x1f{{mpris:length}}")


def list_players() -> list[str]:
    """MPRIS names of every player playerctl can see."""
    out = _run(["playerctl", "--list-all"])
    if not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def playing_players() -> list[str]:
    """MPRIS names of the players whose status is Playing.

    Bare ``playerctl metadata`` answers from whichever player playerctl happens
    to pick first, with no regard for whether it is playing anything. With the
    kiosk browser window left open for CDP — its normal resting state — that is
    routinely a *paused* tab holding a stale track, which then shadows the
    player actually making sound. Asking who is Playing is the fix.
    """
    names = list_players()
    if len(names) < 2:
        # Nothing to disambiguate; skip the per-player probes. This is the hot
        # path (detection runs on a 1.5s timer) and the common case.
        return names
    return [n for n in names if (status(n) or "").strip() == "Playing"]


def playing_player() -> str:
    """One player that is currently Playing, or '' if none is."""
    found = playing_players()
    return found[0] if found else ""


def current_metadata(player: str = "") -> Optional[PlayerMetadata]:
    """Return MPRIS metadata via playerctl, or None if unavailable.

    Targets ``player`` when given, otherwise lets playerctl choose. Note that
    playerctl's own choice ignores playback status, so callers that care which
    player is actually making sound should pass one — see
    :func:`playing_players` and ``detect.preferred_player``. Selection is kept
    out of here deliberately: this module is a thin wrapper over the binary and
    holds no policy about which player matters.
    """
    try:
        proc = subprocess.run(
            _base_cmd(player) + ["metadata", "--format", _metadata_format()],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    parts = proc.stdout.rstrip("\n").split("\x1f")
    if len(parts) < 2:
        return None
    parts += [""] * (6 - len(parts))
    raw_player = _clean_piece(parts[4])
    player_name = raw_player
    title_text = _clean_piece(parts[1])
    url_text = _clean_piece(parts[3])
    if "youtube music" in title_text.lower() or "music.youtube.com" in url_text.lower():
        player_name = "YouTube Music"
    elif "youtube" in title_text.lower() or "youtube.com" in url_text.lower() or "youtu.be" in url_text.lower():
        player_name = "YouTube"

    meta = PlayerMetadata(
        artist=_clean_piece(parts[0]),
        title=title_text,
        album=_clean_piece(parts[2]),
        url=url_text,
        player=player_name,
        mpris_name=raw_player,
        duration=_length_seconds(parts[5]),
    )
    if not meta.artist and not meta.title:
        return None
    return meta


def current_songref() -> Optional[SongRef]:
    """Resolve active desktop-player metadata to a SongRef."""
    meta = current_metadata()
    if meta is None:
        return None
    ref = normalize_player_track(meta.artist, meta.title, meta.album, meta.url)
    if not ref.title:
        return None
    return ref


def _run(cmd: list[str], *, timeout: float = 5.0) -> Optional[str]:
    """Run a playerctl command, returning stdout or None on failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return proc.stdout.strip()


def _base_cmd(player: str = "") -> list[str]:
    cmd = ["playerctl"]
    if player:
        cmd += ["--player", player]
    return cmd


def position(player: str = "", *, timeout: float = 0.5) -> Optional[float]:
    """Current playback position in seconds for the (targeted) player."""
    out = _run(_base_cmd(player) + ["position"], timeout=timeout)
    if out is None:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def status(player: str = "") -> Optional[str]:
    """Playback status string (Playing/Paused/Stopped) or None."""
    return _run(_base_cmd(player) + ["status"])


def art_url(player: str = "") -> Optional[str]:
    """The player's cover-art URL (``mpris:artUrl``), or None.

    Browsers write the artwork to a local file and advertise it here, so this
    is usually a ``file://`` path that costs nothing to read.
    """
    return _run(_base_cmd(player) + ["metadata", "mpris:artUrl"])


def pause(player: str = "") -> bool:
    """Pause the (targeted) player. Returns success.

    Distinct from :func:`play_pause`, which toggles: a caller that only wants
    silence must not start playback because the track happened to be paused
    already.
    """
    return _run(_base_cmd(player) + ["pause"]) is not None


def play_pause(player: str = "") -> bool:
    """Toggle play/pause on the (targeted) player. Returns success."""
    return _run(_base_cmd(player) + ["play-pause"]) is not None


def next_track(player: str = "") -> bool:
    """Skip to the next track. Returns success."""
    return _run(_base_cmd(player) + ["next"]) is not None


def previous_track(player: str = "") -> bool:
    """Skip to the previous track. Returns success."""
    return _run(_base_cmd(player) + ["previous"]) is not None


def seek(offset_s: float, player: str = "") -> bool:
    """Seek by a relative offset (seconds; may be negative). Returns success."""
    arg = f"{offset_s:+g}" if offset_s < 0 else f"{offset_s:g}+"
    return _run(_base_cmd(player) + ["position", arg]) is not None
