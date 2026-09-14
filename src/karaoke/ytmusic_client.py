"""YouTube Music client wrapper using ytmusicapi.

Provides background library & playlist management, song resolution to YouTube Music
video IDs, and synchronization of karaoke queues into real YouTube Music playlists.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .logger import log

_AUTH_ENV_VARS = ("KARAOKE_YTMUSIC_AUTH", "YTMUSIC_AUTH")
_DEFAULT_AUTH_PATHS = (
    Path("~/.config/karaoke/ytmusic_auth.json"),
    Path("~/.local/share/karaoke/ytmusic_auth.json"),
    Path("~/.config/karaoke/browser.json"),
    Path("~/.config/karaoke/oauth.json"),
)


class YTMusicError(Exception):
    """Base error for YouTube Music operations."""


class YTMusicAuthError(YTMusicError):
    """Raised when an operation requires authentication but no valid auth is available."""


def resolve_auth_file(custom_path: str | Path | None = None) -> Optional[Path]:
    """Find the configured YouTube Music auth file, or None if unconfigured."""
    if custom_path:
        p = Path(custom_path).expanduser()
        if p.is_file():
            return p
        return None

    for env_var in _AUTH_ENV_VARS:
        val = os.environ.get(env_var)
        if val:
            p = Path(val).expanduser()
            if p.is_file():
                return p

    for default_path in _DEFAULT_AUTH_PATHS:
        p = default_path.expanduser()
        if p.is_file():
            return p

    return None


def extract_auth_from_chrome_cdp(dest_path: Path | None = None) -> Optional[Path]:
    """Automatically extract authentication headers from the running Chrome kiosk browser over CDP."""
    try:
        from . import player_open
        from ytmusicapi.auth.browser import get_authorization, initialize_headers

        cookie_resp = player_open._cdp_send("Network.getCookies", {"urls": ["https://music.youtube.com", "https://www.youtube.com"]})
        cookies = cookie_resp.get("result", {}).get("cookies", []) if cookie_resp else []
        if not cookies:
            return None

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        sapisid = None
        for c in cookies:
            if c.get("name") in ("__Secure-3PAPISID", "SAPISID", "APISID"):
                sapisid = c.get("value")
                break

        origin = "https://music.youtube.com"
        auth_header = get_authorization(f"{sapisid} {origin}") if sapisid else ""

        dest = (dest_path or Path("~/.config/karaoke/ytmusic_auth.json")).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)

        user_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "x-origin": origin,
            "x-goog-authuser": "0",
            "authorization": auth_header,
            "cookie": cookie_str,
        }
        init_headers = initialize_headers()
        user_headers.update(init_headers)

        with open(dest, "w") as f:
            json.dump(user_headers, f, indent=4)

        log.info("Successfully extracted and saved YouTube Music auth headers from Chrome CDP to %s", dest)
        return dest
    except Exception as exc:
        log.debug("Failed auto-extracting YouTube Music auth from Chrome CDP: %s", exc)
        return None


def clean_search_term(text: str) -> str:
    """Normalize artist or title strings for search."""
    t = re.sub(r"\s*[\(\[](?:official|audio|video|lyric|remastered|karaoke|version).*?[\)\]]", "", text, flags=re.IGNORECASE)
    t = re.sub(r"\s*(?:feat\.?|ft\.?)\s+.*", "", t, flags=re.IGNORECASE)
    return t.strip() or text.strip()


class YTMusicClient:
    """Client for interacting with YouTube Music via ytmusicapi."""

    is_authenticated: bool = False

    def __init__(self, auth: str | Path | dict[str, Any] | None = None) -> None:
        from ytmusicapi import YTMusic

        self._auth_source: str | None = None
        self.is_authenticated: bool = False

        if isinstance(auth, dict):
            self.api = YTMusic(auth=json.dumps(auth))
            self.is_authenticated = True
            self._auth_source = "dict"
        else:
            auth_file = resolve_auth_file(auth)
            if auth_file is None and auth is None:
                auth_file = extract_auth_from_chrome_cdp()

            if auth_file is not None:
                try:
                    self.api = YTMusic(auth=str(auth_file))
                    self.is_authenticated = True
                    self._auth_source = str(auth_file)
                    log.debug("Loaded YouTube Music client with auth from %s", auth_file)
                except Exception as exc:
                    log.warning("Failed to initialize YTMusic with %s: %s; falling back to unauthenticated", auth_file, exc)
                    self.api = YTMusic()
                    self.is_authenticated = False
            else:
                self.api = YTMusic()
                self.is_authenticated = False

    def require_auth(self) -> None:
        """Raise YTMusicAuthError if the client is not authenticated."""
        if not self.is_authenticated:
            raise YTMusicAuthError(
                "Operation requires YouTube Music authentication. "
                "Configure ~/.config/karaoke/ytmusic_auth.json or run karaoke-ytmusic-playlist --setup-auth"
            )

    def search_track(self, artist: str, title: str, *, filter_type: Any = "songs") -> Optional[str]:
        """Search for a song on YouTube Music and return its videoId."""
        c_artist = clean_search_term(artist)
        c_title = clean_search_term(title)
        query = f"{c_artist} {c_title}".strip()
        if not query:
            return None

        try:
            results = self.api.search(query, filter=filter_type, limit=5)
            if not results and filter_type == "songs":
                # Fallback to video search if song catalog doesn't return
                results = self.api.search(query, filter="videos", limit=5)

            for r in results:
                vid = r.get("videoId")
                if vid:
                    return vid
        except Exception as exc:
            log.debug("YTMusic search failed for '%s': %s", query, exc)

        return None

    def get_library_playlists(self, limit: int = 50) -> list[dict[str, Any]]:
        """List playlists in the user's library."""
        self.require_auth()
        try:
            return self.api.get_library_playlists(limit=limit)
        except Exception as exc:
            log.error("Failed to get library playlists from YouTube Music: %s", exc)
            raise YTMusicError(f"Failed to fetch library playlists: {exc}") from exc

    def get_playlist(self, playlist_id: str, limit: int = 100) -> dict[str, Any]:
        """Fetch details and tracks of a playlist."""
        try:
            return self.api.get_playlist(playlist_id, limit=limit)
        except Exception as exc:
            log.error("Failed to get playlist %s from YouTube Music: %s", playlist_id, exc)
            raise YTMusicError(f"Failed to fetch playlist {playlist_id}: {exc}") from exc

    def delete_playlist(self, playlist_id: str) -> dict[str, Any] | str:
        """Delete a YouTube Music playlist by ID."""
        self.require_auth()
        try:
            return self.api.delete_playlist(playlistId=playlist_id)
        except Exception as exc:
            log.error("Failed to delete playlist %s from YouTube Music: %s", playlist_id, exc)
            raise YTMusicError(f"Failed to delete playlist: {exc}") from exc

    def get_playlist_tracks(self, playlist_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Fetch simplified track list from a playlist."""
        pl = self.get_playlist(playlist_id, limit=limit)
        tracks_raw = pl.get("tracks", []) or []
        items: list[dict[str, Any]] = []

        for t in tracks_raw:
            vid = t.get("videoId")
            if not vid:
                continue
            title = t.get("title") or "Unknown Title"
            artists_list = t.get("artists") or []
            artist = ", ".join(a.get("name", "") for a in artists_list if isinstance(a, dict) and a.get("name")) or "Unknown Artist"
            album = t.get("album", {}).get("name") if isinstance(t.get("album"), dict) else None
            duration_s = t.get("duration_seconds")

            items.append({
                "videoId": vid,
                "title": title,
                "artist": artist,
                "album": album,
                "duration_seconds": duration_s,
                "url": f"https://music.youtube.com/watch?v={vid}",
            })
        return items

    def create_playlist(
        self,
        title: str,
        description: str = "",
        privacy_status: str = "PRIVATE",
        video_ids: list[str] | None = None,
    ) -> str:
        """Create a new playlist and return its playlist ID."""
        self.require_auth()
        try:
            res = self.api.create_playlist(
                title=title,
                description=description,
                privacy_status=privacy_status,
                video_ids=video_ids,
            )
            if isinstance(res, str):
                return res
            if isinstance(res, dict) and "id" in res:
                return str(res["id"])
            return str(res)
        except Exception as exc:
            log.error("Failed to create playlist '%s' on YouTube Music: %s", title, exc)
            raise YTMusicError(f"Failed to create playlist: {exc}") from exc

    def add_playlist_items(
        self,
        playlist_id: str,
        video_ids: list[str],
        duplicates: bool = False,
    ) -> dict[str, Any] | str:
        """Add one or more videoIds to an existing playlist."""
        self.require_auth()
        if not video_ids:
            return {"status": "ok", "added": 0}
        try:
            return self.api.add_playlist_items(
                playlistId=playlist_id,
                videoIds=video_ids,
                duplicates=duplicates,
            )
        except Exception as exc:
            log.error("Failed to add items to playlist %s: %s", playlist_id, exc)
            raise YTMusicError(f"Failed to add items to playlist: {exc}") from exc

    def remove_playlist_items(
        self,
        playlist_id: str,
        videos: list[dict[str, Any]],
    ) -> dict[str, Any] | str:
        """Remove tracks from a playlist."""
        self.require_auth()
        if not videos:
            return {"status": "ok", "removed": 0}
        try:
            return self.api.remove_playlist_items(playlistId=playlist_id, videos=videos)
        except Exception as exc:
            log.error("Failed to remove items from playlist %s: %s", playlist_id, exc)
            raise YTMusicError(f"Failed to remove items from playlist: {exc}") from exc

    def sync_playlist(
        self,
        playlist_name: str,
        video_ids: list[str],
        description: str = "Created by Karaoke Platform",
    ) -> str:
        """Find an existing playlist by name (or create it) and sync the video IDs."""
        self.require_auth()
        playlists = self.get_library_playlists(limit=100)
        target_id: str | None = None

        for pl in playlists:
            if pl.get("title", "").strip().lower() == playlist_name.strip().lower():
                target_id = pl.get("playlistId")
                break

        if not target_id:
            return self.create_playlist(
                title=playlist_name,
                description=description,
                privacy_status="PRIVATE",
                video_ids=video_ids,
            )

        existing_tracks = self.get_playlist_tracks(target_id, limit=500)
        existing_vids = {t["videoId"] for t in existing_tracks}
        new_vids = [vid for vid in video_ids if vid not in existing_vids]

        if new_vids:
            self.add_playlist_items(target_id, new_vids)

        return target_id
