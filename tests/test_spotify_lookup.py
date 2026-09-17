"""Spotify lookup cache and the mic/player clock choice.

Both halves exist to stop the same two failures repeating:

- Spotify search is rate limited per client id, and this project lost ~24h of
  API access to calling it in a loop. Every lookup must be cached, misses
  included, or a song Spotify does not carry is re-searched forever.
- A 429 is not a miss. Recording one would cache a false negative permanently.
"""
from types import SimpleNamespace

import pytest

from karaoke import detect, localcache
from karaoke.tui import KaraokeTui


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "t.db")
    c.execute("INSERT INTO tracks (track_id, artist, title) VALUES (1, 'A', 'B')")
    c.commit()
    yield c
    c.close()


# -- the lookup cache ---------------------------------------------------

def test_a_fresh_track_is_due(conn):
    assert localcache.spotify_lookup_due(1, conn) is True


def test_no_track_is_never_due(conn):
    assert localcache.spotify_lookup_due(None, conn) is False


def test_a_hit_stops_further_lookups(conn):
    localcache.record_spotify_lookup(1, "spotify:track:xyz", conn)
    assert localcache.spotify_lookup_due(1, conn) is False


def test_a_miss_is_remembered_and_capped(conn):
    """The expensive case: Spotify has no match, so stop asking."""
    localcache.record_spotify_lookup(1, None, conn)
    assert localcache.spotify_lookup_due(1, conn) is True     # one retry allowed
    localcache.record_spotify_lookup(1, None, conn)
    assert localcache.spotify_lookup_due(1, conn) is False    # then never again


def test_a_miss_stores_null_not_empty_string(conn):
    localcache.record_spotify_lookup(1, "", conn)
    row = conn.execute("SELECT uri, attempts FROM spotify_lookups").fetchone()
    assert row["uri"] is None and row["attempts"] == 1


def test_a_later_hit_overrides_an_earlier_miss(conn):
    localcache.record_spotify_lookup(1, None, conn)
    localcache.record_spotify_lookup(1, "spotify:track:xyz", conn)
    assert localcache.spotify_lookup_due(1, conn) is False


def test_migration_adds_the_table_to_an_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE tracks (track_id INTEGER PRIMARY KEY, artist TEXT,"
                " title TEXT, album TEXT, duration REAL)")
    old.commit()
    old.close()

    c = localcache.connect(path)
    try:
        assert localcache.spotify_lookup_due(1, c) is True
    finally:
        c.close()


# -- detect.same_track --------------------------------------------------

@pytest.mark.parametrize("a1,t1,a2,t2", [
    ("The Mothers of Invention", "Peaches en Regalia",
     "Mothers of Invention", "Peaches en Regalia"),          # leading "The"
    ("Portishead", "Glory Box", "Portishead", "Glory Box (Live)"),   # suffix
    ("Portishead", "Glory Box", "portishead", "GLORY BOX"),          # case
    ("", "Glory Box", "Portishead", "Glory Box"),            # blank artist ok
])
def test_same_track_matches(a1, t1, a2, t2):
    assert detect.same_track(a1, t1, a2, t2) is True


@pytest.mark.parametrize("a1,t1,a2,t2", [
    ("Portishead", "Glory Box", "Portishead", "Sour Times"),   # other song
    ("Portishead", "Glory Box", "Tricky", "Glory Box"),        # other artist
    ("", "Glory Box", "Portishead", "Sour Times"),   # blank artist still needs title
    ("Portishead", "", "Portishead", "Glory Box"),             # no title at all
])
def test_same_track_rejects(a1, t1, a2, t2):
    assert detect.same_track(a1, t1, a2, t2) is False


# -- which clock wins ---------------------------------------------------

def _app(mic_title="Glory Box", mic_artist="Portishead"):
    app = KaraokeTui.__new__(KaraokeTui)
    app._mic_ref = SimpleNamespace(artist=mic_artist, title=mic_title,
                                   offset=0.0, offset_mono=0.0)
    app._mode_override = None
    app._clock_from_mic = False
    return app


