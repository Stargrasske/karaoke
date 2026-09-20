"""The recordings browser, and what it refuses to do.

Playback deliberately reuses the browser window that is already open for
YouTube Music and Spotify: it publishes MPRIS, which is where the app already
reads position from, so a recorded track syncs its lyrics through the same path
a stream does. Introducing a standalone player would have meant a second clock
and a second set of sync offsets — the source of a 13-second bug earlier.
"""
from __future__ import annotations

import pytest

from karaoke.tui import RecordingBrowseScreen


SESSIONS = [
    {"recording_id": 13, "started_at": 1788616837.0, "status": "recording",
     "identified": 77, "audio_bytes": 3_000_000_000},
    {"recording_id": 12, "started_at": 1788613200.0, "status": "analysed",
     "identified": 21, "audio_bytes": 117_000_000},
    {"recording_id": 11, "started_at": 1788609750.0, "status": "complete",
     "identified": 4, "audio_bytes": 0},
]


def _screen(rows=None) -> RecordingBrowseScreen:
    screen = RecordingBrowseScreen(SESSIONS)
    if rows is not None:
        screen._recording_id = 12
        screen._rows = rows
    return screen


def _row(index=0, playable=True, silent=False, confident=True, title="Bambee!"):
    return {"index": index, "artist": "Wizards of Ooze", "title": title,
            "start_wall": 1788613214.0, "duration_s": 270.0,
            "confident": confident, "spread_s": 0.1,
            "playable": playable, "silent": silent}


class _FakeOKResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_ok_response(url, timeout=0):
    return _FakeOKResponse()


# --- navigation ------------------------------------------------------------

def test_escape_at_the_top_level_closes(monkeypatch):
    screen = _screen()
    closed = []
    monkeypatch.setattr(type(screen), "dismiss",
                        lambda self, result=None: closed.append(True))
    screen.action_back()
    assert closed == [True]


def test_escape_inside_a_session_goes_back_rather_than_closing(monkeypatch):
    """Backing out of a track list should not lose the session list."""
    screen = _screen(rows=[_row()])
    closed = []
    monkeypatch.setattr(type(screen), "dismiss",
                        lambda self, result=None: closed.append(True))
    monkeypatch.setattr(type(screen), "_show_sessions",
                        lambda self: setattr(self, "_recording_id", None))
    screen.action_back()
    assert closed == []
    assert screen._recording_id is None


# --- playing ---------------------------------------------------------------

def test_selecting_a_track_opens_it_in_the_existing_window(monkeypatch):
    """The whole design: hand a URL to the browser that is already playing."""
    opened = []
    monkeypatch.setattr("karaoke.player_open.open_song_url",
                        lambda url, kind: opened.append((url, kind)))
    monkeypatch.setattr("urllib.request.urlopen", _fake_ok_response)
    screen = _screen(rows=[_row(index=2)])
    monkeypatch.setattr(type(screen), "dismiss", lambda self, result=None: None)
    monkeypatch.setattr(type(screen), "app",
                        property(lambda self: _FakeApp()))

    screen._play(2)
    assert len(opened) == 1
    url, kind = opened[0]
    assert url.endswith("/recordings/12/tracks/2")
    assert kind == "recording"


def test_a_pruned_track_is_not_played_and_says_why(monkeypatch):
    """Retention deletes audio after a week; the marks outlive it."""
    opened = []
    monkeypatch.setattr("karaoke.player_open.open_song_url",
                        lambda url, kind: opened.append(url))
    app = _FakeApp()
    screen = _screen(rows=[_row(index=0, playable=False)])
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))

    screen._play(0)
    assert opened == []
    assert app.notices and "pruned" in app.notices[0][0]


def test_a_silent_track_is_still_playable(monkeypatch):
    """Marked, not blocked: hearing the silence is how it gets confirmed."""
    opened = []
    monkeypatch.setattr("karaoke.player_open.open_song_url",
                        lambda url, kind: opened.append(url))
    monkeypatch.setattr("urllib.request.urlopen", _fake_ok_response)
    screen = _screen(rows=[_row(index=1, silent=True)])
    monkeypatch.setattr(type(screen), "dismiss", lambda self, result=None: None)
    monkeypatch.setattr(type(screen), "app",
                        property(lambda self: _FakeApp()))

    screen._play(1)
    assert len(opened) == 1


def test_an_unknown_index_does_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr("karaoke.player_open.open_song_url",
                        lambda url, kind: opened.append(url))
    screen = _screen(rows=[_row(index=0)])
    screen._play(99)
    assert opened == []


def test_a_dead_playback_window_is_reported_not_swallowed(monkeypatch):
    """Silence here would look identical to a track that plays inaudibly."""
    def _boom(url, kind):
        raise OSError("no CDP")

    monkeypatch.setattr("karaoke.player_open.open_song_url", _boom)
    app = _FakeApp()
    screen = _screen(rows=[_row(index=0)])
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))

    screen._play(0)
    assert app.notices and app.notices[0][1] == "error"


def test_nothing_plays_before_a_session_is_chosen(monkeypatch):
    opened = []
    monkeypatch.setattr("karaoke.player_open.open_song_url",
                        lambda url, kind: opened.append(url))
    screen = _screen()
    screen._rows = [_row(index=0)]      # rows without a chosen recording
    screen._play(0)
    assert opened == []


class _FakeApp:
    def __init__(self) -> None:
        self.notices: list[tuple[str, str]] = []

    def notify(self, message, severity="information", **kwargs) -> None:
        # **kwargs so a real Textual notify signature (timeout=, title=)
        # does not make the double diverge from the thing it stands in for.
        self.notices.append((message, severity))


# --- refusing to navigate to an error page --------------------------------

def test_a_missing_route_is_named_as_stale_code(monkeypatch):
    """The failure that actually happened: ctrl_api predating the route.

    It answered 404, the browser showed "Not Found", and that is
    indistinguishable from the recording being gone.
    """
    import urllib.error

    from karaoke import tui

    def _404(url, timeout=0):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", _404)
    problem = tui._playback_endpoint_problem("http://localhost:8765/x")
    assert problem and "older code" in problem
    assert "ctrl_api" in problem


def test_an_unreachable_api_says_so(monkeypatch):
    import urllib.error

    from karaoke import tui

    def _down(url, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _down)
    problem = tui._playback_endpoint_problem("http://localhost:8765/x")
    assert problem and "not reachable" in problem


def test_a_working_endpoint_reports_no_problem(monkeypatch):
    from karaoke import tui

    class _OK:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: _OK())
    assert tui._playback_endpoint_problem("http://localhost:8765/x") is None


def test_nothing_is_opened_when_the_endpoint_is_broken(monkeypatch):
    """A blank error page replaces what is playing; refuse instead."""
    import urllib.error

    from karaoke import tui

    opened = []
    monkeypatch.setattr("karaoke.player_open.open_song_url",
                        lambda url, kind: opened.append(url))
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=0: (_ for _ in ()).throw(
                            urllib.error.HTTPError(url, 404, "nf", {}, None)))
    app = _FakeApp()
    screen = _screen(rows=[_row(index=0)])
    monkeypatch.setattr(type(screen), "app", property(lambda self: app))

    screen._play(0)
    assert opened == []
    assert app.notices and app.notices[0][1] == "error"
