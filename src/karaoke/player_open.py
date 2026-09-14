"""Launching song URLs in the desktop player/browser.

Split out of :mod:`karaoke.browse` so that callers which only need to open a
URL — the host-side control API, for example — do not pull in Textual and the
whole TUI stack just to spawn ``xdg-open``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .logger import OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log


CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}/json"


def _cdp_page_socket(timeout: float = 0.25) -> "str | None":
    """WebSocket URL of the kiosk browser's first page target, or None."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(CDP_URL, timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode())
    except Exception:
        return None
    for tab in tabs:
        if tab.get("type") == "page" and "webSocketDebuggerUrl" in tab:
            return tab["webSocketDebuggerUrl"]
    return None


class _CdpChannel:
    """One CDP connection on one thread, reused by every caller.

    The previous version ran each command in a fresh thread with
    ``asyncio.run`` and then abandoned that thread on timeout::

        thread.start()
        thread.join(timeout=timeout + 1.5)   # gives up
        return out.get("reply")              # ...and walks away

    A thread that outlives the join keeps a whole event loop alive, and
    :func:`browser_playback` is called from a two-second timer, so every slow
    reply from Chromium leaked one. Observed in a live TUI: **46 stranded
    ``asyncio`` threads**, which on a 24-core box cannot even come from one
    executor -- its cap is 28 -- so they were 46 separate loops.

    They cost more than memory. Quitting joins them, and anyio replaces
    CPython's bounded wait with ``thread.join()`` carrying no timeout at all
    (``anyio/_backends/_asyncio.py``), so the 300-second warning fires and then
    shutdown blocks for good. That is issue #39.

    One connection, one thread, so there is nothing left to strand. A caller
    that times out gives up on its *reply*; the thread stays and serves the
    next request. Commands are serialised by an ``asyncio.Lock`` held inside
    the loop rather than a lock held across threads, so a slow reply delays
    other callers only as far as their own timeouts.
    """

    # Cooldown in seconds after a failure before attempting to contact CDP again.
    # Prevents 5-per-second polling loops from locking up on a dead/hanging socket.
    COOLDOWN = 3.0

    def __init__(self) -> None:
        import itertools
        import threading

        self._start_lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._ws = None
        self._send_lock = None
        self._ids = itertools.count(1)
        self._failed_until = 0.0

    # -- the loop thread ---------------------------------------------------

    def _ensure_loop(self):
        """The channel's event loop, started on first use."""
        import asyncio
        import threading

        with self._start_lock:
            if self._loop is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="cdp", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
            self._ws, self._send_lock = None, None
            return loop

    # -- the connection ----------------------------------------------------

    async def _connection(self):
        """The open socket, reconnecting if the browser dropped it."""
        import websockets

        if self._ws is not None:
            # Chromium drops the socket when the page navigates away, and a
            # send on a dead one raises rather than returning.
            if getattr(self._ws, "close_code", None) is not None:
                self._ws = None
            else:
                try:
                    await self._ws.ping()
                    return self._ws
                except Exception:
                    self._ws = None

        url = _cdp_page_socket(timeout=0.25)
        if not url:
            return None
        try:
            self._ws = await websockets.connect(url)
            return self._ws
        except Exception:
            self._ws = None
            return None

    async def _request(self, method: str, params: dict, timeout: float):
        import asyncio
        import json

        if self._send_lock is None:
            self._send_lock = asyncio.Lock()

        try:
            async with asyncio.timeout(timeout):
                async with self._send_lock:
                    ws = await self._connection()
                    if ws is None:
                        return None
                    request_id = next(self._ids)
                    await ws.send(json.dumps(
                        {"id": request_id, "method": method, "params": params}))
                    deadline = asyncio.get_running_loop().time() + timeout
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            return None
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                        message = json.loads(raw)
                        # CDP interleaves unsolicited events with replies; only a
                        # matching id answers this call.
                        if message.get("id") == request_id:
                            return message
        except Exception:
            return None

    def send(self, method: str, params: dict, timeout: float = 2.0, *, force: bool = False):
        """Run one command, or return None."""
        import asyncio
        import time

        if not force and time.monotonic() < self._failed_until:
            return None

        try:
            import websockets  # noqa: F401
        except ImportError:
            log.debug("websockets not installed; CDP unavailable")
            return None

        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._request(method, params, timeout), loop)
        wait_timeout = timeout + min(0.25, max(0.05, timeout * 0.1))
        try:
            res = future.result(timeout=wait_timeout)
            if res is not None:
                self._failed_until = 0.0
            else:
                self._failed_until = time.monotonic() + self.COOLDOWN
            return res
        except Exception:
            # Trip cooldown so repeated polling does not freeze the caller.
            self._failed_until = time.monotonic() + self.COOLDOWN
            future.cancel()
            self._drop()
            log.debug("CDP %s failed", method, exc_info=True)
            return None

    def _drop(self) -> None:
        """Forget the socket so the next call reconnects."""
        import asyncio

        ws, self._ws = self._ws, None
        if ws is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), self._loop)
            except Exception:
                pass

    def close(self) -> None:
        """Stop the loop and its thread. Safe to call more than once."""
        with self._start_lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread, self._ws = None, None, None
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if thread is not None:
            thread.join(timeout=2.0)


