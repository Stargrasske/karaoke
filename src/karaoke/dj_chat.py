"""Interactive AI DJ Chat Client and TUI Modal Screen for Karaoke.

Connects to the local Karaoke MCP tools (library search, harmonic Camelot mixing,
playback controls, lyric sentiment analysis, crowd stats) and local LLMs (Ollama)
or provides instant deterministic DJ recommendations.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Static

from . import mcp_server

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_BASE_URL = f"http://{OLLAMA_HOST}"
else:
    OLLAMA_BASE_URL = OLLAMA_HOST

DEFAULT_MODEL = os.environ.get("KARAOKE_DJ_MODEL", "qwen3:1.7b")

DJ_SYSTEM_PROMPT = (
    "You are an energetic, charismatic Karaoke DJ and MC. You know every track in "
    "our 18,000+ song library. Use harmonic mixing (Camelot wheel) and BPM pacing to "
    "keep the room dancing. Suggest songs that match the singers' energy, warn them "
    "about tough high notes, and hype up the crowd! Keep your answers punchy, "
    "entertaining, and concise (2-4 sentences or a clean bullet list)."
)


class DJChatSession:
    """Manages chat conversation, tool augmentation, and LLM communication."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        current_track_id: Optional[int] = None,
        current_track_provider: Optional[Any] = None,
    ) -> None:
        self.model = model
        self.current_track_id = current_track_id
        self.current_track_provider = current_track_provider
        self.history: list[dict[str, str]] = [
            {"role": "system", "content": DJ_SYSTEM_PROMPT}
        ]
        self.last_suggestions: list[dict[str, Any]] = []
        self.last_candidates: list[dict[str, Any]] = []
        self.on_tracks_queued: Optional[Any] = None
        self.follow_dj_handler: Optional[Any] = None

    def ask(self, user_prompt: str) -> str:
        """Process user input, execute tools if needed, and query Ollama or return DJ response."""
        p_lower = user_prompt.strip().lower()

        # Direct number or 'all' selection from active candidates
        if self.last_candidates:
            if p_lower in [str(i) for i in range(1, len(self.last_candidates) + 1)]:
                return self.cmd_queue(p_lower)
            if p_lower == "all":
                return self.cmd_queue("all")

        # Direct Slash Commands
        if p_lower in ("/now", "/nowplaying", "now", "now playing"):
            return self.cmd_now_playing()

        if p_lower in ("/stats", "stats", "crowd stats"):
            return self.cmd_stats()

        if p_lower.startswith("/suggest") or p_lower in ("suggest", "next"):
            parts = user_prompt.strip().split(maxsplit=1)
            strategy = parts[1] if len(parts) > 1 else "harmonic"
            return self.cmd_suggest(strategy=strategy)

        if (
            p_lower.startswith("/queue")
            or p_lower.startswith("/add")
            or p_lower.startswith("/q")
        ):
            parts = user_prompt.strip().split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            return self.cmd_queue(arg)

        if p_lower in ("/dj-list", "/playlist", "/list", "dj-list", "playlist", "list"):
            return self.cmd_dj_list()

        if p_lower in ("/clear-dj", "/clear-dj-list", "/cleardj", "clear-dj", "clear dj"):
            return self.cmd_clear_dj()

        if p_lower in ("/follow-dj", "/load-dj", "/play-dj", "follow-dj", "follow dj"):
            if self.follow_dj_handler:
                return self.follow_dj_handler()
            return self.cmd_follow_dj()

        if (
            p_lower.startswith("/vibe")
            or p_lower.startswith("/lyrics")
            or p_lower == "vibe"
            or p_lower == "lyrics"
            or p_lower == "lyric vibe"
            or p_lower.startswith("vibe ")
            or p_lower.startswith("lyrics ")
        ):
            parts = user_prompt.strip().split(maxsplit=1)
            track_id = None
            if len(parts) > 1:
                val = parts[1].strip()
                if val.isdigit():
                    num = int(val)
                    if self.last_candidates and 1 <= num <= len(self.last_candidates):
                        track_id = self.last_candidates[num - 1].get("track_id")
                    else:
                        track_id = num
            return self.cmd_vibe(track_id=track_id)

        if p_lower.startswith("/search "):
            query = user_prompt.strip()[8:].strip()
            return self.cmd_search(query)

        if p_lower.startswith("/play "):
            arg = user_prompt.strip()[6:].strip()
            return self.cmd_play(arg)

        if p_lower.startswith("/model"):
            parts = user_prompt.strip().split(maxsplit=1)
            if len(parts) > 1:
                self.model = parts[1].strip()
                return f"🎧 Switched active DJ model to **{self.model}**."
            return f"🎧 Active DJ model is **{self.model}** (use `/model <name>` to switch)."

        if p_lower in ("/help", "help", "?"):
            return (
                "🎧 **Karaoke AI DJ Commands**:\n"
                "• `/suggest [harmonic|energy_up|cool_down|acoustic]` — Next song recommendations\n"
                "• `1`, `2`, `3`, `4` or `/queue <#>` — Add suggestion to playlist & player queue\n"
                "• `/queue all` (or `all`) — Add all suggestions to 'dj-list'\n"
                "• `/dj-list` (or `/playlist`) — View tracks currently in the DJ playlist\n"
                "• `/follow-dj` — Follow & load the DJ playlist in the active player\n"
                "• `/clear-dj` — Clear all songs from 'dj-list'\n"
                "• `/now` — What's playing now & detected Camelot key/BPM\n"
                "• `/vibe [track_id]` — Singing mood, emotional arc & vocal tips\n"
                "• `/stats` — Library summary, crowd favorites & hit counts\n"
                "• `/search <query>` — Search the 18,000+ local track database\n"
                "• `/play <track_id>` — Trigger track playback on host player\n"
                "• `/model <name>` — Switch Ollama model (e.g. `qwen3:latest`, `qwen3:1.7b`)\n"
                "• Or just ask in natural language (e.g. *'Find an 8A Camelot pop anthem'*)"
            )

        # Natural Language query: enrich with real library context
        augmented_context = self._gather_context_for_prompt(user_prompt)

        # Attempt Ollama inference
        llm_reply = self._query_ollama(user_prompt, extra_context=augmented_context)
        if llm_reply:
            return llm_reply

        # Fallback if Ollama is unreachable or slow:
        if "suggest" in p_lower or "next" in p_lower or "after" in p_lower:
            return self.cmd_suggest()
        if "playing" in p_lower:
            return self.cmd_now_playing()
        if "stat" in p_lower:
            return self.cmd_stats()

        # Default smart search
        return self.cmd_search(user_prompt)

    def cmd_now_playing(self) -> str:
        raw = mcp_server.get_now_playing()
        data = json.loads(raw)
        if data.get("status") == "playing":
            lib = data.get("library_match") or {}
            key_str = lib.get("key") or "unknown"
            cam_str = lib.get("camelot") or "?"
            bpm_str = f"{lib.get('bpm')} BPM" if lib.get("bpm") else "unknown BPM"
            return (
                f"🎶 **Currently Rocking**: **{data.get('artist')}** — *{data.get('title')}*\n"
                f"• Player: `{data.get('player')}`\n"
                f"• Harmonic Key: `{key_str}` (Camelot `{cam_str}`) • Tempo: `{bpm_str}`\n"
                f"• Genre: {lib.get('genre') or 'Various'}"
            )
        else:
            recents = data.get("recent_popular_tracks", [])
            lines = [f"💤 **Player is currently idle** (no active track)."]
            if recents:
                lines.append("\n🔥 **Crowd favorites ready to drop**:")
                for t in recents:
                    lines.append(f"  • {t['artist']} — {t['title']} ({t['plays']} plays)")
            lines.append("\nTip: Type `/suggest` or `/search <artist>` to get the party rolling!")
            return "\n".join(lines)

    def cmd_suggest(self, strategy: str = "harmonic") -> str:
        # Check now playing first
        now_data = json.loads(mcp_server.get_now_playing())
        seed_id = None
        artist = None
        title = None
        if now_data.get("status") == "playing":
            artist = now_data.get("artist")
            title = now_data.get("title")

        raw = mcp_server.suggest_next_tracks(
            track_id=seed_id,
            artist=artist,
            title=title,
            strategy=strategy,
            limit=4,
        )
        data = json.loads(raw)
        if "error" in data:
            # Pick a crowd top track to seed
            stats = json.loads(mcp_server.get_dj_stats())
            top = stats.get("top_tracks", [])
            if top:
                seed = top[0]
                raw = mcp_server.suggest_next_tracks(artist=seed["artist"], title=seed["title"], strategy=strategy, limit=4)
                data = json.loads(raw)

        seed = data.get("seed", {})
        suggestions = data.get("suggestions", [])
        self.last_suggestions = list(suggestions)
        self.last_candidates = list(suggestions)

        lines = [
            f"🎧 **DJ Follow-Up Suggestions** (Strategy: `{strategy}`)",
            f"From seed: **{seed.get('artist')}** — *{seed.get('title')}* "
            f"(`{seed.get('key')}` / Camelot `{seed.get('camelot')}` / {round(seed.get('bpm', 0))} BPM)\n"
        ]
        if not suggestions:
            lines.append("No direct harmonic match found. Try `/suggest energy_up` or `/search`!")
        else:
            for i, s in enumerate(suggestions, 1):
                synced_icon = "🎤 [synced]" if s.get("has_synced_lyrics") else "📝"
                tid_str = f"[dim][#{s.get('track_id')}][/dim] " if s.get("track_id") else ""
                lines.append(
                    f"[bold cyan][{i}][/bold cyan] {tid_str}**{s['artist']}** — *{s['title']}* "
                    f"(`{s.get('key')}` / `{s.get('camelot')}` / {s.get('bpm')} BPM) {synced_icon}\n"
                    f"    ↳ *Reason: {s.get('reason')}*"
                )
            lines.append(
                f"\n👉 **Quick Action**: Type **1** to **{len(suggestions)}** to queue, or type **'all'** to queue all (or click buttons below)."
            )
            lines.append("👉 Playlist: Managed in **dj-list** (type `/dj-list` to view, `/follow-dj` to follow in player)")
        return "\n".join(lines)

    def cmd_queue(self, arg: str = "") -> str:
        """Add tracks to the 'dj-list' playlist."""
        arg = arg.strip()
        tracks_to_add: list[dict[str, Any]] = []

        if not arg:
            return self.cmd_dj_list()

        if arg.lower() == "all":
            if not self.last_candidates:
                return "❌ No active suggestions or search results to add. Try `/suggest` or `/search` first!"
            tracks_to_add = list(self.last_candidates)
        elif arg.isdigit() and self.last_candidates and 1 <= int(arg) <= len(self.last_candidates):
            tracks_to_add = [self.last_candidates[int(arg) - 1]]
        elif arg.isdigit():
            # Specific track ID
            tid = int(arg)
            raw = mcp_server.add_to_dj_playlist(track_id=tid)
            data = json.loads(raw)
            if "error" in data:
                return f"❌ {data['error']}"
            t = data.get("track", {})
            if self.on_tracks_queued:
                self.on_tracks_queued([t])
            yt_link = data.get("playlist_url", "")
            yt_notice = f"\n🔗 **YouTube Music**: {yt_link}" if yt_link and "music.youtube.com" in yt_link else ""
            return (
                f"✅ **Added to DJ Playlist ('Karaoke: DJ List')** at position #{data.get('position', 1)}:\n"
                f"• **[#{t.get('track_id')}] {t.get('artist')}** — *{t.get('title')}* "
                f"(`{t.get('camelot', '?')}` / {t.get('bpm', '?')} BPM){yt_notice}\n"
                f"👉 Use `/dj-list` to view, or `/follow-dj` to sync with live player."
            )
        else:
            # Query by text
            s_data = json.loads(mcp_server.search_songs(query=arg, limit=1))
            found = s_data.get("tracks", [])
            if not found:
                return f"❌ Could not find any songs matching '{arg}' to add to DJ playlist."
            tracks_to_add = [found[0]]

        added_info = []
        for candidate in tracks_to_add:
            raw = mcp_server.add_to_dj_playlist(track_id=candidate.get("track_id"))
            data = json.loads(raw)
            if data.get("ok"):
                added_info.append(data.get("track", candidate))

        if not added_info:
            return "❌ Failed to add songs to DJ playlist."

        if self.on_tracks_queued:
            self.on_tracks_queued(added_info)

        url_notice = ""
        try:
            raw_pl = mcp_server.get_dj_playlist()
            pl_data = json.loads(raw_pl)
            yt_url = pl_data.get("url", "")
            if yt_url and "music.youtube.com" in yt_url:
                url_notice = f"\n🔗 **YouTube Music**: {yt_url}"
        except Exception:
            pass

        lines = [f"✅ **Added {len(added_info)} song(s) to DJ Playlist ('Karaoke: DJ List')**:"]
        for t in added_info:
            lines.append(
                f"• **[#{t.get('track_id')}] {t.get('artist')}** — *{t.get('title')}* "
                f"(`{t.get('camelot', '?')}` / {t.get('bpm', '?')} BPM)"
            )
        lines.append(f"\n👉 Live player sync: Type `/follow-dj` to follow this playlist in the player.{url_notice}")
        return "\n".join(lines)

    def cmd_dj_list(self) -> str:
        raw = mcp_server.get_dj_playlist()
        data = json.loads(raw)
        tracks = data.get("tracks", [])
        yt_url = data.get("url", "")
        url_line = f"\n🔗 **YouTube Music**: {yt_url}" if yt_url and "music.youtube.com" in yt_url else ""
        if not tracks:
            return (
                f"📋 **DJ Playlist ('Karaoke: DJ List') is currently empty.**{url_line}\n"
                "Tip: Type `/suggest` and pick a song (`1`, `2`, `/queue 1`, or `/queue all`) to build your setlist!"
            )
        lines = [
            f"📋 **Karaoke AI DJ Playlist ('Karaoke: DJ List')** ({len(tracks)} track{'s' if len(tracks) != 1 else ''}):{url_line}"
        ]
        for t in tracks:
            pos = t.get("position", "?")
            tid_str = f"[dim][#{t.get('track_id')}][/dim] " if t.get("track_id") else ""
            lines.append(f"  {pos}. {tid_str}**{t.get('artist')}** — *{t.get('title')}*")
        lines.append("\n👉 Controls: Type `/follow-dj` to load into active player, or `/clear-dj` to empty.")
        return "\n".join(lines)

    def cmd_clear_dj(self) -> str:
        mcp_server.clear_dj_playlist()
        return "🗑️ **DJ Playlist ('Karaoke: DJ List') has been cleared.** Ready for a fresh setlist!"

    def cmd_follow_dj(self) -> str:
        from . import localcache
        yt_pid = "dj-list"
        yt_url = ""
        try:
            from . import ytmusic_playlist
            yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist()
        except Exception:
            pass

        with localcache.connect() as conn:
            tracks = localcache.get_saved_playlist_tracks(yt_pid, conn=conn) or localcache.get_saved_playlist_tracks("dj-list", conn=conn)
        if not tracks:
            return "❌ DJ Playlist ('Karaoke: DJ List') is currently empty. Add tracks with `/suggest` or `1`, `2` first!"
        url_text = f"\n🔗 **YouTube Music**: {yt_url}" if yt_url else ""
        return (
            f"🎧 **DJ Playlist ('Karaoke: DJ List') has {len(tracks)} track(s)**.{url_text}\n"
            "In the Karaoke TUI player, press the **[🎧 Follow DJ]** button or run `/follow-dj` to load into active playback."
        )

    def cmd_vibe(self, track_id: Optional[int] = None) -> str:
        if track_id is None:
            if callable(getattr(self, "current_track_provider", None)):
                try:
                    track_id = self.current_track_provider()
                except Exception as exc:
                    log.debug("current_track_provider failed: %s", exc)
                    track_id = None

            if track_id is None and getattr(self, "current_track_id", None):
                track_id = self.current_track_id

        if track_id is None:
            try:
                now_data = json.loads(mcp_server.get_now_playing())
                if now_data.get("track_id"):
                    track_id = now_data["track_id"]
                elif now_data.get("status") == "playing":
                    with localcache.connect() as conn:
                        track_id = localcache.find_track_id_relaxed(
                            now_data.get("artist", ""),
                            now_data.get("title", ""),
                            conn,
                        )
                        if not track_id and now_data.get("url"):
                            f = localcache.find_track_by_url(now_data["url"], conn)
                            if f:
                                track_id = f[0]
            except Exception as exc:
                log.debug("now_playing resolution in cmd_vibe failed: %s", exc)

        if track_id is None and self.last_candidates:
            track_id = self.last_candidates[0].get("track_id")

        if track_id is None:
            try:
                stats = json.loads(mcp_server.get_dj_stats())
                top = stats.get("top_tracks", [])
                if top:
                    with localcache.connect() as conn:
                        track_id = localcache.find_track_id_relaxed(top[0].get("artist", ""), top[0].get("title", ""), conn)
            except Exception:
                pass

        if track_id is None:
            return "❌ No active track detected. Start playing a song or specify a track ID: `/vibe <track_id>`"

        data = json.loads(mcp_server.analyze_lyric_vibe(track_id))
        if "error" in data:
            return f"❌ {data['error']}"

        lines = [
            f"✨ **Vibe & Lyric Analysis** for **{data.get('artist')}** — *{data.get('title')}*",
            f"• Dominant Mood: **{data.get('dominant_mood', '').upper()}**",
            f"• Key Character: *{data.get('key_character')}*",
            f"• Lyrics: {data.get('line_count', 0)} lines (Synced: {data.get('has_synced_lyrics')})",
        ]
        if data.get("ascii_arc"):
            lines.append(f"• Emotional Arc: `{data.get('ascii_arc')}`")
        lines.append("")
        lines.append("🎤 **Singing Delivery Tips**:")
        for tip in data.get("dj_singer_tips", []):
            lines.append(f"  • {tip}")
        if data.get("ascii_bars"):
            lines.append(f"\n**Sentiment Spectrum**:\n{data.get('ascii_bars')}")
        return "\n".join(lines)

    def cmd_stats(self) -> str:
        data = json.loads(mcp_server.get_dj_stats())
        lines = [
            "📊 **Karaoke Crowd & DJ Stats**:",
            f"• Total Library Events: {data.get('total_events', 0):,}",
            f"• Recorded Plays: {data.get('plays', 0):,} (Distinct tracks: {data.get('distinct_tracks', 0):,})",
            f"• Cache Hit Rate: {data.get('cache_hit_rate', '0%')}",
            "",
            "🏆 **Top 3 Crowd Anthems**:",
        ]
        for t in data.get("top_tracks", [])[:3]:
            lines.append(f"  1. {t['artist']} — {t['title']} ({t['plays']} plays)")
        return "\n".join(lines)

    def cmd_search(self, query: str) -> str:
        data = json.loads(mcp_server.search_songs(query=query, limit=5))
        tracks = data.get("tracks", [])
        self.last_candidates = list(tracks)
        if not tracks:
            return f"🔍 No tracks found matching '{query}'. Try another search query!"
        lines = [f"🔍 **Found {data.get('count')} tracks matching '{query}'**:"]
        for i, t in enumerate(tracks, 1):
            synced = "🎤 [synced]" if t.get("has_synced_lyrics") else ""
            tid_str = f"[dim][#{t['track_id']}][/dim] "
            lines.append(
                f"[bold cyan][{i}][/bold cyan] {tid_str}**{t['artist']}** — *{t['title']}* "
                f"(`{t.get('key') or '?'}` / Camelot `{t.get('camelot') or '?'}` / {round(t.get('bpm') or 0)} BPM) {synced}"
            )
        lines.append(f"\n👉 Type **1** to **{len(tracks)}** to queue to **dj-list**, or `/play <#>` to play directly.")
        return "\n".join(lines)

    def cmd_play(self, arg: str) -> str:
        track_id = None
        if arg.isdigit():
            val = int(arg)
            if self.last_candidates and 1 <= val <= len(self.last_candidates):
                track_id = self.last_candidates[val - 1].get("track_id")
            else:
                track_id = val
        else:
            res = json.loads(mcp_server.search_songs(query=arg, limit=1))
            if res.get("tracks"):
                track_id = res["tracks"][0]["track_id"]

        if not track_id:
            return f"❌ Could not find track to play for '{arg}'."

        res = json.loads(mcp_server.play_track(track_id))
        if "error" in res:
            return f"❌ {res['error']}"
        return (
            f"▶️ **Now Playing!** Track #{res['track_id']}: **{res['artist']}** — *{res['title']}*\n"
            f"Target: `{res['target']}`\n"
            f"CLI: `{res['karaoke_cli_command']}`"
        )

    def _gather_context_for_prompt(self, prompt: str) -> str:
        context_parts = []
        try:
            now = json.loads(mcp_server.get_now_playing())
            if now.get("status") == "playing":
                context_parts.append(
                    f"Currently playing: '{now.get('title')}' by '{now.get('artist')}' "
                    f"(Camelot key: {now.get('library_match', {}).get('camelot', '?')}, "
                    f"BPM: {now.get('library_match', {}).get('bpm', '?')})."
                )
        except Exception:
            pass
        return " ".join(context_parts)

    def _query_ollama(self, prompt: str, extra_context: str = "") -> Optional[str]:
        url = f"{OLLAMA_BASE_URL}/api/chat"
        system_content = DJ_SYSTEM_PROMPT
        if extra_context:
            system_content += f"\nLive context: {extra_context}"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "num_predict": 180,
                "temperature": 0.7,
            },
        }

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    body = json.loads(response.read().decode("utf-8"))
                    msg = body.get("message", {}).get("content", "").strip()
                    if msg:
                        return f"🎧 {msg}"
        except Exception:
            return None
        return None


