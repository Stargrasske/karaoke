"""Unified API Client for Karaoke Library and Control APIs.

Bridges the TUI, CLI, and future Web UI to the backend HTTP endpoints:
- Library API (`http://localhost:8000`, cluster-deployable)
- Control API (`http://127.0.0.1:8765`, host/desktop-bound)

Supports fallback: if the HTTP API server is unreachable, methods can execute
the underlying Python function locally so single-process CLI/TUI modes never fail.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .logger import log


def _lib_api_url() -> str:
    host = os.environ.get("KARAOKE_API_HOST", "localhost")
    port = os.environ.get("KARAOKE_API_PORT", "8000")
    if host == "0.0.0.0":
        host = "localhost"
    return f"http://{host}:{port}"


def _ctrl_api_url() -> str:
    host = os.environ.get("KARAOKE_CTRL_HOST", "127.0.0.1")
    port = os.environ.get("KARAOKE_CTRL_PORT", "8765")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{port}"


def _clean_params(params: dict[str, Any]) -> dict[str, str]:
    return {k: str(v).lower() if isinstance(v, bool) else str(v)
            for k, v in params.items() if v is not None}


class ApiClient:
    """HTTP client wrapping the Karaoke Library API and Control API endpoints."""

    def __init__(
        self,
        lib_url: Optional[str] = None,
        ctrl_url: Optional[str] = None,
        timeout: float = 5.0,
        fallback_local: bool = True,
    ) -> None:
        self.lib_url = (lib_url or _lib_api_url()).rstrip("/")
        self.ctrl_url = (ctrl_url or _ctrl_api_url()).rstrip("/")
        self.timeout = timeout
        self.fallback_local = fallback_local

    def _http_get(self, base_url: str, path: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        url = f"{base_url}{path}"
        if params:
            clean_params = {k: str(v).lower() if isinstance(v, bool) else str(v)
                            for k, v in params.items() if v is not None}
            if clean_params:
                url += "?" + urllib.parse.urlencode(clean_params)
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.debug("HTTP GET %s failed: %s", url, exc)
            return None

    def _http_post(self, base_url: str, path: str, body: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        url = f"{base_url}{path}"
        data = json.dumps(body or {}).encode("utf-8") if body is not None else b"{}"
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.debug("HTTP POST %s failed: %s", url, exc)
            return None

    def _http_patch(self, base_url: str, path: str, body: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        url = f"{base_url}{path}"
        data = json.dumps(body or {}).encode("utf-8") if body is not None else b"{}"
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="PATCH")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.debug("HTTP PATCH %s failed: %s", url, exc)
            return None

    def _http_delete(self, base_url: str, path: str) -> Optional[dict[str, Any]]:
        url = f"{base_url}{path}"
        try:
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            log.debug("HTTP DELETE %s failed: %s", url, exc)
            return None

    # -- Library API methods --

    def health(self) -> Optional[dict[str, Any]]:
        return self._http_get(self.lib_url, "/api/health")

    def list_tracks(self, q: Optional[str] = None, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        res = self._http_get(self.lib_url, "/api/tracks", {"q": q, "limit": limit, "offset": offset})
        if res is not None and isinstance(res, list):
            return res
        return []

    def get_track(self, track_id: int) -> Optional[dict[str, Any]]:
        return self._http_get(self.lib_url, f"/api/tracks/{track_id}")

    def get_track_analysis_history(self, track_id: int) -> dict[str, Any]:
        res = self._http_get(self.lib_url, f"/api/tracks/{track_id}/analysis/history")
        if res is not None:
            return res
        return {"track_id": track_id, "history": [], "count": 0}

    def get_stats(self, limit: int = 10, days: Optional[float] = None) -> Optional[dict[str, Any]]:
        return self._http_get(self.lib_url, "/api/stats", {"limit": limit, "days": days})

    def suggest_queue(self, track_ids: list[int], limit: int = 10,
                      per_artist: int = 2) -> list[dict[str, Any]]:
        """Tracks that keep the vibe of a whole queue going (audio similarity)."""
        body = {"track_ids": track_ids, "limit": limit, "per_artist": per_artist}
        res = self._http_post(self.lib_url, "/api/queue/suggest", body)
        if res is not None and isinstance(res, list):
            return res
        if self.fallback_local:
            from . import queue_suggest, localcache
            try:
                suggestions = queue_suggest.suggest_for_queue(
                    track_ids, limit=limit, per_artist=per_artist)
                with localcache.connect() as conn:
                    return [{
                        "track_id": s.track_id, "artist": s.artist,
                        "title": s.title, "score": s.score,
                        "seeds_matched": s.seeds_matched, "space": s.space,
                        "url": queue_suggest.playable_url(s.track_id, conn),
                    } for s in suggestions]
            except Exception:
                return []
        return []

    def sounds_like(self, track_id: int, limit: int = 10,
                    per_artist: int = 2) -> list[dict[str, Any]]:
        """Find tracks that acoustically sound like a specific track."""
        res = self._http_get(self.lib_url, f"/api/tracks/{track_id}/sounds-like",
                             {"limit": limit, "per_artist": per_artist})
        if res is not None and isinstance(res, list):
            return res
        return self.suggest_queue([track_id], limit=limit, per_artist=per_artist)

    def list_recordings(
        self,
        status: Optional[str] = None,
        source: Optional[str] = None,
        has_marks: Optional[bool] = None,
        identified_only: Optional[bool] = None,
        keep_audio: Optional[bool] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        q: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        params = {
            "status": status, "source": source, "has_marks": has_marks,
            "identified_only": identified_only, "keep_audio": keep_audio,
            "since": since, "until": until, "q": q, "limit": limit, "offset": offset,
        }
        res = self._http_get(self.lib_url, "/api/recordings", params)
        if res is not None:
            return res
        if self.fallback_local:
            from .api import list_recordings as _local_list
            return _local_list(
                status=status, source=source, has_marks=has_marks,
                identified_only=identified_only, keep_audio=keep_audio,
                since=since, until=until, q=q, limit=limit, offset=offset,
            )
        return {"recordings": [], "count": 0}

    def get_recording(
        self, recording_id: int, confident_only: bool = False, min_marks: Optional[int] = None
    ) -> Optional[dict[str, Any]]:
        res = self._http_get(self.lib_url, f"/api/recordings/{recording_id}",
                            {"confident_only": confident_only, "min_marks": min_marks})
        if res is not None:
            return res
        if self.fallback_local:
            from .api import get_recording as _local_get
            try:
                return _local_get(recording_id, confident_only=confident_only, min_marks=min_marks)
            except Exception:
                return None
        return None

    def update_recording(self, recording_id: int, note: Optional[str] = None, keep_audio: Optional[bool] = None) -> Optional[dict[str, Any]]:
        body = {}
        if note is not None:
            body["note"] = note
        if keep_audio is not None:
            body["keep_audio"] = keep_audio
        return self._http_patch(self.lib_url, f"/api/recordings/{recording_id}", body)

    def radio_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        res = self._http_get(self.lib_url, "/api/radio/sessions", {"limit": limit})
        if res and "sessions" in res:
            return res["sessions"]
        if self.fallback_local:
            from . import localcache
            return localcache.get_radio_sessions(limit=limit)
        return []

    def radio_session(self, session_id: int) -> Optional[dict[str, Any]]:
        res = self._http_get(self.lib_url, f"/api/radio/sessions/{session_id}")
        if res and "session" in res:
            return res
        if self.fallback_local:
            from . import localcache
            s = localcache.get_radio_session(session_id)
            if s:
                tracks = localcache.get_radio_session_tracks(session_id)
                return {"session": s, "tracks": tracks, "count": len(tracks)}
        return None

    def import_radio_session(self, session_id: int, save_audio: bool = True, resolve_streaming: bool = True) -> dict[str, Any]:
        params = {"save_audio": str(save_audio).lower(), "resolve_streaming": str(resolve_streaming).lower()}
        res = self._http_post(self.ctrl_url, f"/api/radio/sessions/{session_id}/import?save_audio={params['save_audio']}&resolve_streaming={params['resolve_streaming']}")
        if res is not None:
            return res
        if self.fallback_local:
            from . import radio_pipeline
            return radio_pipeline.import_radio_session(session_id, save_audio=save_audio, resolve_streaming=resolve_streaming)
        return {"status": "unreachable"}

    # -- Control API methods --

    def ctrl_health(self) -> Optional[dict[str, Any]]:
        return self._http_get(self.ctrl_url, "/api/health")

    def play(
        self,
        url: Optional[str] = None,
        kind: Optional[str] = None,
        artist: Optional[str] = None,
        title: Optional[str] = None,
        prefer_audio: bool = True,
    ) -> dict[str, Any]:
        body = {"url": url, "kind": kind, "artist": artist, "title": title,
                "prefer_audio": prefer_audio}
        res = self._http_post(self.ctrl_url, "/api/play", body)
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import play_track, PlayRequest
            try:
                return play_track(PlayRequest(
                    url=url, kind=kind, artist=artist, title=title,
                    prefer_audio=prefer_audio))
            except Exception as e:
                return {"status": "error", "detail": str(e)}
        return {"status": "unreachable"}

    def list_play_sessions(self) -> dict[str, Any]:
        res = self._http_get(self.ctrl_url, "/api/play/sessions")
        if res is not None:
            return res
        return {"sessions": [], "count": 0}

    def recent_events(self, since_ts: Optional[float] = None, limit: int = 50) -> dict[str, Any]:
        """Recent Celery task-completion events (Argo Events -> OpenSearch ledger).

        Used by pollers to refresh a finished track in place without a reload.
        """
        params = {"since_ts": since_ts, "limit": limit}
        res = self._http_get(self.ctrl_url, "/api/events/recent", params)
        if res is not None:
            return res
        if self.fallback_local:
            from . import events
            items = events.recent_events(since_ts=since_ts, limit=limit)
            return {"events": items, "count": len(items)}
        return {"events": [], "count": 0}

    def get_play_session(self, session_id: str) -> Optional[dict[str, Any]]:
        return self._http_get(self.ctrl_url, f"/api/play/sessions/{session_id}")

    def stop_play_session(self, session_id: str) -> dict[str, Any]:
        res = self._http_delete(self.ctrl_url, f"/api/play/sessions/{session_id}")
        if res is not None:
            return res
        return {"status": "unreachable", "session_id": session_id}

    def list_players(self) -> dict[str, Any]:
        res = self._http_get(self.ctrl_url, "/api/players")
        if res is not None:
            return res
        if self.fallback_local:
            from .playerctl import list_players as lp, playing_players as pp, playing_player as plp
            names = lp()
            playing = pp()
            return {"players": names, "playing": playing, "active": plp(), "count": len(names)}
        return {"players": [], "playing": [], "active": "", "count": 0}

    def get_current_player(self, player: str = "") -> dict[str, Any]:
        res = self._http_get(self.ctrl_url, "/api/players/current", {"player": player})
        if res is not None:
            return res
        if self.fallback_local:
            from .playerctl import current_metadata, status, position, art_url, playing_player
            target = player or playing_player()
            meta = current_metadata(target)
            return {
                "player": target, "status": status(target), "position_s": position(target),
                "art_url": art_url(target),
                "metadata": None if meta is None else {
                    "artist": meta.artist, "title": meta.title, "album": meta.album,
                    "url": meta.url, "player": meta.player, "mpris_name": meta.mpris_name,
                    "duration": meta.duration,
                },
            }
        return {"player": "", "status": None, "position_s": None, "art_url": None, "metadata": None}

    def player_play_pause(self, player: Optional[str] = None) -> bool:
        res = self._http_post(self.ctrl_url, "/api/players/play-pause", {"player": player})
        if res is not None and res.get("status") == "ok":
            return True
        if self.fallback_local:
            from .playerctl import play_pause
            return play_pause(player or "")
        return False

    def player_pause(self, player: Optional[str] = None) -> bool:
        res = self._http_post(self.ctrl_url, "/api/players/pause", {"player": player})
        if res is not None and res.get("status") == "ok":
            return True
        if self.fallback_local:
            from .playerctl import pause
            return pause(player or "")
        return False

    def player_next(self, player: Optional[str] = None) -> bool:
        res = self._http_post(self.ctrl_url, "/api/players/next", {"player": player})
        if res is not None and res.get("status") == "ok":
            return True
        if self.fallback_local:
            from .playerctl import next_track
            return next_track(player or "")
        return False

    def player_previous(self, player: Optional[str] = None) -> bool:
        res = self._http_post(self.ctrl_url, "/api/players/previous", {"player": player})
        if res is not None and res.get("status") == "ok":
            return True
        if self.fallback_local:
            from .playerctl import previous_track
            return previous_track(player or "")
        return False

    def player_seek(self, offset_s: float, player: Optional[str] = None) -> bool:
        res = self._http_post(self.ctrl_url, "/api/players/seek", {"offset_s": offset_s, "player": player})
        if res is not None and res.get("status") == "ok":
            return True
        if self.fallback_local:
            from .playerctl import seek
            return seek(offset_s, player or "")
        return False

    def get_player_window(self) -> Optional[dict[str, Any]]:
        res = self._http_get(self.ctrl_url, "/api/players/window")
        if res is not None:
            return res
        if self.fallback_local:
            from . import player_open
            return player_open.get_window_bounds()
        return None

    def set_player_window(
        self,
        state: Optional[str] = None,
        left: Optional[int] = None,
        top: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> bool:
        body = {"state": state, "left": left, "top": top, "width": width, "height": height}
        res = self._http_post(self.ctrl_url, "/api/players/window", body)
        if res is not None and res.get("status") in ("ok", "launched"):
            return True
        if self.fallback_local:
            from . import player_open
            if state == "focus":
                return player_open.bring_to_front()
            return player_open.set_window_bounds(state=state, left=left, top=top, width=width, height=height)
        return False

    def restart_player_window(self) -> bool:
        res = self._http_post(self.ctrl_url, "/api/players/window/restart")
        if res is not None and res.get("status") == "ok":
            return True
        if self.fallback_local:
            from . import player_open
            return player_open.restart_kiosk_browser()
        return False

    def record_start(self, source: Optional[str] = None, keep_audio: bool = False, note: Optional[str] = None) -> dict[str, Any]:
        body = {"source": source, "keep_audio": keep_audio, "note": note}
        res = self._http_post(self.ctrl_url, "/api/record/start", body)
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import record_start, RecordRequest
            try:
                return record_start(RecordRequest(source=source, keep_audio=keep_audio, note=note))
            except Exception as e:
                return {"status": "error", "detail": str(e)}
        return {"status": "unreachable"}

    def record_stop(self, recording_id: Optional[int] = None) -> dict[str, Any]:
        body = {"recording_id": recording_id}
        res = self._http_post(self.ctrl_url, "/api/record/stop", body)
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import record_stop, StopRequest
            try:
                return record_stop(StopRequest(recording_id=recording_id))
            except Exception as e:
                return {"status": "error", "detail": str(e)}
        return {"status": "unreachable"}

    def record_status(self) -> dict[str, Any]:
        res = self._http_get(self.ctrl_url, "/api/record/status")
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import record_status
            return record_status()
        return {"recording": [], "count": 0}

    def record_analyse(self, recording_id: int, keep: bool = True,
                       prune_after: bool = False) -> dict[str, Any]:
        res = self._http_post(self.ctrl_url, f"/api/recordings/{recording_id}/analyse?prune_after={'true' if prune_after else 'false'}")
        if res is not None:
            return res
        if self.fallback_local:
            # No server: run synchronously in-process and return the result lines
            # (the HTTP endpoint runs this in the background instead).
            from . import recording_worker
            try:
                lines = recording_worker.analyse(recording_id, prune_after=prune_after)
                return {"status": "analysed", "recording_id": recording_id, "lines": lines}
            except Exception as e:
                return {"status": "error", "detail": str(e)}
        return {"status": "unreachable"}

    def record_discard_audio(self, recording_id: int) -> dict[str, Any]:
        res = self._http_delete(self.ctrl_url, f"/api/recordings/{recording_id}/audio")
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import record_discard
            return record_discard(recording_id)
        return {"status": "unreachable"}

    def sample(self, artist: Optional[str] = None, title: Optional[str] = None, seconds: Optional[float] = None) -> dict[str, Any]:
        body = {"artist": artist, "title": title, "seconds": seconds}
        res = self._http_post(self.ctrl_url, "/api/sample", body)
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import sample_now, SampleRequest
            try:
                return sample_now(SampleRequest(artist=artist, title=title, seconds=seconds))
            except Exception as e:
                return {"status": "error", "detail": str(e)}
        return {"status": "unreachable"}

    def sample_stream_url(self, artist: Optional[str] = None,
                          title: Optional[str] = None,
                          seconds: Optional[float] = None) -> str:
        """Return the SSE URL a TUI/web client can consume for live sample progress."""
        params = _clean_params({"artist": artist, "title": title, "seconds": seconds})
        from urllib.parse import urlencode
        query = urlencode(params)
        return f"{self.ctrl_url}/api/sample/stream" + (f"?{query}" if query else "")

    def scan_folder(
        self,
        dir: str,
        use_fingerprint: bool = True,
        classify_audio: bool = True,
        resolve_streaming: bool = True,
        dry_run: bool = False,
        limit: Optional[int] = None,
    ) -> dict[str, Any]:
        body = {
            "dir": str(dir), "use_fingerprint": use_fingerprint,
            "classify_audio": classify_audio, "resolve_streaming": resolve_streaming,
            "dry_run": dry_run, "limit": limit,
        }
        res = self._http_post(self.ctrl_url, "/api/scan/folder", body)
        if res is not None:
            return res
        if self.fallback_local:
            from .folder_scan import scan_and_ingest_folder
            return scan_and_ingest_folder(
                dir, use_fingerprint=use_fingerprint, classify_audio=classify_audio,
                resolve_streaming=resolve_streaming, dry_run=dry_run, limit=limit,
            )
        return {"status": "unreachable"}

    def audio_cut(self, file_path: str, duration_s: float, start_s: float = 0.0, output_path: Optional[str] = None) -> dict[str, Any]:
        body = {"file_path": file_path, "duration_s": duration_s, "start_s": start_s, "output_path": output_path}
        res = self._http_post(self.ctrl_url, "/api/audio/cut", body)
        if res is not None:
            return res
        if self.fallback_local:
            from .ctrl_api import audio_cut, AudioCutRequest
            try:
                return audio_cut(AudioCutRequest(file_path=file_path, duration_s=duration_s, start_s=start_s, output_path=output_path))
            except Exception as e:
                return {"status": "error", "detail": str(e)}
        return {"status": "unreachable"}