def test_player_clock_wins_when_it_has_the_same_song(monkeypatch):
    """The point of the change: use Spotify's exact position, not reckoning."""
    app = _app()
    monkeypatch.setattr(detect, "detect_active", lambda *a: detect.Detection(
        "spotify", "spotify", "Portishead", "Glory Box"))
    det = app._effective_detection()
    assert det.mode == "spotify"
    assert app._clock_from_mic is True


def test_radio_survives_when_the_player_has_a_different_song(monkeypatch):
    """Regression guard: the mic must still win when MPRIS is stale."""
    app = _app()
    monkeypatch.setattr(detect, "detect_active", lambda *a: detect.Detection(
        "scan", "firefox", "Someone Else", "Another Song"))
    det = app._effective_detection()
    assert det.mode == "radio"
    assert det.title == "Glory Box"
    assert app._clock_from_mic is False


def test_radio_survives_when_nothing_is_playing(monkeypatch):
    app = _app()
    monkeypatch.setattr(detect, "detect_active",
                        lambda *a: detect.Detection(mode="browse"))
    assert app._effective_detection().mode == "radio"


# -- the background lookup ----------------------------------------------

def test_rate_limit_is_not_recorded_as_a_miss(tmp_path, monkeypatch):
    """The trap: caching a 429 would hide the song from Spotify forever."""
    from karaoke import spotify_client
    from karaoke.spotify_client import SpotifyRateLimited

    db = tmp_path / "t.db"
    real_connect = localcache.connect
    c = real_connect(db)
    c.execute("INSERT INTO tracks (track_id, artist, title) VALUES (1,'A','B')")
    c.commit()
    c.close()

    class FakeClient:
        def search_track(self, artist, title):
            raise SpotifyRateLimited(3600)

    opened = []

    def _tracking_connect(*a, **k):
        opened.append(1)
        return real_connect(db)

    monkeypatch.setattr(spotify_client, "SpotifyClient", FakeClient)
    monkeypatch.setattr(localcache, "connect", _tracking_connect)

    app = KaraokeTui.__new__(KaraokeTui)
    app._spotify_off = False
    app.call_from_thread = lambda fn, *a, **k: None
    app.notify = lambda *a, **k: None
    app._background_fetch_spotify(1, "A", "B")

    assert app._spotify_off is True    # stops for the rest of the session
    assert opened == []                # nothing was written at all

    c = real_connect(db)
    try:
        assert c.execute("SELECT count(*) FROM spotify_lookups").fetchone()["count"] == 0
        assert localcache.spotify_lookup_due(1, c) is True   # still askable later
    finally:
        c.close()


def test_a_genuine_miss_is_recorded(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    c = localcache.connect(db)
    c.execute("INSERT INTO tracks (track_id, artist, title) VALUES (1,'A','B')")
    c.commit()
    c.close()

    from karaoke import spotify_client

    class FakeClient:
        def search_track(self, artist, title):
            return None

    monkeypatch.setattr(spotify_client, "SpotifyClient", FakeClient)
    real_connect = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real_connect(db))

    app = KaraokeTui.__new__(KaraokeTui)
    app._spotify_off = False
    app.call_from_thread = lambda fn, *a, **k: None
    app.notify = lambda *a, **k: None
    app._background_fetch_spotify(1, "A", "B")

    c = real_connect(db)
    try:
        row = c.execute("SELECT uri, attempts FROM spotify_lookups").fetchone()
        assert row["uri"] is None and row["attempts"] == 1
        assert app._spotify_off is False      # a miss is not a failure
    finally:
        c.close()


