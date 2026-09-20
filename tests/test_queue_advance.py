"""The play queue advances itself, so a search result actually plays through.

The first version only played the first entry: nothing advanced the queue but
the `>` key. Worse, the site fills the gap itself -- opening a YouTube Music
watch URL makes it attach an RD… radio station, so "next" is its suggestion
rather than the list you built.
"""
import pytest

from karaoke import player_open


# -- knowing a track ended ----------------------------------------------

def test_a_finished_video_is_detected():
    assert player_open.track_finished(
        {"present": True, "ended": True, "position": 10.0, "duration": 200.0})


def test_the_tail_counts_as_finished():
    """The element is swapped a moment before `ended` is set: by the time it
    would be true it already holds the site's replacement track."""
    assert player_open.track_finished(
        {"present": True, "ended": False, "position": 199.0, "duration": 200.0})


def test_mid_track_is_not_finished():
    assert not player_open.track_finished(
        {"present": True, "ended": False, "position": 100.0, "duration": 200.0})


def test_a_paused_track_is_not_finished():
    assert not player_open.track_finished(
        {"present": True, "ended": False, "paused": True,
         "position": 5.0, "duration": 200.0})


def test_an_unknown_duration_is_not_finished():
    """A live stream reports no duration; it must not look like it ended."""
    assert not player_open.track_finished(
        {"present": True, "ended": False, "position": 900.0, "duration": 0})


def test_no_video_and_no_state_are_safe():
    assert not player_open.track_finished({"present": False})
    assert not player_open.track_finished(None)


# -- knowing the player holds nothing -----------------------------------

def test_an_empty_element_is_idle():
    """Navigating off the watch URL leaves a <video> with no source. It never
    ends, so the queue must recognise it some other way."""
    assert player_open.track_idle(
        {"present": True, "ended": False, "paused": True,
         "position": 0, "duration": 0, "readyState": 0})


def test_a_playing_track_is_not_idle():
    assert not player_open.track_idle(
        {"present": True, "ended": False, "position": 100.0,
         "duration": 200.0, "readyState": 4})


def test_a_live_stream_is_not_idle():
    """It reports no duration either, but it has data -- readyState tells
    it apart from an empty element."""
    assert not player_open.track_idle(
        {"present": True, "ended": False, "position": 900.0,
         "duration": 0, "readyState": 4})


def test_a_finished_track_is_not_idle():
    """The end of a track is the queue's business, not the stall path's."""
    assert not player_open.track_idle(
        {"present": True, "ended": True, "position": 200.0,
         "duration": 200.0, "readyState": 0})


def test_no_video_and_no_state_are_not_idle():
    assert not player_open.track_idle({"present": False})
    assert not player_open.track_idle(None)


# -- the queue ----------------------------------------------------------

def _app(queue, at=0, play_once=True):
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._queue = queue
    app._queue_at = at
    app._play_once = play_once
    app._last_finished_url = ""
    app._idle_since = 0.0
    app.notify = lambda *a, **k: None
    app._render_queue = lambda: None
    return app


def _rows(*specs):
    return [{"artist": f"A{i}", "title": f"T{i}", "url": u}
            for i, u in enumerate(specs)]


def test_a_finished_track_advances_the_queue(monkeypatch):
    app = _app(_rows("https://youtu.be/1", "https://youtu.be/2"))
    played = []
    monkeypatch.setattr(app, "play_queue_index",
                        lambda i: played.append(i) or True, raising=False)
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: {"present": True, "ended": True, "url": "u1",
                                 "position": 1, "duration": 2})
    app._watch_queue()
    assert played == [1]


def test_it_only_advances_once_per_track(monkeypatch):
    """A stalled read would otherwise skip through the whole queue."""
    app = _app(_rows("https://youtu.be/1", "https://youtu.be/2", "https://youtu.be/3"))
    played = []
    monkeypatch.setattr(app, "play_queue_index",
                        lambda i: played.append(i) or True, raising=False)
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: {"present": True, "ended": True, "url": "same",
                                 "position": 1, "duration": 2})
    app._watch_queue()
    app._watch_queue()
    app._watch_queue()
    assert played == [1]


def test_play_once_off_leaves_the_player_alone(monkeypatch):
    """Then the site's own autoplay is the point, and taking over would fight it."""
    app = _app(_rows("https://youtu.be/1", "https://youtu.be/2"), play_once=False)
    monkeypatch.setattr(app, "play_queue_index",
                        lambda i: pytest.fail("must not take over"), raising=False)
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: {"present": True, "ended": True, "url": "u",
                                 "position": 1, "duration": 2})
    app._watch_queue()


