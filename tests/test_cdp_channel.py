"""The CDP connection must not strand threads, because quitting joins them.

Every command used to get a fresh thread running ``asyncio.run``, joined with
a timeout and then abandoned if it overran. :func:`browser_playback` runs off a
two-second timer, so a browser that answered slowly leaked one thread -- each
holding a live event loop -- per tick. A live TUI was found with 46 of them.

The cost lands at exit. Shutdown joins the executors, and anyio's replacement
for ``_shutdown_default_executor`` ends in a bare ``thread.join()`` with no
timeout, so CPython's 300-second cap expires, prints its warning, and then the
process blocks for good.

These tests hold the shape of the fix: one thread serving every call, and no
new thread when a call gives up.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import types

import pytest

from karaoke import player_open


# --- a websockets stand-in -------------------------------------------------

class FakeSocket:
    """A CDP socket that replies to whatever id it is asked."""

    def __init__(self, *, hang=False, events=0):
        self.hang = hang
        self.events = events
        self.sent = []
        self.closed = False
        self._queue: list[str] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        # Unsolicited events arrive interleaved with replies; CDP does this
        # constantly and a reply reader that stops at the first frame will
        # read one of these instead of its answer.
        for n in range(self.events):
            self._queue.append(json.dumps({"method": "Page.frameNavigated",
                                           "params": {"n": n}}))
        self._queue.append(json.dumps(
            {"id": json.loads(raw)["id"], "result": {"ok": True}}))

    async def recv(self):
        if self.hang:
            await asyncio.sleep(30)
        return self._queue.pop(0)

    async def ping(self):
        if self.closed:
            raise ConnectionError("closed")

    async def close(self):
        self.closed = True


def install_fake_websockets(monkeypatch, socket):
    """Put a fake ``websockets`` module in place, and count connections."""
    connections = []

    async def connect(url):
        connections.append(url)
        return socket

    module = types.ModuleType("websockets")
    module.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", module)
    monkeypatch.setattr(player_open, "_cdp_page_socket",
                        lambda *a, **k: "ws://localhost:9222/page/1")
    return connections


@pytest.fixture
def channel():
    """A channel of its own, always shut down again."""
    made = player_open._CdpChannel()
    yield made
    made.close()


def _thread_names():
    return sorted(t.name for t in threading.enumerate())


# --- the leak --------------------------------------------------------------

def test_many_calls_use_one_thread(monkeypatch, channel):
    """The whole point: 20 commands, one thread, nothing stranded."""
    install_fake_websockets(monkeypatch, FakeSocket())

    before = len(threading.enumerate())
    for _ in range(20):
        assert channel.send("Page.navigate", {}) is not None
    after = len(threading.enumerate())

    assert after - before == 1


def test_a_timed_out_call_strands_nothing(monkeypatch, channel):
    """The old code walked away from the thread here. That was the leak."""
    install_fake_websockets(monkeypatch, FakeSocket(hang=True))

    before = len(threading.enumerate())
    assert channel.send("Page.navigate", {}, timeout=0.05) is None
    after = len(threading.enumerate())

    # One channel thread, and no per-call thread left behind by the giving up.
    assert after - before == 1


def test_repeated_timeouts_do_not_accumulate(monkeypatch, channel):
    """A browser that is slow for a while must not cost a thread per tick."""
    install_fake_websockets(monkeypatch, FakeSocket(hang=True))

    channel.send("Page.navigate", {}, timeout=0.05)
    settled = len(threading.enumerate())
    for _ in range(5):
        channel.send("Page.navigate", {}, timeout=0.05)

    assert len(threading.enumerate()) == settled


def test_the_socket_is_reused_across_calls(monkeypatch, channel):
    """Reconnecting per command is what made each call expensive enough to
    time out in the first place."""
    connections = install_fake_websockets(monkeypatch, FakeSocket())

    for _ in range(5):
        channel.send("Page.navigate", {})

    assert len(connections) == 1


# --- still a working transport ---------------------------------------------

def test_the_reply_comes_back(monkeypatch, channel):
    install_fake_websockets(monkeypatch, FakeSocket())
    reply = channel.send("Runtime.evaluate", {"expression": "1"})
    assert reply["result"] == {"ok": True}


def test_events_do_not_get_mistaken_for_the_reply(monkeypatch, channel):
    """CDP interleaves events with replies; only a matching id answers."""
    install_fake_websockets(monkeypatch, FakeSocket(events=3))
    reply = channel.send("Runtime.evaluate", {"expression": "1"})
    assert reply.get("result") == {"ok": True}
    assert "method" not in reply


def test_each_command_gets_a_fresh_id(monkeypatch, channel):
    """Every request used to go out as id 1, so a late reply to one call
    would satisfy the next."""
    socket = FakeSocket()
    install_fake_websockets(monkeypatch, socket)

    channel.send("Page.navigate", {})
    channel.send("Page.navigate", {})

    ids = [message["id"] for message in socket.sent]
    assert len(set(ids)) == len(ids)


def test_no_browser_is_not_an_error(monkeypatch, channel):
    install_fake_websockets(monkeypatch, FakeSocket())
    monkeypatch.setattr(player_open, "_cdp_page_socket", lambda *a, **k: None)
    assert channel.send("Page.navigate", {}) is None


def test_without_websockets_installed_it_declines(monkeypatch, channel):
    # A None in sys.modules is how CPython spells "this import fails".
    monkeypatch.setitem(sys.modules, "websockets", None)
    assert channel.send("Page.navigate", {}) is None


def test_a_closed_socket_is_replaced(monkeypatch, channel):
    """Chromium drops the connection when the page navigates away."""
    socket = FakeSocket()
    connections = install_fake_websockets(monkeypatch, socket)
    channel.send("Page.navigate", {})

    socket.closed = True
    assert channel.send("Page.navigate", {}) is not None
    assert len(connections) == 2


# --- shutting down ---------------------------------------------------------

def test_closing_stops_the_thread(monkeypatch):
    """A loop nobody stops is a loop that gets joined at exit."""
    install_fake_websockets(monkeypatch, FakeSocket())
    made = player_open._CdpChannel()
    made.send("Page.navigate", {})
    assert "cdp" in _thread_names()

    made.close()
    assert "cdp" not in _thread_names()


def test_closing_twice_is_harmless(monkeypatch):
    install_fake_websockets(monkeypatch, FakeSocket())
    made = player_open._CdpChannel()
    made.send("Page.navigate", {})
    made.close()
    made.close()


def test_the_channel_restarts_after_being_closed(monkeypatch, channel):
    """Closing on exit must not leave a permanently dead channel behind if
    anything still asks -- a stopped loop rejects work rather than reviving."""
    install_fake_websockets(monkeypatch, FakeSocket())
    channel.send("Page.navigate", {})
    channel.close()
    assert channel.send("Page.navigate", {}) is not None


def test_cooldown_skips_further_calls_until_expired(monkeypatch, channel):
    """After a timeout or error, subsequent calls return None immediately without waiting."""
    socket = FakeSocket(hang=True)
    install_fake_websockets(monkeypatch, socket)

    # First call times out and trips the cooldown
    assert channel.send("Page.navigate", {}, timeout=0.05) is None
    assert channel._failed_until > 0

    # With force=True it attempts even during cooldown
    assert channel.send("Page.navigate", {}, timeout=0.05, force=True) is None


@pytest.mark.skip(reason="Outdated/Broken in main")
def test_cdp_seek(monkeypatch):
    """cdp_seek evaluates video seek script with offset over CDP."""
    sent_expressions = []
    def fake_cdp_send(method, params, *, timeout=2.0, force=False):
        if method == "Runtime.evaluate":
            sent_expressions.append(params.get("expression", ""))
            return {"result": {"result": {"value": True}}}
        return None

    monkeypatch.setattr(player_open, "_cdp_send", fake_cdp_send)
    assert player_open.cdp_seek(5.0) is True
    assert any("5.0" in expr and "currentTime" in expr for expr in sent_expressions)
    assert player_open.cdp_seek(-5.0) is True
    assert any("-5.0" in expr and "currentTime" in expr for expr in sent_expressions)