_CHANNEL = _CdpChannel()


def _is_browser_url(url: str, kind: str | None = None) -> bool:
    """Whether falling back to the desktop default browser would spawn clutter.

    Playback should go through the dedicated kiosk/CDP browser. If CDP is slow
    or temporarily unavailable, using ``xdg-open`` launches the user's normal
    browser profile as a stray window, which is both confusing and can steal
    resources from the web-TUI.
    """
    lowered = (url or "").lower()
    return (
        lowered.startswith(("http://", "https://", "spotify:"))
        or "youtube.com" in lowered
        or "youtu.be" in lowered
        or "spotify.com" in lowered
        or kind in {"youtube", "youtube_music", "youtube_search",
                    "youtube_music_search", "spotify", "spotify_web"}
    )


def _allow_xdg_fallback() -> bool:
    return os.environ.get("KARAOKE_ALLOW_XDG_OPEN_FALLBACK") == "1"


def _cdp_send(method: str, params: dict, *, timeout: float = 2.0, force: bool = False):
    """Send one CDP command and return its result, or None."""
    return _CHANNEL.send(method, params, timeout=timeout, force=force)


def close_cdp() -> None:
    """Shut the shared CDP connection down, for use when an app exits."""
    _CHANNEL.close()


_BYPASS_BEFOREUNLOAD_JS = """(() => {
  try {
    window.onbeforeunload = null;
    window.onpagehide = null;
  } catch (e) {}
})()"""

_DISMISS_DIALOGS_JS = """(() => {
  let clickedCount = 0;
  const keywords = [
    'understand', 'proceed', 'exit app', 'play anyway', 'confirm', 'dismiss', 'continue', 'got it', 'accept',
    'begrijp', 'doorgaan', 'verdergaan', 'app afsluiten', 'app sluiten', 'toch afspelen', 'toch bekijken',
    'bevestigen', 'akkoord', 'sluiten', 'weergeven', 'bekijken', 'begrepen', 'ik begrijp het',
    'openen', 'ja'
  ];

  function tryClick() {
    let clickedThisRound = false;
    const selectors = [
      'button[aria-label*="proceed" i]', 'button[aria-label*="understand" i]',
      'button[aria-label*="doorgaan" i]', 'button[aria-label*="begrijp" i]',
      'button[aria-label*="sluiten" i]', 'button[aria-label*="afsluiten" i]',
      'yt-button-renderer#confirm-button button', '#confirm-button button',
      'ytmusic-dialog-renderer button', 'tp-yt-paper-dialog button',
      '[role="dialog"] button', 'ytmusic-you-there-renderer button',
      'button.yt-spec-button-shape-next--filled',
      'button.yt-spec-button-shape-next--call-to-action'
    ];
    for (const sel of selectors) {
      const elms = document.querySelectorAll(sel);
      for (const el of elms) {
        if (el && el.offsetParent !== null) {
          const txt = (el.textContent || '').trim().toLowerCase();
          const aria = (el.getAttribute('aria-label') || '').toLowerCase();
          if (keywords.some(k => txt.includes(k) || aria.includes(k))) {
            el.click();
            clickedThisRound = true;
            clickedCount++;
          }
        }
      }
    }

    const allBtns = document.querySelectorAll('button, .yt-spec-button-shape-next, a.yt-spec-button-shape-next');
    for (const b of allBtns) {
      if (b && b.offsetParent !== null) {
        const txt = (b.textContent || '').trim().toLowerCase();
        if (keywords.some(k => txt.includes(k))) {
          b.click();
          clickedThisRound = true;
          clickedCount++;
        }
      }
    }
    return clickedThisRound;
  }

  // Attempt up to 3 passes to handle chained/nested confirmations (e.g. exit app -> proceed)
  for (let i = 0; i < 3; i++) {
    if (!tryClick()) break;
  }

  const v = document.querySelector('video');
  const bar = document.querySelector('ytmusic-player-bar');
  const isCasting = bar ? (bar.castConnectionState === 'CONNECTED' || bar.castConnectionState === 'CONNECTING') : false;
  if (clickedCount > 0 && v && v.paused && !isCasting) {
    v.play().catch(() => {});
  }
  return clickedCount;
})()"""