def test_an_empty_queue_is_not_watched(monkeypatch):
    app = _app([])
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: pytest.fail("must not poll CDP with no queue"))
    app._watch_queue()


def test_nothing_happens_before_the_first_track_plays(monkeypatch):
    app = _app(_rows("https://youtu.be/1"), at=-1)
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: pytest.fail("must not poll CDP before playing"))
    app._watch_queue()


def test_advancing_skips_entries_with_no_source(monkeypatch):
    """A row that cannot be played must not stall the whole queue."""
    app = _app(_rows("https://youtu.be/1", "", "https://youtu.be/3"))
    attempted = []

    def fake_play(i):
        attempted.append(i)
        return bool(app._queue[i]["url"])

    monkeypatch.setattr(app, "play_queue_index", fake_play, raising=False)
    app.action_queue_next()
    assert attempted == [1, 2]


def test_the_end_of_the_queue_stops(monkeypatch):
    app = _app(_rows("https://youtu.be/1"), at=0)
    notes = []
    app.notify = lambda msg, **k: notes.append(msg)
    monkeypatch.setattr(app, "play_queue_index",
                        lambda i: pytest.fail("nothing left"), raising=False)
    app.action_queue_next()
    assert "End of queue" in notes[0]


# -- a player that came to rest holding nothing -------------------------

def _idle_app(monkeypatch, clock):
    """A queue watched against a player showing an empty <video>."""
    app = _app(_rows("https://youtu.be/1", "https://youtu.be/2"))
    played = []
    monkeypatch.setattr(app, "play_queue_index",
                        lambda i: played.append(i) or True, raising=False)
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: {"present": True, "ended": False, "paused": True,
                                 "position": 0, "duration": 0, "readyState": 0,
                                 "url": "https://music.youtube.com/@someone"})
    monkeypatch.setattr("karaoke.tui.time.monotonic", lambda: clock[0])
    return app, played


def test_an_idle_player_does_not_advance_immediately(monkeypatch):
    """A watch URL reads as idle while it loads; skipping then would race
    every track in the queue."""
    from karaoke import tui

    clock = [1000.0]
    app, played = _idle_app(monkeypatch, clock)
    app._watch_queue()
    clock[0] += tui.IDLE_STALL_S - 1
    app._watch_queue()
    assert played == []


def test_a_player_idle_for_long_enough_advances(monkeypatch):
    """Otherwise the queue waits for an end that cannot arrive: an empty
    element has no duration and never sets `ended`."""
    from karaoke import tui

    clock = [1000.0]
    app, played = _idle_app(monkeypatch, clock)
    app._watch_queue()
    clock[0] += tui.IDLE_STALL_S + 1
    app._watch_queue()
    assert played == [1]


def test_playback_resuming_clears_the_stall(monkeypatch):
    """The load simply took a while -- that is not a stall."""
    from karaoke import tui

    clock = [1000.0]
    app, played = _idle_app(monkeypatch, clock)
    app._watch_queue()
    monkeypatch.setattr("karaoke.tui.browser_playback",
                        lambda: {"present": True, "ended": False, "position": 3.0,
                                 "duration": 200.0, "readyState": 4, "url": "u"})
    clock[0] += tui.IDLE_STALL_S + 1
    app._watch_queue()
    assert played == []
    assert app._idle_since == 0.0


# -- pausing other players, but not our own window ----------------------

def test_the_kiosk_window_is_not_paused(monkeypatch):
    """It is the only MPRIS player on a dedicated box, and pausing the very
    browser we are about to navigate parks it at 0:00."""
    cmdlines = {
        "/proc/4242/cmdline":
            f"chrome\0--kiosk\0--remote-debugging-port={player_open.CDP_PORT}\0",
        "/proc/77/cmdline": "vlc\0",
    }
    monkeypatch.setattr(player_open.Path, "read_bytes",
                            lambda self: cmdlines[str(self).replace("\\", "/")].encode())
    assert player_open._kiosk_mpris_names(
        ["chromium.instance4242", "vlc.instance77"]) == {"chromium.instance4242"}


def test_players_with_no_readable_process_are_left_alone(monkeypatch):
    """An unparseable or vanished player is not ours, so it stays pausable."""
    def boom(self):
        raise OSError("gone")

    monkeypatch.setattr(player_open.Path, "read_bytes", boom)
    assert player_open._kiosk_mpris_names(["chromium.instance1", "spotify"]) == set()


def test_play_once_is_on_by_default():
    """The queue you built should be what plays, without asking."""
    from karaoke.tui import KaraokeTui
    import inspect
    src = inspect.getsource(KaraokeTui.__init__)
    assert "_play_once = True" in src