class DJChatScreen(ModalScreen[None]):
    """Interactive modal screen for chatting with the Karaoke AI DJ in the TUI."""

    CSS = """
    DJChatScreen {
        align: center middle;
    }
    #dj-chat-dialog {
        width: 100;
        height: 90%;
        border: thick $accent;
        padding: 1 2;
        background: $surface;
        border-title-align: center;
    }
    #chat-log {
        height: 1fr;
        border: solid $accent-muted;
        background: $background;
        padding: 0 1;
        margin-bottom: 1;
    }
    #quick-actions {
        height: auto;
        margin-bottom: 1;
    }
    .quick-row {
        height: 3;
        align: center middle;
    }
    .quick-row Button {
        margin: 0 1;
        min-width: 12;
    }
    #input-row {
        height: 3;
    }
    #chat-input {
        width: 1fr;
        margin-right: 1;
    }
    #btn-send {
        width: 12;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("ctrl+l", "clear_log", "Clear Chat"),
    ]

    def __init__(
        self,
        session: Optional[DJChatSession] = None,
        current_track_provider: Optional[Any] = None,
        current_track_id: Optional[int] = None,
        initial_prompt: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.session = session or DJChatSession(
            current_track_provider=current_track_provider,
            current_track_id=current_track_id,
        )
        if current_track_provider and not getattr(self.session, "current_track_provider", None):
            self.session.current_track_provider = current_track_provider
        if current_track_id and not getattr(self.session, "current_track_id", None):
            self.session.current_track_id = current_track_id
        self.initial_prompt = initial_prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dj-chat-dialog") as dialog:
            dialog.border_title = "🎧 Karaoke AI DJ Booth"
            dialog.border_subtitle = "esc: close · /suggest /queue /dj-list /follow-dj /vibe /help"
            yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
            with Vertical(id="quick-actions"):
                with Horizontal(classes="quick-row"):
                    yield Button("⚡ Suggest Next", id="btn-suggest", variant="primary")
                    yield Button("➕ Add #1", id="btn-add-1", variant="success")
                    yield Button("➕ Add All", id="btn-add-all", variant="default")
                    yield Button("📋 DJ List", id="btn-list", variant="default")
                    yield Button("🎧 Follow DJ", id="btn-follow", variant="default")
                with Horizontal(classes="quick-row"):
                    yield Button("🎵 Now Playing", id="btn-now", variant="default")
                    yield Button("✨ Lyric Vibe", id="btn-vibe", variant="default")
                    yield Button("📊 Crowd Stats", id="btn-stats", variant="default")
                    yield Button("🗑️ Clear DJ", id="btn-clear-dj", variant="error")
                    yield Button("❓ Help", id="btn-help", variant="default")
            with Horizontal(id="input-row"):
                yield Input(placeholder="Ask the DJ (e.g. '1', '/queue all', '/dj-list', 'suggest next')...", id="chat-input")
                yield Button("Send", id="btn-send", variant="primary")

    def on_mount(self) -> None:
        self.session.on_tracks_queued = self._on_tracks_queued
        self.session.follow_dj_handler = self.action_follow_dj
        log = self.query_one("#chat-log", RichLog)
        log.write(
            "[bold cyan]🎧 AI DJ:[/bold cyan] Yo! Welcome to the [bold gold1]Karaoke DJ Booth[/bold gold1]! 🎤\n"
            "I'm wired into your 18,000+ track library with Camelot harmonic keys and BPM pacing.\n"
            "Managed playlist: [bold cyan]dj-list[/bold cyan]. Click [bold green][⚡ Suggest Next][/bold green] to get started, "
            "then type [bold]1[/bold], [bold]2[/bold], or click [bold][➕ Add #1][/bold] to queue!"
        )
        self.query_one("#chat-input", Input).focus()
        if self.initial_prompt:
            self.set_timer(0.05, lambda: self._handle_user_message(self.initial_prompt))

    def action_clear_log(self) -> None:
        self.query_one("#chat-log", RichLog).clear()

    def action_follow_dj(self) -> str:
        from . import localcache, ytmusic_playlist, player_open
        yt_pid = "dj-list"
        yt_url = ""
        try:
            yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist()
        except Exception:
            pass

        with localcache.connect() as conn:
            local_tracks = localcache.get_saved_playlist_tracks(yt_pid, conn=conn)
            client = ytmusic_playlist.get_ytmusic_client()
            if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
                reconciled_rows, _ = ytmusic_playlist.reconcile_playlist_with_remote(
                    yt_pid, local_tracks, client=client, conn=conn
                )
            else:
                reconciled_rows = local_tracks
            pl = localcache.find_saved_playlist_by_id(yt_pid, conn=conn)

        tracks_to_load = reconciled_rows or local_tracks
        if not tracks_to_load:
            return "❌ DJ Playlist ('Karaoke: DJ List') is currently empty. Add tracks with `/suggest` or `1`, `2` first!"

        rows = [
            {
                "track_id": t.get("track_id"),
                "artist": t.get("artist") or "",
                "title": t.get("title") or "",
                "url": t.get("url") or (f"https://music.youtube.com/watch?v={t['video_id']}" if t.get("video_id") else ""),
                "kind": "ytmusic",
                "key": str(t.get("key") or "—"),
            }
            for t in tracks_to_load
        ]
        pl_name = (pl.get("name") if pl else "") or "Karaoke: DJ List"
        url_text = f"\n🔗 **YouTube Music**: https://music.youtube.com/playlist?list={yt_pid}" if yt_pid.startswith("PL") else ""

        first_vid = next((r.get("video_id") or localcache.extract_youtube_id(r.get("url", "")) for r in rows if (r.get("video_id") or r.get("url"))), "")
        play_url = (
            f"https://music.youtube.com/watch?v={first_vid}&list={yt_pid}"
            if first_vid and yt_pid.startswith(("PL", "VL", "RD", "OLAK5"))
            else (yt_url or f"https://music.youtube.com/playlist?list={yt_pid}")
        )
        try:
            player_open.open_song_url(play_url, "youtube_music_playlist", prefer_audio=True)
        except Exception as exc:
            log.warning("Could not open player: %s", exc)

        if hasattr(self.app, "_apply_restored_playlist"):
            try:
                self.app.call_from_thread(self.app._apply_restored_playlist, rows, yt_pid, pl_name)
            except Exception:
                self.app._apply_restored_playlist(rows, yt_pid, pl_name)
            return f"🎧 **Opened & following DJ Playlist ('{pl_name}') in YouTube Music!** ({len(rows)} tracks loaded){url_text}"
        return f"🎧 DJ Playlist ('{pl_name}') ready with {len(rows)} tracks.{url_text}"

    def _on_tracks_queued(self, added_tracks: list[dict[str, Any]]) -> None:
        if not hasattr(self.app, "_queue"):
            return

        def _sync() -> None:
            from . import ytmusic_playlist, player_open
            yt_pid = "dj-list"
            try:
                yt_pid, _ = ytmusic_playlist.resolve_or_create_dj_playlist()
            except Exception:
                pass

            with localcache.connect() as conn:
                local_tracks = localcache.get_saved_playlist_tracks(yt_pid, conn=conn)
                client = ytmusic_playlist.get_ytmusic_client()
                if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
                    reconciled_rows, _ = ytmusic_playlist.reconcile_playlist_with_remote(
                        yt_pid, local_tracks, client=client, conn=conn
                    )
                else:
                    reconciled_rows = local_tracks
                pl = localcache.find_saved_playlist_by_id(yt_pid, conn=conn)

            tracks_to_load = reconciled_rows or local_tracks
            rows = [
                {
                    "track_id": t.get("track_id"),
                    "artist": str(t.get("artist") or ""),
                    "title": str(t.get("title") or ""),
                    "url": str(t.get("url") or (f"https://music.youtube.com/watch?v={t['video_id']}" if t.get("video_id") else "")),
                    "kind": "ytmusic",
                    "key": str(t.get("key") or t.get("camelot") or "—"),
                }
                for t in tracks_to_load
            ]
            pl_name = (pl.get("name") if pl else "") or "Karaoke: DJ List"
            self.app._apply_restored_playlist(rows, yt_pid, pl_name)

            # Open/navigate in player if not currently playing this playlist or if idle
            det = getattr(self.app, "_det", None)
            curr_pid = getattr(self.app, "_active_playlist_id", "")
            if not det or not det.is_active or curr_pid != yt_pid:
                first_vid = next((r.get("video_id") or localcache.extract_youtube_id(r.get("url", "")) for r in rows if (r.get("video_id") or r.get("url"))), "")
                play_url = (
                    f"https://music.youtube.com/watch?v={first_vid}&list={yt_pid}"
                    if first_vid and yt_pid.startswith(("PL", "VL", "RD", "OLAK5"))
                    else f"https://music.youtube.com/playlist?list={yt_pid}"
                )
                try:
                    player_open.open_song_url(play_url, "youtube_music_playlist", prefer_audio=True)
                except Exception as exc:
                    log.warning("Could not open player for DJ playlist: %s", exc)

            if hasattr(self.app, "notify"):
                self.app.notify(f"DJ synced '{pl_name}' with YouTube Music ({len(rows)} tracks)")

        try:
            self.app.call_from_thread(_sync)
        except Exception:
            try:
                _sync()
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id
        if btn_id == "btn-now":
            self._handle_user_message("/now")
        elif btn_id == "btn-suggest":
            self._handle_user_message("/suggest")
        elif btn_id == "btn-add-1":
            if not self.session.last_candidates:
                self._handle_user_message("/suggest")
            else:
                self._handle_user_message("/queue 1")
        elif btn_id == "btn-add-all":
            if not self.session.last_candidates:
                self._handle_user_message("/suggest")
            else:
                self._handle_user_message("/queue all")
        elif btn_id == "btn-list":
            self._handle_user_message("/dj-list")
        elif btn_id == "btn-follow":
            self._handle_user_message("/follow-dj")
        elif btn_id == "btn-vibe":
            self._handle_user_message("/vibe")
        elif btn_id == "btn-stats":
            self._handle_user_message("/stats")
        elif btn_id == "btn-clear-dj":
            self._handle_user_message("/clear-dj")
        elif btn_id == "btn-help":
            self._handle_user_message("/help")
        elif btn_id == "btn-send":
            inp = self.query_one("#chat-input", Input)
            val = inp.value.strip()
            if val:
                inp.value = ""
                self._handle_user_message(val)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        if not val:
            return
        event.input.value = ""
        self._handle_user_message(val)

    def _handle_user_message(self, text: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(f"\n[bold green]You:[/bold green] {escape(text)}")
        log.write("[dim]🎧 DJ is thinking...[/dim]")
        self._process_dj_response(text)

    @work(thread=True)
    def _process_dj_response(self, text: str) -> None:
        response = self.session.ask(text)
        self.app.call_from_thread(self._update_chat_response, response)

    def _update_chat_response(self, response: str) -> None:
        log = self.query_one("#chat-log", RichLog)
        log.write(f"[bold cyan]🎧 DJ:[/bold cyan] {response}\n")


class StandaloneDJChatApp(App):
    """Standalone TUI app for testing the DJ Chat directly."""

    def on_mount(self) -> None:
        self.push_screen(DJChatScreen())


def main() -> None:
    """Run the DJ Chat as a standalone application."""
    app = StandaloneDJChatApp()
    app.run()


if __name__ == "__main__":
    main()
