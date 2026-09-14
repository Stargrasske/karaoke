"""Tests for the interactive song browser helpers."""
from __future__ import annotations

from karaoke import browse


class DummyProcess:
    pid = 4242


def test_open_song_url_blocks_xdg_open_for_browser_urls(monkeypatch, tmp_path):
    calls = []

    def fake_popen(args, stdout=None, stderr=None):
        calls.append((args, stdout, stderr))
        return DummyProcess()

    monkeypatch.delenv("KARAOKE_ALLOW_XDG_OPEN_FALLBACK", raising=False)
    monkeypatch.setattr(browse.subprocess, "Popen", fake_popen)
    # Mock try_chrome_cdp_navigate to always return False to isolate test
    import karaoke.player_open
    monkeypatch.setattr(karaoke.player_open, "try_chrome_cdp_navigate", lambda *args, **kwargs: False)
    monkeypatch.setattr(karaoke.player_open, "launch_kiosk_browser", lambda *args, **kwargs: False)

    pid = browse.open_song_url("https://www.youtube.com/watch?v=bXWHf2HH8jY", "youtube")

    assert pid is None
    assert calls == []


def test_open_song_url_prefers_youtube_music_audio_search(monkeypatch, tmp_path):
    calls = []

    def fake_popen(args, stdout=None, stderr=None):
        calls.append((args, stdout, stderr))
        return DummyProcess()

    monkeypatch.setattr(browse, "OPEN_STDOUT_LOG", tmp_path / "xdg.stdout.log")
    monkeypatch.setattr(browse, "OPEN_STDERR_LOG", tmp_path / "xdg.stderr.log")
    monkeypatch.setenv("KARAOKE_ALLOW_XDG_OPEN_FALLBACK", "1")
    monkeypatch.setattr(browse.subprocess, "Popen", fake_popen)
    import karaoke.player_open
    monkeypatch.setattr(karaoke.player_open, "try_chrome_cdp_navigate", lambda *args, **kwargs: False)
    monkeypatch.setattr(karaoke.player_open, "launch_kiosk_browser", lambda *args, **kwargs: False)

    # 1. Direct watch link -> YouTube Music watch URL (plays immediately)
    browse.open_song_url(
        "https://www.youtube.com/watch?v=bXWHf2HH8jY", "youtube",
        artist="The Slits", title="I Heard It Through The Grapevine")

    assert calls[0][0] == [
        "xdg-open",
        "https://music.youtube.com/watch?v=bXWHf2HH8jY",
    ]

    # 2. No direct URL -> YouTube Music search query
    browse.open_song_url(
        "", "youtube_search",
        artist="The Slits", title="I Heard It Through The Grapevine")

    assert calls[1][0] == [
        "xdg-open",
        "https://music.youtube.com/search?q=The+Slits+I+Heard+It+Through+The+Grapevine",
    ]


def test_open_song_url_preserves_playlist_parameters(monkeypatch, tmp_path):
    calls = []

    def fake_popen(args, stdout=None, stderr=None):
        calls.append((args, stdout, stderr))
        return DummyProcess()

    monkeypatch.setattr(browse, "OPEN_STDOUT_LOG", tmp_path / "xdg.stdout.log")
    monkeypatch.setattr(browse, "OPEN_STDERR_LOG", tmp_path / "xdg.stderr.log")
    monkeypatch.setenv("KARAOKE_ALLOW_XDG_OPEN_FALLBACK", "1")
    monkeypatch.setattr(browse.subprocess, "Popen", fake_popen)
    import karaoke.player_open
    monkeypatch.setattr(karaoke.player_open, "try_chrome_cdp_navigate", lambda *args, **kwargs: False)
    monkeypatch.setattr(karaoke.player_open, "launch_kiosk_browser", lambda *args, **kwargs: False)

    browse.open_song_url(
        "https://music.youtube.com/watch?v=ABCDEFGHIJK&list=PL_TEST123&index=4", "youtube_music",
    )

    assert calls[0][0] == [
        "xdg-open",
        "https://music.youtube.com/watch?v=ABCDEFGHIJK&list=PL_TEST123&index=4",
    ]



