"""YouTube mode: resolve a SongRef from a YouTube URL.

YouTube video titles are much noisier than streaming metadata (which
``lyrics.clean_title`` already handles): they carry promo decorations like
``(Official Music Video)``, ``[Lyrics]``, ``| Records``, VEVO/uploader noise and
inconsistent ``Artist - Title`` ordering. This module turns that mess into a best
guess of ``(artist, title)`` so the existing LRCLIB -> cache -> synced-render
pipeline can take over unchanged.

Two layers:

- ``parse_youtube_title`` — a *pure* (no network) heuristic that strips YouTube
  promo decorations and splits artist/title. Unit-tested against real-world junk.
- ``fetch_metadata`` / ``resolve_youtube`` — thin yt-dlp wrappers that pull the
  video title/uploader/duration (and optionally download the audio so the Whisper
  fallback and librosa beat detection can run, which Spotify mode can never do).

yt-dlp is an *optional* dependency (``pip install 'karaoke[youtube]'``); the
import is lazy so the rest of the app never pays for it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import settings
from .identify import SongRef

# Bracketed/parenthesised groups are dropped when they contain any of these
# promo keywords. We deliberately keep groups like "(feat. X)" or "(Remastered
# 2011)" — LRCLIB's fetch already retries with lyrics.clean_title(), which knows
# those — so removing them here would only lose signal.
_JUNK_WORDS = (
    r"official|officiel|video|videoclip|vídeo|audio|lyrics?|"
    r"visuali[sz]er|lyric\s*video|music\s*video|m/?v|hd|hq|4k|8k|"
    r"full\s*album|full\s*video|explicit|clean|clip\s*officiel|"
    r"color\s*coded|colou?r\s*coded|sub\s*espa|legendado|tradu|"
    r"live\s*performance|performance\s*video|dance\s*(?:practice|version)|"
    r"remix\s*video|cover|karaoke\s*version"
)
_DECORATION = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*(?:%s)[^\)\]\}]*[\)\]\}]" % _JUNK_WORDS,
    re.IGNORECASE,
)

# Standalone (unbracketed) trailing promo phrases, e.g.
# "Artist - Title Official Music Video". We require a qualifier (official / music
# / lyric) or an explicit "... Lyrics" so we never strip a lone real word like
# the trailing "Video" in "Video Games" or "Audio".
_BARE_EDGE = re.compile(
    r"\s+(?:"
    r"official(?:\s+(?:music|lyric))?\s+(?:video|audio)"
    r"|(?:music|lyric)\s+video"
    r"|official\s+audio"
    r"|lyrics?"
    r")\s*$",
    re.IGNORECASE,
)

# Everything after a trailing " | ..." (label / channel tag) is noise.
_TRAILING_PIPE = re.compile(r"\s*\|.*$")

# Uploader suffixes that are not part of the artist name.
_UPLOADER_TAIL = re.compile(
    r"\s*(?:-\s*topic|vevo|official|officiel|music|records?|tv|channel|"
    r"entertainment)\s*$",
    re.IGNORECASE,
)

_DASHES = (" - ", " – ", " — ", " ~ ")


def _strip_decorations(title: str) -> str:
    """Remove YouTube promo decorations from a raw video title."""
    out = title.strip()
    # Drop everything after a trailing pipe (label/tag), then bracketed junk.
    out = _TRAILING_PIPE.sub("", out).strip()
    prev = None
    while out and out != prev:  # collapse stacked decorations
        prev = out
        out = _DECORATION.sub("", out).strip()
        out = _BARE_EDGE.sub("", out).strip()
    # Tidy leftover separators / quotes / whitespace.
    out = out.strip(" -–—|·•\t")
    out = re.sub(r"\s{2,}", " ", out)
    return out or title.strip()


def clean_uploader(uploader: str) -> str:
    """Normalise a channel/uploader name into a plausible artist name."""
    out = (uploader or "").strip()
    prev = None
    while out and out != prev:
        prev = out
        out = _UPLOADER_TAIL.sub("", out).strip()
    return out or (uploader or "").strip()


def _split_artist_title(text: str) -> tuple[str, str]:
    """Split "Artist - Title" on the first dash-like separator.

    Returns ("", text) when there is no usable split (both sides must be
    non-empty after stripping quotes/whitespace).
    """
    for sep in _DASHES:
        if sep in text:
            left, right = text.split(sep, 1)
            left = left.strip().strip('"\u201c\u201d\u2018\u2019')
            right = right.strip().strip('"\u201c\u201d\u2018\u2019')
            if left and right:
                return left, right
    return "", text.strip()


def parse_youtube_title(
    raw_title: str, uploader: Optional[str] = None
) -> tuple[str, str]:
    """Best-guess ``(artist, title)`` from a YouTube video title + uploader.

    Heuristics, in order:

    1. Strip promo decorations (``(Official Video)``, ``[Lyrics]``, ``| Label``…).
    2. Auto-generated "Art - Topic" channels: the uploader IS the artist. Prefer
       an explicit "Artist - Title" split in the (usually clean) title, else use
       the Topic artist with the whole title.
    3. A normal "Artist - Title" split in the cleaned title.
    4. Fall back to the cleaned uploader as artist + cleaned title.
    """
    cleaned = _strip_decorations(raw_title)
    up = (uploader or "").strip()

    if up.lower().endswith("- topic") or up.lower().endswith("topic"):
        topic_artist = clean_uploader(up)
        a, t = _split_artist_title(cleaned)
        if a and t:
            return a, t
        return topic_artist, cleaned

    a, t = _split_artist_title(cleaned)
    if a and t:
        return a, t

    if up:
        return clean_uploader(up), cleaned
    return "", cleaned


def _ejs_opts() -> dict:
    """yt-dlp options to enable EJS remote-component challenge solving.

    Modern YouTube requires solving JS signature / ``n`` challenges to expose
    real audio/video formats; without a challenge solver yt-dlp only sees image
    storyboards and raises "Requested format is not available". yt-dlp can fetch
    the solver scripts on demand via its EJS remote components. We enable the
    GitHub source by default (a supported JS runtime like ``deno`` or ``node``
    must be installed on the host). Override or disable via the
    ``KARAOKE_YTDLP_REMOTE_COMPONENTS`` env var (set it empty to disable).
    """
    import os
    spec = os.environ.get("KARAOKE_YTDLP_REMOTE_COMPONENTS", "ejs:github")
    spec = spec.strip()
    if not spec:
        return {}
    return {"remote_components": [s for s in spec.split(",") if s.strip()]}


def _cookie_opts(
    cookies_from_browser: Optional[str], cookies_file: Optional[str]
) -> dict:
    """Build yt-dlp cookie options for authenticated (e.g. Premium) access.

    ``cookies_from_browser`` is a yt-dlp browser spec — ``"firefox"``,
    ``"chrome"``, or the full ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` form
    (e.g. ``"firefox::Meta"`` for a container). It is passed as the
    ``cookiesfrombrowser`` tuple yt-dlp expects. ``cookies_file`` points at an
    exported Netscape ``cookies.txt``.

    Using your logged-in YouTube Music cookies unlocks higher-bitrate Premium
    audio, library-only/private tracks, and age-restricted videos. It does NOT
    read Premium's encrypted in-app offline downloads (those are DRM-locked and
    unreadable) — it authenticates the normal yt-dlp fetch as you.
    """
    opts: dict = {}
    import os
    from pathlib import Path
    cfb = cookies_from_browser or os.environ.get("KARAOKE_COOKIES_FROM_BROWSER")
    if cfb and cfb.strip().lower() == "chrome":
        p2 = Path.home() / ".config" / "google-chrome" / "Profile 2"
        if p2.is_dir():
            cfb = "chrome+GNOMEKEYRING:Profile 2"

    if cfb:
        # yt-dlp parses "BROWSER[+KEYRING][:PROFILE][::CONTAINER]"; the API wants
        # a tuple (browser, profile|None, keyring|None, container|None). We hand
        # it the raw spec split minimally and let yt-dlp normalize, matching how
        # the --cookies-from-browser CLI flag is parsed.
        spec = cfb.strip()
        browser, _, profile = spec.partition(":")
        keyring = None
        if "+" in browser:
            browser, _, keyring = browser.partition("+")
        container = None
        if "::" in profile:
            profile, _, container = profile.partition("::")
        opts["cookiesfrombrowser"] = (
            browser.lower() or None,
            profile or None,
            keyring or None,
            container or None,
        )
    if cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


_AUDIO_EXTS = {
    ".webm", ".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav", ".aac",
}


@dataclass(frozen=True)
class YouTubeCacheSummary:
    """Size/count report for the YouTube audio download cache."""

    directory: Path
    files: int
    bytes: int
    removed_files: int = 0
    removed_bytes: int = 0

    @property
    def mib(self) -> float:
        """Current cache size in MiB."""
        return self.bytes / (1024 * 1024)

    @property
    def removed_mib(self) -> float:
        """Pruned cache size in MiB."""
        return self.removed_bytes / (1024 * 1024)


def _youtube_audio_files(directory: Optional[Path] = None) -> list[Path]:
    """Return downloaded audio files in the YouTube cache (oldest first)."""
    root = Path(directory or settings.youtube_dir)
    if not root.is_dir():
        return []
    files = [
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    ]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


def youtube_cache_summary(directory: Optional[Path] = None) -> YouTubeCacheSummary:
    """Report the current YouTube download-cache size/count."""
    root = Path(directory or settings.youtube_dir)
    files = _youtube_audio_files(root)
    total = sum(p.stat().st_size for p in files)
    return YouTubeCacheSummary(root, len(files), total)


def prune_youtube_cache(
    max_mb: int,
    *,
    keep: Optional[Path] = None,
    directory: Optional[Path] = None,
) -> YouTubeCacheSummary:
    """Prune the YouTube audio cache to at most ``max_mb`` MiB (oldest first).

    ``keep`` protects the just-downloaded file so a single large current download
    is never immediately deleted. ``max_mb <= 0`` disables automatic pruning.
    """
    root = Path(directory or settings.youtube_dir)
    files = _youtube_audio_files(root)
    total = sum(p.stat().st_size for p in files)
    if max_mb <= 0:
        return YouTubeCacheSummary(root, len(files), total)

    limit = max_mb * 1024 * 1024
    protected = keep.resolve() if keep else None
    removed_files = 0
    removed_bytes = 0

    for p in files:
        if total <= limit:
            break
        if protected and p.resolve() == protected:
            continue
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        removed_files += 1
        removed_bytes += size

    remaining = len(_youtube_audio_files(root))
    return YouTubeCacheSummary(root, remaining, total, removed_files, removed_bytes)


def clear_youtube_cache(directory: Optional[Path] = None) -> YouTubeCacheSummary:
    """Delete all downloaded YouTube audio files."""
    root = Path(directory or settings.youtube_dir)
    removed_files = 0
    removed_bytes = 0
    for p in _youtube_audio_files(root):
        size = p.stat().st_size
        try:
            p.unlink()
        except OSError:
            continue
        removed_files += 1
        removed_bytes += size
    return YouTubeCacheSummary(root, 0, 0, removed_files, removed_bytes)


def search(query: str, limit: int = 1) -> list[dict]:
    """Search YouTube and return lightweight result dicts for backfill.

    Each result carries ``url``, ``title``, ``duration`` (seconds, or None) and
    ``uploader``. Duration and uploader are what let a caller tell an official
    upload from a cover, a live cut or a whole-album rip; flat extraction
    already provides them, so they are kept rather than discarded.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("YouTube search needs yt-dlp installed.") from exc

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = (info or {}).get("entries") or []
    results: list[dict] = []
    for entry in entries:
        video_id = entry.get("id") or entry.get("url")
        if not video_id:
            continue
        url = entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"
        try:
            duration = float(entry["duration"]) if entry.get("duration") else None
        except (TypeError, ValueError):
            duration = None
        results.append({
            "url": url,
            "title": entry.get("title") or "",
            "duration": duration,
            "uploader": entry.get("uploader") or entry.get("channel") or "",
        })
    return results


def download(url: str, **kwargs) -> str:
    """Download YouTube audio and return the cached local path."""
    meta = fetch_metadata(url, download=True, **kwargs)
    path = meta.get("path")
    if not path:
        raise RuntimeError("yt-dlp did not produce an audio file")
    return str(path)


def fetch_metadata(
    url: str,
    *,
    download: bool = False,
    cookies_from_browser: Optional[str] = None,
    cookies_file: Optional[str] = None,
    cache_max_mb: Optional[int] = None,
) -> dict:
    """Fetch YouTube video metadata via yt-dlp (optionally downloading audio).

    Returns a dict with ``title``, ``uploader``, ``duration`` (seconds, float or
    None) and ``path`` (local audio file when ``download=True``, else None).

    ``cookies_from_browser`` / ``cookies_file`` authenticate the request with
    your logged-in YouTube (Music) session — see ``_cookie_opts`` — which unlocks
    higher-quality Premium audio and library/private/age-restricted tracks.

    Raises ``RuntimeError`` with an actionable message when yt-dlp is not
    installed, so the CLI can tell the user how to enable YouTube mode.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "YouTube mode needs yt-dlp. Install it with "
            "`pip install 'karaoke[youtube]'` (or `pip install yt-dlp`)."
        ) from exc

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": not download,
    }
    opts.update(_cookie_opts(cookies_from_browser, cookies_file))
    opts.update(_ejs_opts())
    path: Optional[str] = None
    if download:
        out_dir = settings.youtube_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            }
        )

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=download)
        if download:
            path = ydl.prepare_filename(info)
            if path and Path(path).is_file():
                prune_youtube_cache(
                    settings.yt_cache_max_mb if cache_max_mb is None else cache_max_mb,
                    keep=Path(path),
                )

    dur = info.get("duration")
    return {
        "title": info.get("track") or info.get("title") or "",
        "uploader": info.get("artist") or info.get("uploader") or "",
        "duration": float(dur) if dur is not None else None,
        "path": path if (path and Path(path).is_file()) else None,
    }


def resolve_youtube(
    url: str,
    *,
    download: bool = False,
    cookies_from_browser: Optional[str] = None,
    cookies_file: Optional[str] = None,
    cache_max_mb: Optional[int] = None,
) -> Optional[SongRef]:
    """Resolve a YouTube URL into a SongRef (source="youtube").

    Uses yt-dlp track/artist tags when present (music.youtube.com often supplies
    them), otherwise the smart title parser. Returns None only when yt-dlp yields
    no usable title at all. ``cookies_from_browser`` / ``cookies_file`` forward
    your logged-in session for Premium-quality / library access.
    """
    meta = fetch_metadata(
        url, download=download,
        cookies_from_browser=cookies_from_browser, cookies_file=cookies_file,
        cache_max_mb=cache_max_mb,
    )
    raw_title = meta["title"]
    if not raw_title:
        return None

    # music.youtube.com may already give clean track/artist tags.
    if meta["uploader"] and not any(s in raw_title for s in _DASHES) and (
        meta["uploader"].lower().endswith(("- topic", "topic"))
    ):
        artist = clean_uploader(meta["uploader"])
        title = _strip_decorations(raw_title)
    else:
        artist, title = parse_youtube_title(raw_title, meta["uploader"])

    if not title:
        return None
    return SongRef(
        artist=artist,
        title=title,
        duration=meta["duration"],
        path=meta["path"],
        source="youtube",
        url=url,
    )