def test_a_hit_stores_a_spotify_source(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    c = localcache.connect(db)
    c.execute("INSERT INTO tracks (track_id, artist, title) VALUES (1,'A','B')")
    c.commit()
    c.close()

    from karaoke import spotify_client

    class FakeClient:
        def search_track(self, artist, title):
            return "spotify:track:xyz"

    monkeypatch.setattr(spotify_client, "SpotifyClient", FakeClient)
    real_connect = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real_connect(db))

    app = KaraokeTui.__new__(KaraokeTui)
    app._spotify_off = False
    app.call_from_thread = lambda fn, *a, **k: None
    app.notify = lambda *a, **k: None
    app._background_fetch_spotify(1, "A", "B")

    c = real_connect(db)
    try:
        assert c.execute("SELECT count(*) FROM sources WHERE kind='spotify'"
                         ).fetchone()["count"] == 1
        assert localcache.spotify_lookup_due(1, c) is False
    finally:
        c.close()


def test_lookups_stop_once_spotify_is_off(tmp_path, monkeypatch):
    from karaoke import spotify_client

    called = []

    class FakeClient:
        def search_track(self, artist, title):
            called.append((artist, title))
            return None

    monkeypatch.setattr(spotify_client, "SpotifyClient", FakeClient)
    app = KaraokeTui.__new__(KaraokeTui)
    app._spotify_off = True
    app._background_fetch_spotify(1, "A", "B")
    assert called == []


# --- batch metadata lookup -------------------------------------------------

def test_tracks_by_id_uses_the_single_track_endpoint(monkeypatch):
    """The obvious choice, /v1/tracks?ids=, answers 403 Forbidden for this app
    while /v1/tracks/{id} answers 200 on the same token -- an app restriction,
    not an auth problem, since /me succeeds too."""
    from karaoke import spotify_client

    called = []

    class _Resp:
        status_code = 200
        headers: dict = {}

        def __init__(self, tid):
            self._tid = tid

        def json(self):
            return {"id": self._tid, "name": "Song",
                    "album": {"name": "Record"}, "duration_ms": 211586}

    def fake_get(url, headers=None, timeout=0, params=None):
        called.append(url)
        return _Resp(url.rsplit("/", 1)[-1])

    monkeypatch.setattr(spotify_client.requests, "get", fake_get)
    client = spotify_client.SpotifyClient.__new__(spotify_client.SpotifyClient)
    client._headers = lambda: {}
    out = client.tracks_by_id(["aaa", "bbb"], pause=0)

    assert all(u.endswith(("/aaa", "/bbb")) for u in called)
    assert not any("ids=" in u for u in called)
    assert out["aaa"]["album"]["name"] == "Record"


def test_tracks_by_id_skips_a_dead_id(monkeypatch):
    """A region-locked or removed track 404s; the rest must still come back."""
    from karaoke import spotify_client

    class _Resp:
        headers: dict = {}

        def __init__(self, code, tid=""):
            self.status_code = code
            self._tid = tid

        def json(self):
            return {"id": self._tid, "name": "S", "album": {"name": "A"},
                    "duration_ms": 1000}

    def fake_get(url, headers=None, timeout=0, params=None):
        tid = url.rsplit("/", 1)[-1]
        return _Resp(404) if tid == "dead" else _Resp(200, tid)

    monkeypatch.setattr(spotify_client.requests, "get", fake_get)
    client = spotify_client.SpotifyClient.__new__(spotify_client.SpotifyClient)
    client._headers = lambda: {}
    out = client.tracks_by_id(["dead", "good"], pause=0)
    assert list(out) == ["good"]


def test_tracks_by_id_raises_on_a_rate_limit(monkeypatch):
    """Never swallowed: a 429 must stop the caller, not look like a miss."""
    from karaoke import spotify_client

    class _Resp:
        status_code = 429
        headers = {"Retry-After": "3600"}

    monkeypatch.setattr(spotify_client.requests, "get",
                        lambda *a, **k: _Resp())
    client = spotify_client.SpotifyClient.__new__(spotify_client.SpotifyClient)
    client._headers = lambda: {}
    with pytest.raises(spotify_client.SpotifyRateLimited):
        client.tracks_by_id(["aaa"], pause=0)