def test_open_song_url_uses_playerctl_for_spotify(monkeypatch, tmp_path):
    calls = []

    def fake_popen(args, stdout=None, stderr=None):
        calls.append((args, stdout, stderr))
        return DummyProcess()

    monkeypatch.setattr(browse, "OPEN_STDOUT_LOG", tmp_path / "xdg.stdout.log")
    monkeypatch.setattr(browse, "OPEN_STDERR_LOG", tmp_path / "xdg.stderr.log")
    monkeypatch.setenv("KARAOKE_ALLOW_XDG_OPEN_FALLBACK", "1")
    monkeypatch.setattr(browse.subprocess, "Popen", fake_popen)
    import karaoke.player_open
    monkeypatch.setattr(karaoke.player_open, "try_chrome_cdp_navigate", lambda *args, **kwargs: False)
    monkeypatch.setattr(karaoke.player_open, "launch_kiosk_browser", lambda *args, **kwargs: False)

    pid = browse.open_song_url("spotify:track:123", "spotify")

    assert pid == 4242
    assert calls[0][0] == ["xdg-open", "https://open.spotify.com/track/123"]



def test_load_songs_prefers_browser_openable_source(tmp_path, monkeypatch):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    db = tmp_path / "k.db"
    _real_connect = localcache.connect
    conn = _real_connect(db)
    # Track with BOTH a spotify and a youtube source. Browse must pick the
    # youtube (browser-openable) one so Enter opens the page, not Spotify.
    localcache.add_track_and_lyrics(
        "Kiki Rockwell", "Cup Runneth Over", Lyrics(),
        url="https://open.spotify.com/track/abc", kind="spotify", conn=conn,
    )
    localcache.add_track_source(
        "Kiki Rockwell", "Cup Runneth Over",
        url="https://www.youtube.com/watch?v=xNx0", kind="youtube", conn=conn,
    )
    conn.close()

    monkeypatch.setattr(browse.localcache, "connect", lambda *a, **k: _real_connect(db))

    app = browse.KaraokeBrowser()
    # Force the SQLite fallback path: this test exercises the source-preference
    # SQL, not the HTTP client (which may hit a live server on the dev box).
    monkeypatch.setattr(app.api, "list_tracks", lambda *a, **k: [])

    class FakeTable:
        def add_row(self, *args):
            pass
        def clear(self):
            pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable())
    app.load_songs()

    assert len(app._song_data) == 1
    assert app._song_data[0]["kind"] == "youtube"
    assert "youtube.com" in app._song_data[0]["url"]


def test_browse_mood_slider_filtering(monkeypatch):
    app = browse.KaraokeBrowser()
    fake_tracks = [
        {"artist": "Mellow", "title": "Song A", "energy": 0.2, "key": "C major", "bpm": 80},
        {"artist": "Mid", "title": "Song B", "energy": 0.5, "key": "G major", "bpm": 120},
        {"artist": "Heavy", "title": "Song C", "energy": 0.85, "key": "A minor", "bpm": 160},
    ]
    monkeypatch.setattr(app.api, "list_tracks", lambda *a, **k: fake_tracks)

    class FakeTable:
        def add_row(self, *args): pass
        def clear(self): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable())
    app.load_songs()
    assert len(app._visible_songs) == 3

    # Increase energy threshold to 60%
    app.action_increase_mood()
    app.action_increase_mood()
    app.action_increase_mood()
    app.action_increase_mood()
    app.action_increase_mood()
    app.action_increase_mood()
    assert app._mood_level == 0.6
    assert len(app._visible_songs) == 1
    assert app._visible_songs[0]["artist"] == "Heavy"


def test_browse_mood_presets(monkeypatch):
    app = browse.KaraokeBrowser()
    fake_tracks = [
        {"artist": "Mellow", "title": "Song A", "energy": 0.2, "key": "C major", "bpm": 80},
        {"artist": "Mid", "title": "Song B", "energy": 0.5, "key": "G major", "bpm": 120},
        {"artist": "Heavy", "title": "Song C", "energy": 0.85, "key": "A minor", "bpm": 160},
    ]
    monkeypatch.setattr(app.api, "list_tracks", lambda *a, **k: fake_tracks)

    class FakeTable:
        def add_row(self, *args): pass
        def clear(self): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable())
    app.load_songs()

    # Cycle to Chill / Mellow (0% - 40%)
    app.action_cycle_mood_mode()
    assert len(app._visible_songs) == 1
    assert app._visible_songs[0]["artist"] == "Mellow"

    # Cycle to Groovy / Upbeat (40% - 75%)
    app.action_cycle_mood_mode()
    assert len(app._visible_songs) == 1
    assert app._visible_songs[0]["artist"] == "Mid"

    # Cycle to High Energy / Anthem (75% - 100%)
    app.action_cycle_mood_mode()
    assert len(app._visible_songs) == 1
    assert app._visible_songs[0]["artist"] == "Heavy"