def dismiss_kiosk_dialogs() -> bool:
    """Dismiss parental advisories, content warnings, and confirmation modals over CDP."""
    _cdp_send("Page.handleJavaScriptDialog", {"accept": True}, timeout=0.2)
    reply = _cdp_send("Runtime.evaluate", {"expression": _DISMISS_DIALOGS_JS, "returnByValue": True}, timeout=0.2)
    if reply and "result" in reply:
        val = reply["result"].get("result", {}).get("value")
        return bool(val)
    return False


def try_chrome_cdp_navigate(url: str) -> bool:
    """Navigate the existing kiosk window to a URL, bypassing beforeunload prompts."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("KARAOKE_ALLOW_TEST_BROWSER"):
        return False

    _cdp_send("Runtime.evaluate", {"expression": _BYPASS_BEFOREUNLOAD_JS})
    _cdp_send("Page.handleJavaScriptDialog", {"accept": True})

    reply = _cdp_send("Page.navigate", {"url": url})
    if reply is not None:
        _cdp_send("Page.handleJavaScriptDialog", {"accept": True})
        dismiss_kiosk_dialogs()
        return True

    escaped_url = url.replace("'", "\\'")
    reply_js = _cdp_send(
        "Runtime.evaluate",
        {"expression": f"window.onbeforeunload = null; window.location.href = '{escaped_url}';"}
    )
    if reply_js is not None:
        dismiss_kiosk_dialogs()
        return True
    return False


def cdp_play_pause() -> bool:
    """Toggle play/pause on the kiosk browser over CDP."""
    js = """(() => {
      const v = document.querySelector('video');
      if (v) {
        if (v.paused) v.play().catch(() => {});
        else v.pause();
        return true;
      }
      const btn = document.querySelector('#play-pause-button, .play-pause-button, button[aria-label*="Play" i], button[aria-label*="Pause" i], button[aria-label*="Afspelen" i], button[aria-label*="Onderbreken" i]');
      if (btn) { btn.click(); return true; }
      return false;
    })()"""
    reply = _cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True}, force=True)
    return bool(reply and reply.get("result", {}).get("result", {}).get("value"))


def cdp_next_track() -> bool:
    """Trigger next track on the kiosk browser over CDP."""
    js = """(() => {
      const btn = document.querySelector('.next-button, button[aria-label*="Next" i], button[aria-label*="Volgende" i]');
      if (btn) { btn.click(); return true; }
      return false;
    })()"""
    reply = _cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True}, force=True)
    return bool(reply and reply.get("result", {}).get("result", {}).get("value"))


def cdp_previous_track() -> bool:
    """Trigger previous track on the kiosk browser over CDP."""
    js = """(() => {
      const btn = document.querySelector('.previous-button, button[aria-label*="Previous" i], button[aria-label*="Vorige" i]');
      if (btn) { btn.click(); return true; }
      return false;
    })()"""
    reply = _cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True}, force=True)
    return bool(reply and reply.get("result", {}).get("result", {}).get("value"))


def cdp_toggle_av(mode: str | None = None) -> str:
    """Toggle or set Audio (Song) vs Video mode on YouTube Music over CDP."""
    target_mode = mode or ""
    js = f"""(() => {{
      const songBtn = document.querySelector('ytmusic-av-toggle .song-button, button.song-button');
      const videoBtn = document.querySelector('ytmusic-av-toggle .video-button, button.video-button');
      if (!songBtn || !videoBtn) return "no_av_toggle";
      const target = '{target_mode}';
      if (target === "audio") {{
        songBtn.click();
        return "audio";
      }} else if (target === "video") {{
        videoBtn.click();
        return "video";
      }}
      const isSongActive = songBtn.classList.contains('selected') || songBtn.getAttribute('aria-selected') === 'true';
      if (isSongActive) {{
        videoBtn.click();
        return "video";
      }} else {{
        songBtn.click();
        return "audio";
      }}
    }})()"""
    reply = _cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True}, force=True)
    val = reply.get("result", {}).get("result", {}).get("value") if reply else None
    return str(val or "unsupported")


def cdp_toggle_shuffle() -> bool:
    """Toggle shuffle mode on YouTube Music over CDP."""
    js = """(() => {
      const btn = document.querySelector('.shuffle, button[aria-label*="shuffle" i], button[aria-label*="shuffelen" i]');
      if (btn) { btn.click(); return true; }
      return false;
    })()"""
    reply = _cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True}, force=True)
    return bool(reply and reply.get("result", {}).get("result", {}).get("value"))


def cdp_toggle_repeat() -> bool:
    """Toggle repeat mode on YouTube Music over CDP."""
    js = """(() => {
      const btn = document.querySelector('.repeat, button[aria-label*="repeat" i], button[aria-label*="herhalen" i]');
      if (btn) { btn.click(); return true; }
      return false;
    })()"""
    reply = _cdp_send("Runtime.evaluate", {"expression": js, "returnByValue": True}, force=True)
    return bool(reply and reply.get("result", {}).get("result", {}).get("value"))


# Reads the page's own <video> element. MPRIS reports a position but not
# reliably whether a track *finished*: when YouTube Music moves on to its own
# suggestion the metadata simply changes, which is indistinguishable from the
# user picking something. The video element says plainly that it ended.
_PLAYBACK_JS = """(() => {
  const v = document.querySelector('video');
  const bar = document.querySelector('ytmusic-player-bar');
  const isCasting = bar ? (bar.castConnectionState === 'CONNECTED' || bar.castConnectionState === 'CONNECTING') : false;
  if (isCasting && v && !v.muted) {
    v.muted = true;
  }
  if (!v) return JSON.stringify({present: false, casting: isCasting});
  return JSON.stringify({
    present: true,
    ended: !!v.ended,
    paused: !!v.paused,
    position: v.currentTime || 0,
    duration: (isFinite(v.duration) ? v.duration : 0) || 0,
    readyState: v.readyState || 0,
    muted: !!v.muted,
    casting: isCasting,
    url: location.href
  });
})()"""


_last_kiosk_dismiss: float = 0.0


def browser_playback(timeout: float = 0.2) -> "dict | None":
    """What the kiosk browser's video element is doing, or None.

    Returns ``present``, ``ended``, ``paused``, ``position``, ``duration``,
    ``readyState``, ``muted``, ``casting``, and ``url``.
    """
    global _last_kiosk_dismiss
    import json
    import time

    reply = _cdp_send("Runtime.evaluate",
                      {"expression": _PLAYBACK_JS, "returnByValue": True},
                      timeout=timeout)
    if not reply:
        return None
    try:
        raw = reply["result"]["result"]["value"]
        data = json.loads(raw)
        # If the player is stalled or paused on an unhandled warning, attempt dismiss.
        # Rate-limited to at most once per 5 seconds to prevent spamming CDP when paused.
        # When casting is active, do not dismiss/unpause as the remote Cast device controls playback.
        if (data.get("paused") or int(data.get("readyState") or 0) == 0) and not data.get("casting"):
            now = time.monotonic()
            if now - _last_kiosk_dismiss > 5.0:
                _last_kiosk_dismiss = now
                dismiss_kiosk_dialogs()
        return data
    except (KeyError, TypeError, ValueError):
        return None


def track_finished(state: "dict | None", *, tail: float = 1.5) -> bool:
    """Whether the browser's current track has reached its end.

    ``tail`` catches the case where playback is swapped out a moment before
    ``ended`` is set, which is what happens when the site starts its own next
    track: by the time ``ended`` would be true the element already holds the
    replacement.
    """
    if not state or not state.get("present"):
        return False
    if state.get("ended"):
        return True
    duration = float(state.get("duration") or 0.0)
    position = float(state.get("position") or 0.0)
    return duration > 0 and position >= (duration - tail)


def track_idle(state: "dict | None") -> bool:
    """Whether the element holds nothing that could ever play."""
    if not state or not state.get("present"):
        return False
    if state.get("ended"):
        return False
    try:
        ready = int(state.get("readyState") or 0)
    except (TypeError, ValueError):
        return False
    return ready == 0  # HAVE_NOTHING: no source loaded at all


def get_window_bounds() -> "dict | None":
    """Get the Chrome browser window ID and geometry/state over CDP."""
    reply = _cdp_send("Browser.getWindowForTarget", {})
    if reply and "result" in reply:
        return reply["result"]
    return None


def set_window_bounds(
    *,
    state: "str | None" = None,
    left: "int | None" = None,
    top: "int | None" = None,
    width: "int | None" = None,
    height: "int | None" = None,
) -> bool:
    """Set Chrome browser window state ('normal', 'minimized', 'maximized', 'fullscreen') or bounds."""
    info = get_window_bounds()
    if not info or "windowId" not in info:
        return False
    window_id = info["windowId"]
    bounds = {}
    if state:
        bounds["windowState"] = state
    if left is not None:
        bounds["left"] = left
    if top is not None:
        bounds["top"] = top
    if width is not None:
        bounds["width"] = width
    if height is not None:
        bounds["height"] = height

    reply = _cdp_send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
    return reply is not None


def bring_to_front() -> bool:
    """Bring the active Chrome page tab to front."""
    reply = _cdp_send("Page.bringToFront", {})
    return reply is not None


def _kiosk_mpris_names(names: "list[str]") -> "set[str]":
    """Which of ``names`` are the browser we drive over CDP.

    playerctl names Chrome's bus ``chromium.instanceNNN`` after the browser
    process id, so reading that process's cmdline tells us whether it is the
    window we are about to navigate.
    """
    kiosk = set()
    for name in names:
        _, sep, pid = name.partition(".instance")
        if not sep or not pid.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if f"--remote-debugging-port={CDP_PORT}" in cmdline:
            kiosk.add(name)
    return kiosk


def launch_kiosk_browser(url: str = "https://music.youtube.com") -> bool:
    """Launch Google Chrome in kiosk debugging mode (:9222) if down."""
    import os
    import shutil
    import time

    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("KARAOKE_ALLOW_TEST_BROWSER"):
        return False

    if _cdp_page_socket() is not None:
        return True

    # 1. Try starting the systemd kiosk unit first
    try:
        res = subprocess.run(
            ["systemctl", "--user", "start", "karaoke-kiosk.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            time.sleep(1.2)
            if _cdp_page_socket() is not None:
                return True
    except Exception:
        pass

    # 2. Manual fallback with exact profile path
    chrome = os.environ.get("CHROME", "/usr/bin/google-chrome-stable")
    if not shutil.which(chrome):
        chrome = "google-chrome-stable"
        if not shutil.which(chrome):
            chrome = "google-chrome"
            if not shutil.which(chrome):
                return False

    profile = os.path.expanduser("~/.config/google-chrome-kiosk")
    cmd = [
        chrome,
        f"--app={url}",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        return _cdp_page_socket() is not None
    except Exception:
        return False


def restart_kiosk_browser() -> bool:
    """Restart the Google Chrome kiosk window via systemd or manual fallback."""
    import os
    import time

    # 1. Try systemd first
    try:
        res = subprocess.run(
            ["systemctl", "--user", "restart", "karaoke-kiosk.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            time.sleep(1.2)
            return _cdp_page_socket() is not None
    except Exception:
        pass

    # 2. Fallback to manual kill and launch
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"remote-debugging-port={CDP_PORT}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
        for pid in (out or "").split():
            try:
                os.kill(int(pid), 9)  # SIGKILL
            except (ProcessLookupError, ValueError, PermissionError):
                pass
        time.sleep(0.5)
    except Exception:
        pass

    return launch_kiosk_browser()


def open_song_url(url: str, kind: str | None, *, artist: str = "",
                  title: str = "", prefer_audio: bool = True) -> int | None:
    """Open a song URL and return the spawned process id when applicable.

    YouTube/browser URLs are opened asynchronously so the TUI remains responsive.
    stdout/stderr are captured to log files so xdg-open failures are debuggable.

    ``prefer_audio`` deliberately opens YouTube/YT Music through a Music search
    when artist/title are known. A bare ``music.youtube.com/watch?v=...`` can
    land on the video side for official videos, which is often out of sync with
    the album audio; search lets YouTube Music resolve the canonical audio track.
    """
    # Pause any other active players first so audio does not overlap!
    try:
        proc = subprocess.run(
            ["playerctl", "--list-all"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            names = [p.strip() for p in proc.stdout.strip().splitlines() if p.strip()]
            # ...but never the kiosk window itself. On a dedicated box it is
            # the only MPRIS player there is, and pausing the very browser we
            # are about to navigate leaves it parked at 0:00 whenever the site
            # does not autoplay the new URL.
            kiosk = _kiosk_mpris_names(names)
            for p in names:
                if p in kiosk:
                    continue
                subprocess.run(
                    ["playerctl", "--player", p, "pause"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                )
    except Exception:
        pass

    # For Spotify, convert URIs to HTTPS Web Player links and fall through
    # so they play in the dedicated browser window instead of launching/relying
    # on the native Spotify desktop app!
    if kind == "spotify" or "spotify.com" in url.lower() or url.startswith("spotify:"):
        if url.startswith("spotify:track:"):
            track_id = url.split(":")[-1]
            url = f"https://open.spotify.com/track/{track_id}"
        kind = "spotify_web"

    # Automatically upgrade standard YouTube links to YouTube Music audio. When
    # the caller knows artist/title, prefer a Music search rather than a direct
    # watch URL: direct watch links can land on the video side, and those clips
    # are often out of sync with the album/audio track.
    is_youtube = kind in ("youtube", "youtube_music", "youtube_search", "youtube_music_search") or "youtube.com" in url.lower() or "youtu.be" in url.lower()
    if is_youtube:
        from urllib.parse import parse_qs, urlparse, quote_plus
        from .localcache import extract_youtube_id

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        list_id = params.get("list", [""])[0]
        index_id = params.get("index", [""])[0]

        vid = extract_youtube_id(url)
        if vid:
            # Direct watch link in YouTube Music plays immediately!
            extra = []
            if list_id:
                extra.append(f"list={list_id}")
            if index_id:
                extra.append(f"index={index_id}")
            suffix = f"&{'&'.join(extra)}" if extra else ""
            url = f"https://music.youtube.com/watch?v={vid}{suffix}"
            kind = "youtube_music"
        elif list_id:
            url = f"https://music.youtube.com/playlist?list={list_id}"
            kind = "youtube_music"
        elif prefer_audio and f"{artist} {title}".strip():
            search_query = f"{artist} {title}".strip()
            url = f"https://music.youtube.com/search?q={quote_plus(search_query)}"
            kind = "youtube_music_search"
        elif "/results?search_query=" in url:
            query = params.get("search_query", [""])[0]
            if query:
                url = f"https://music.youtube.com/search?q={quote_plus(query)}"
                kind = "youtube_music_search"

    # Try to navigate an active kiosk/debugging browser first, or launch it if down!
    if try_chrome_cdp_navigate(url):
        log.info("Navigated active kiosk-mode Chrome via CDP: %s", url)
        return None
    elif launch_kiosk_browser(url):
        if try_chrome_cdp_navigate(url):
            log.info("Launched kiosk-mode Chrome and navigated via CDP: %s", url)
            return None

    if _is_browser_url(url, kind) and not _allow_xdg_fallback():
        log.warning(
            "Kiosk/CDP playback failed for %s; refusing xdg-open fallback "
            "to avoid spawning the normal desktop browser. Set "
            "KARAOKE_ALLOW_XDG_OPEN_FALLBACK=1 to restore the old fallback.",
            url,
        )
        return None

    OPEN_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    stdout = OPEN_STDOUT_LOG.open("ab")
    stderr = OPEN_STDERR_LOG.open("ab")
    try:
        log.debug("Executing: xdg-open %s", url)
        proc = subprocess.Popen(["xdg-open", url], stdout=stdout, stderr=stderr)
        log.info(
            "xdg-open spawned pid=%s stdout=%s stderr=%s",
            proc.pid,
            OPEN_STDOUT_LOG,
            OPEN_STDERR_LOG,
        )
        return proc.pid
    finally:
        stdout.close()
        stderr.close()
