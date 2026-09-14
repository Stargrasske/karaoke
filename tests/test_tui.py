"""Tests for the expanded Textual TUI helper functions."""

from karaoke.tui import (
    _default_sync_offset,
    caption_is_synced,
    first_nonempty_line,
    lyric_preview,
    mood_for_preview,
)


def test_lyric_preview_prefers_synced_lyrics():
    song = {
        "synced_lyrics": "[00:01.00] I love this\n[00:02.00] ignored later",
        "plain_lyrics": "plain fallback",
    }

    assert lyric_preview(song, max_lines=1) == "[00:01.00] I love this"


def test_lyric_preview_falls_back_to_plain_lyrics():
    song = {"synced_lyrics": "", "plain_lyrics": "\nhello\nworld\n"}

    assert lyric_preview(song) == "hello\nworld"


def test_mood_for_preview_uses_first_non_neutral_line():
    assert mood_for_preview("la la la\nI love you") == "tender"


def test_first_nonempty_line():
    assert first_nonempty_line("\n  \n  seed text  \nnext") == "seed text"


def test_caption_is_synced_gates_autoload():
    assert caption_is_synced("youtube_caption_manual_en-US_enhanced")
    assert caption_is_synced("youtube_caption_automatic_en_synced")
    assert not caption_is_synced("youtube_caption_manual_en_plain")
    assert not caption_is_synced("")


def test_default_sync_offset_from_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET", "0.7")
    assert _default_sync_offset() == 0.7


def test_default_sync_offset_fallback_on_bad_value(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET", "not-a-number")
    assert _default_sync_offset() == 0.0


def test_default_sync_offset_default(monkeypatch):
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset() == 0.0


def test_default_sync_offset_spotify_is_zero(monkeypatch):
    # Spotify reports an accurate native position, so no browser-lag offset.
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET_SPOTIFY", raising=False)
    assert _default_sync_offset("spotify") == 0.0


def test_default_sync_offset_spotify_env_override(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET_SPOTIFY", "0.3")
    assert _default_sync_offset("spotify") == 0.3


def test_default_sync_offset_scan_unaffected_by_spotify_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET_SPOTIFY", "0.3")
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset("scan") == 0.0


def test_sync_offset_get_set_roundtrip(tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    c = localcache.connect(tmp_path / "karaoke.db")
    localcache.add_track_and_lyrics("A", "B", Lyrics(plain="x", source="lrclib"), conn=c)
    tid = localcache.find_track_id("A", "B", c)
    assert tid is not None

    # No offset saved yet.
    assert localcache.get_sync_offset(tid, c) is None

    localcache.set_sync_offset(tid, 1.4, c)
    assert localcache.get_sync_offset(tid, c) == 1.4

    # Upsert replaces, does not duplicate.
    localcache.set_sync_offset(tid, -0.5, c)
    assert localcache.get_sync_offset(tid, c) == -0.5
    assert c.execute("SELECT count(*) FROM track_sync_offsets").fetchone()[0] == 1


def test_sync_offset_none_track_is_safe(tmp_path):
    from karaoke import localcache

    c = localcache.connect(tmp_path / "karaoke.db")
    assert localcache.get_sync_offset(None, c) is None


def test_mood_square_glyph_rows_are_uniform_width():
    """All five squares must be the same visual width.

    "☔" and "🔥" are 2 cells while "☀ ♡ ◇" are 1, so the unpadded squares
    rendered lopsided under `content-align: center`.
    """
    from karaoke import visuals
    from karaoke.tui import MOOD_GLYPHS

    for mood, art in MOOD_GLYPHS.items():
        rows = art.splitlines()
        assert len(rows) == 3, mood
        assert {visuals.cell_width(r) for r in rows} == {2}, mood


# --- help screen / bindings ------------------------------------------------

def test_binding_rows_are_generated_from_bindings():
    from karaoke.tui import KaraokeTui, binding_rows

    rows = dict(binding_rows(KaraokeTui.BINDINGS))
    assert rows["H"] == "Browse"
    assert rows["?"] == "Keys"
    assert rows[","] == "Lyrics -0.1s"
    assert rows["["] == "Seek -5s"


def test_binding_rows_omit_hidden_bindings():
    """escape is bound but show=False, so it stays out of the footer and help."""
    from karaoke.tui import KaraokeTui, binding_rows

    assert "Close browse" not in dict(binding_rows(KaraokeTui.BINDINGS)).values()


def test_every_binding_has_a_matching_action():
    """Catches a typo'd action name, which would otherwise fail only at runtime."""
    from textual.binding import Binding
    from karaoke.tui import KaraokeTui

    for binding in Binding.make_bindings(KaraokeTui.BINDINGS):
        action = binding.action.split("(")[0]
        assert hasattr(KaraokeTui, f"action_{action}") or hasattr(
            KaraokeTui, action), action


def test_binding_keys_are_unique():
    from textual.binding import Binding
    from karaoke.tui import KaraokeTui

    keys = [b.key for b in Binding.make_bindings(KaraokeTui.BINDINGS)]
    assert len(keys) == len(set(keys)), f"duplicate key: {keys}"


def test_help_table_has_a_row_per_binding():
    from karaoke.tui import KaraokeTui, binding_rows, help_table

    rows = binding_rows(KaraokeTui.BINDINGS)
    assert help_table(rows).row_count == len(rows)


def test_confirm_screen_can_be_escaped():
    """escape must resolve to an explicit False, not a bare dismiss()."""
    from textual.binding import Binding
    from karaoke.tui import ConfirmScreen

    keys = {b.key: b.action for b in Binding.make_bindings(ConfirmScreen.BINDINGS)}
    assert keys.get("escape") == "cancel"
    assert hasattr(ConfirmScreen, "action_cancel")


# --- browse overlay --------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.classes = set()
        self.calls = []

    def add_class(self, name):
        self.calls.append(("add_class", name))
        self.classes.add(name)

    def remove_class(self, name):
        self.calls.append(("remove_class", name))
        self.classes.discard(name)

    def has_class(self, name):
        return name in self.classes


class _FakeTable:
    def __init__(self, log):
        self._log = log

    def focus(self):
        self._log.append(("focus", "library"))


def _app_with_fakes(monkeypatch, *, open_=False):
    """A KaraokeTui whose query_one returns fakes, so no event loop is needed."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)          # skip App.__init__
    overlay = _FakeOverlay()
    if open_:
        overlay.classes.add(KaraokeTui._BROWSE_OPEN)
    calls = overlay.calls
    table = _FakeTable(calls)

    def fake_query_one(selector, *args, **kwargs):
        if "browse-overlay" in selector:
            return overlay
        if "library" in selector:
            return table
        raise AssertionError(f"unexpected query_one({selector!r})")

    monkeypatch.setattr(app, "query_one", fake_query_one, raising=False)
    monkeypatch.setattr(app, "set_focus",
                        lambda w: calls.append(("set_focus", w)), raising=False)
    return app, overlay, calls


def test_show_browse_reveals_then_focuses(monkeypatch):
    """Order matters: a hidden widget silently refuses focus."""
    from karaoke.tui import KaraokeTui

    app, overlay, calls = _app_with_fakes(monkeypatch)
    app._show_browse()

    assert overlay.has_class(KaraokeTui._BROWSE_OPEN)
    assert calls == [("add_class", "-visible"), ("focus", "library")]


def test_hide_browse_releases_focus(monkeypatch):
    app, overlay, calls = _app_with_fakes(monkeypatch, open_=True)
    app._hide_browse()

    assert not overlay.has_class("-visible")
    assert ("set_focus", None) in calls


def test_toggle_browse_round_trips(monkeypatch):
    app, overlay, _ = _app_with_fakes(monkeypatch)
    app.action_toggle_browse()
    assert overlay.has_class("-visible")
    app.action_toggle_browse()
    assert not overlay.has_class("-visible")


def test_escape_is_a_noop_when_browse_is_closed(monkeypatch):
    app, overlay, calls = _app_with_fakes(monkeypatch)
    app.action_hide_browse()
    assert calls == []


def test_open_selected_hides_overlay_on_success(monkeypatch):
    from karaoke import tui

    app, overlay, _ = _app_with_fakes(monkeypatch, open_=True)
    monkeypatch.setattr(app, "_selected_song",
                        lambda: {"url": "https://youtu.be/x", "kind": "youtube",
                                 "artist": "A", "title": "B"}, raising=False)
    monkeypatch.setattr(tui, "open_song_url", lambda url, kind, **kwargs: 123)
    monkeypatch.setattr(app, "notify", lambda *a, **k: None, raising=False)
    # Run the background open worker inline so the test stays synchronous.
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: fn(), raising=False)
    monkeypatch.setattr(app, "call_from_thread", lambda fn, *a, **k: fn(*a, **k), raising=False)

    app._open_selected()
    assert not overlay.has_class("-visible")


def test_open_selected_hides_overlay_even_when_opening_fails(monkeypatch):
    """The overlay closes immediately; the open runs off the UI thread so a
    slow/failing CDP call can no longer freeze the event loop and swallow keys.
    """
    from karaoke import tui

    app, overlay, _ = _app_with_fakes(monkeypatch, open_=True)
    monkeypatch.setattr(app, "_selected_song",
                        lambda: {"url": "https://youtu.be/x", "kind": "youtube",
                                 "artist": "A", "title": "B"}, raising=False)
    monkeypatch.setattr(tui, "open_song_url",
                        lambda url, kind, **kwargs: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setattr(app, "notify", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: fn(), raising=False)
    monkeypatch.setattr(app, "call_from_thread", lambda fn, *a, **k: fn(*a, **k), raising=False)

    class _NP:
        def update(self, _): pass
    monkeypatch.setattr(app, "query_one",
                        lambda sel, *a, **k: _NP() if "now-playing" in sel
                        else (overlay if "browse-overlay" in sel else _NP()),
                        raising=False)

    app._open_selected()
    assert not overlay.has_class("-visible")


def test_sort_rows_defaults_to_mood_energy(monkeypatch):
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._sort = "energy_desc"
    rows = [
        {"artist": "Low", "title": "Song", "energy": 0.2},
        {"artist": "High", "title": "Song", "energy": 0.9},
    ]
    assert [row["artist"] for row in app._sort_rows(rows)] == ["High", "Low"]


def test_wildcard_queue_is_shuffled_after_filtering(monkeypatch):
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._queue_is_wildcard = True
    app._unfiltered_queue = [
        {"track_id": 1, "artist": "A", "title": "One", "energy": 0.5},
        {"track_id": 2, "artist": "B", "title": "Two", "energy": 0.5},
        {"track_id": 3, "artist": "C", "title": "Three", "energy": 0.5},
    ]
    app._queue_at = -1
    app._sort = "artist"
    app._mood_filter = "all"
    app._genre_filter = "all"
    app._mood_level = 0.0

    class FakeTable:
        def set_class(self, *a, **k): pass
        def clear(self): pass
        def add_columns(self, *a): pass
        def add_row(self, *a): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable(), raising=False)
    monkeypatch.setattr(app, "_render_queue", lambda: None, raising=False)
    monkeypatch.setattr(app, "play_queue_index", lambda *a: None, raising=False)

    import random
    monkeypatch.setattr(random.SystemRandom, "shuffle", lambda self, values: values.reverse())
    app._filter_and_set_queue()

    assert [row["artist"] for row in app._queue] == ["C", "B", "A"]


def test_playlist_queue_controls(monkeypatch):
    """Test enqueueing, shuffling, and clearing queue in KaraokeTui."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._queue = []
    app._queue_at = -1
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)
    monkeypatch.setattr(app, "_selected_song", lambda: {"artist": "Artist 1", "title": "Song 1", "url": "http://a", "kind": "youtube"}, raising=False)
    monkeypatch.setattr(app, "_render_queue", lambda: None, raising=False)
    monkeypatch.setattr(app, "play_queue_index", lambda *a, **k: None, raising=False)

    class FakeTable:
        def set_class(self, *a, **k): pass
        def clear(self): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable(), raising=False)

    app.action_enqueue_selected()
    assert len(app._queue) == 1
    assert app._queue[0]["title"] == "Song 1"

    monkeypatch.setattr(app, "_selected_song", lambda: {"artist": "Artist 2", "title": "Song 2", "url": "http://b", "kind": "youtube"}, raising=False)
    app.action_enqueue_selected()
    assert len(app._queue) == 2

    app.action_shuffle_queue()
    assert len(app._queue) == 2

    app.action_clear_queue()
    assert len(app._queue) == 0
    assert app._queue_at == -1


def test_apply_suggestions_appends_to_queue(monkeypatch):
    """Keep-the-vibe-going picks are appended to the queue."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._queue = [{"track_id": 1, "artist": "Seed", "title": "S", "url": "u", "kind": ""}]
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)
    monkeypatch.setattr(app, "_render_queue", lambda: None, raising=False)

    class FakeTable:
        def set_class(self, *a, **k): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable(), raising=False)

    picks = [
        {"track_id": 10, "artist": "A", "title": "x", "url": "ua", "space": "clap"},
        {"track_id": 11, "artist": "B", "title": "y", "url": "ub", "space": "clap"},
    ]
    app._apply_suggestions(picks)
    assert len(app._queue) == 3
    assert [r["track_id"] for r in app._queue[1:]] == [10, 11]
    assert any("keep the vibe going" in n for n in notifications)


def test_apply_suggestions_empty_warns(monkeypatch):
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._queue = [{"track_id": 1, "artist": "Seed", "title": "S", "url": "u", "kind": ""}]
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)
    app._apply_suggestions([])
    assert len(app._queue) == 1
    assert any("No suggestions" in n for n in notifications)


def test_queue_filtering_on_mood_change(monkeypatch):
    """Test that changing mood filter or energy level filters the active queue."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._mood_filter = "all"
    app._genre_filter = "all"
    app._mood_level = 0.0
    app._unfiltered_queue = [
        {"track_id": 1, "artist": "Soft", "title": "Quiet", "energy": 0.2},
        {"track_id": 2, "artist": "Loud", "title": "Anthem", "energy": 0.9},
    ]
    app._queue = list(app._unfiltered_queue)
    app._queue_at = -1

    monkeypatch.setattr(app, "_render_mood_slider", lambda: None, raising=False)
    monkeypatch.setattr(app, "load_songs", lambda: None, raising=False)
    monkeypatch.setattr(app, "_show_selected_song", lambda: None, raising=False)
    monkeypatch.setattr(app, "_render_queue", lambda: None, raising=False)

    class FakeTable:
        def set_class(self, *a, **k): pass
        def clear(self): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable(), raising=False)

    app._mood_level = 0.75
    app._filter_and_set_queue()
    assert len(app._queue) == 1
    assert app._queue[0]["title"] == "Anthem"


def test_queue_filtering_on_genre_change(monkeypatch):
    """Changing the genre filter also filters the active queue."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._mood_filter = "all"
    app._genre_filter = "punk rock"
    app._mood_level = 0.0
    app._unfiltered_queue = [
        {"track_id": 1, "artist": "A", "title": "Fast", "genre": "punk rock"},
        {"track_id": 2, "artist": "B", "title": "Smooth", "genre": "soul"},
    ]
    app._queue = list(app._unfiltered_queue)
    app._queue_at = -1

    class FakeTable:
        def set_class(self, *a, **k): pass
        def clear(self): pass

    monkeypatch.setattr(app, "query_one", lambda *a, **k: FakeTable(), raising=False)
    monkeypatch.setattr(app, "_render_queue", lambda: None, raising=False)

    app._filter_and_set_queue()
    assert [row["title"] for row in app._queue] == ["Fast"]


def test_star_search_can_fill_queue_for_active_genre_filter(tmp_path):
    """`*` is the explicit show-all query; genre can narrow it afterwards."""
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    try:
        rock_id = localcache.add_track_and_lyrics(
            "A", "Rock Song", Lyrics(plain="words", source="lrclib"),
            url="https://youtu.be/rock", conn=conn)
        pop_id = localcache.add_track_and_lyrics(
            "B", "Pop Song", Lyrics(plain="words", source="lrclib"),
            url="https://youtu.be/pop", conn=conn)
        conn.execute("INSERT INTO track_genre (track_id, genre, labelled_at) VALUES (?, ?, 1)",
                     (rock_id, "rock"))
        conn.execute("INSERT INTO track_genre (track_id, genre, labelled_at) VALUES (?, ?, 1)",
                     (pop_id, "pop"))
        conn.commit()

        app = KaraokeTui.__new__(KaraokeTui)
        app._mood_filter = "all"
        app._genre_filter = "pop"
        app._mood_level = 0.0

        rows = app._apply_mood_filter(app._all_queue_rows(conn), conn)
        assert [row["title"] for row in rows] == ["Pop Song"]
    finally:
        conn.close()


def test_search_with_genre_filter_in_tui(tmp_path):
    """Searching for a query like 'nothing' with an active genre filter only returns tracks of that genre."""
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "tui_genre.db")
    try:
        t1 = localcache.add_track_and_lyrics(
            "Metal Band", "Nothing Else", Lyrics(plain="nothing here", source="lrclib"),
            url="https://youtu.be/metal", conn=conn)
        t2 = localcache.add_track_and_lyrics(
            "Pop Singer", "Nothing Compares", Lyrics(plain="nothing compares", source="lrclib"),
            url="https://youtu.be/pop", conn=conn)
        conn.execute("INSERT INTO track_genre (track_id, genre, labelled_at) VALUES (?, ?, 1)",
                     (t1, "heavy metal"))
        conn.execute("INSERT INTO track_genre (track_id, genre, labelled_at) VALUES (?, ?, 1)",
                     (t2, "pop"))
        conn.commit()

        app = KaraokeTui.__new__(KaraokeTui)
        app._mood_filter = "all"
        app._genre_filter = "heavy metal"
        app._mood_level = 0.0

        from karaoke import librarysearch
        hits = librarysearch.search("nothing", conn, limit=70, genre=app._genre_filter)
        rows = [{"track_id": h.track_id, "artist": h.artist, "title": h.title, "genre": h.genre} for h in hits]
        filtered = app._apply_mood_filter(rows, conn)
        assert len(filtered) == 1
        assert filtered[0]["title"] == "Nothing Else"
        assert filtered[0]["genre"] == "heavy metal"

        # Now switch to pop
        app._genre_filter = "pop"
        hits_pop = librarysearch.search("nothing", conn, limit=70, genre=app._genre_filter)
        rows_pop = [{"track_id": h.track_id, "artist": h.artist, "title": h.title, "genre": h.genre} for h in hits_pop]
        filtered_pop = app._apply_mood_filter(rows_pop, conn)
        assert len(filtered_pop) == 1
        assert filtered_pop[0]["title"] == "Nothing Compares"
        assert filtered_pop[0]["genre"] == "pop"
    finally:
        conn.close()


def test_load_tracks_with_genre_filter(tmp_path):
    """_load_tracks filters by genre in SQL so tracks beyond the first 70 are found."""
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "load_tracks.db")
    try:
        # Insert 80 tracks with genre "rock"
        for i in range(80):
            tid = localcache.add_track_and_lyrics(
                f"A Rocker {i:02d}", f"Rock Song {i}", Lyrics(plain="rock", source="lrclib"),
                url=f"https://youtu.be/rock{i}", conn=conn)
            conn.execute("INSERT INTO track_genre (track_id, genre, labelled_at) VALUES (?, 'rock', 1)", (tid,))
        # Insert 1 jazz track with artist starting with 'Z'
        z_id = localcache.add_track_and_lyrics(
            "Z Jazz Artist", "Jazz Tune", Lyrics(plain="jazz", source="lrclib"),
            url="https://youtu.be/jazz", conn=conn)
        conn.execute("INSERT INTO track_genre (track_id, genre, labelled_at) VALUES (?, 'jazz', 1)", (z_id,))
        conn.commit()

        app = KaraokeTui.__new__(KaraokeTui)
        app._filter = "all"
        app._genre_filter = "jazz"
        app._song_data = []

        app._load_tracks(conn, only_working=False)
        assert len(app._song_data) == 1
        assert app._song_data[0]["title"] == "Jazz Tune"
        assert app._song_data[0]["genre"] == "jazz"
    finally:
        conn.close()



def test_play_count_migration_and_increment(tmp_path):
    """play_count column is backfilled from play_events and bumped on play."""
    from karaoke import localcache

    conn = localcache.connect(tmp_path / "pc.db")
    # Seed a track and some historical play events.
    localcache.add_track_source("Flea", "A Plea", conn=conn)
    for _ in range(3):
        localcache.log_event("file", "play", artist="Flea", title="A Plea", conn=conn)

    # A fresh connect() re-runs ensure_play_count_column, which backfills.
    conn.close()
    conn = localcache.connect(tmp_path / "pc.db")
    row = conn.execute(
        "SELECT play_count FROM tracks WHERE artist='Flea' AND title='A Plea'"
    ).fetchone()
    assert row["play_count"] == 3

    # A new play event increments the denormalised counter.
    localcache.log_event("file", "play", artist="Flea", title="A Plea", conn=conn)
    row = conn.execute(
        "SELECT play_count FROM tracks WHERE artist='Flea' AND title='A Plea'"
    ).fetchone()
    assert row["play_count"] == 4
    conn.close()


# --- A: approve for post-processing ---------------------------------------

def _approver(monkeypatch, tmp_path):
    """A KaraokeTui wired to a temp DB, without booting the app."""
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)
    app = KaraokeTui.__new__(KaraokeTui)
    return app, conn


def _add_track(conn, artist="A", title="B", *, synced=True, plain=True):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    localcache.add_track_and_lyrics(
        artist, title,
        Lyrics(plain="w" if plain else "",
               synced_raw="[00:01.00] w" if synced else "", source="lrclib"),
        url="https://youtu.be/x", conn=conn)


def test_approve_refuses_when_nothing_is_playing(monkeypatch, tmp_path):
    app, _ = _approver(monkeypatch, tmp_path)
    ok, msg = app.approve_postprocess("", "")
    assert not ok and "Nothing playing" in msg


def test_approve_refuses_a_track_not_in_the_library(monkeypatch, tmp_path):
    app, _ = _approver(monkeypatch, tmp_path)
    ok, msg = app.approve_postprocess("Ghost", "Missing")
    assert not ok and "not in the library" in msg


def test_approve_refuses_when_there_are_no_lyrics(monkeypatch, tmp_path):
    """"if text found" — with none, the gap queue is the right path, not this."""
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn, synced=False, plain=False)
    ok, msg = app.approve_postprocess("A", "B")
    assert not ok and "No lyrics" in msg


def test_approve_queues_when_lyrics_exist(monkeypatch, tmp_path):
    from karaoke import postprocess_queue as pq
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn)
    published = []
    monkeypatch.setattr(pq, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t, u)) or True)

    ok, msg = app.approve_postprocess("A", "B", "https://youtu.be/x")
    assert ok, msg
    assert published == [("A", "B", "https://youtu.be/x")]
    assert "analysis" in msg          # what it actually queued


def test_approve_reports_an_unreachable_broker(monkeypatch, tmp_path):
    from karaoke import postprocess_queue as pq
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn)
    monkeypatch.setattr(pq, "publish_postprocess_task", lambda *a, **k: False)

    ok, msg = app.approve_postprocess("A", "B")
    assert not ok and "Broker unreachable" in msg


def test_approve_clears_the_session_guard(monkeypatch, tmp_path):
    """So a track can be re-queued after the broker comes back."""
    from karaoke import postprocess_queue as pq
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn)
    monkeypatch.setattr(pq, "publish_postprocess_task", lambda *a, **k: True)
    app._current_song = ("A", "B", "")
    app._postprocess_enqueued = {("a", "b")}
    monkeypatch.setattr(app, "notify", lambda *a, **k: None, raising=False)

    app.action_approve_postprocess()
    assert ("a", "b") not in app._postprocess_enqueued


# --- background lyric fetch ------------------------------------------------

def test_background_fetch_caches_lyrics_and_forces_a_resync(monkeypatch, tmp_path):
    """The cache-only detection path could never gain lyrics on its own."""
    from karaoke import localcache, tui
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(tui, "fetch_lrclib", None, raising=False)
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib",
                        lambda a, t, *args, **kw: Lyrics(
                            plain="w", synced_raw="[00:01.00] w", source="lrclib"))

    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("a", "b")
    app._background_fetch_lyrics("A", "B")

    assert localcache.get_cached_lyrics("A", "B", conn=conn) is not None
    assert app._sync_key is None          # next poll re-resolves


def test_background_fetch_is_quiet_on_a_miss(monkeypatch, tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib",
                        lambda a, t, *args, **kw: Lyrics())

    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("a", "b")
    app._background_fetch_lyrics("A", "B")

    assert localcache.get_cached_lyrics("A", "B", conn=conn) is None
    assert app._sync_key == ("a", "b")    # nothing changed, no needless resync


def test_background_fetch_survives_a_network_error(monkeypatch, tmp_path):
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)

    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib", boom)

    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("a", "b")
    app._background_fetch_lyrics("A", "B")     # must not raise


def test_background_fetch_ignores_a_blank_track(monkeypatch, tmp_path):
    from karaoke.tui import KaraokeTui
    app = KaraokeTui.__new__(KaraokeTui)
    app._background_fetch_lyrics("", "")       # must not raise or hit the net


# --- mic / radio mode ------------------------------------------------------

def _mic_app(ref=None, stopped=True, duration=None):
    from karaoke.tui import KaraokeTui
    app = KaraokeTui.__new__(KaraokeTui)
    app._mic_ref = ref
    app._mic_stop = None if stopped else __import__("threading").Event()
    app._mode_override = None
    # Bounds the dead-reckoned playhead so a repeating track cannot run past
    # its own end; None leaves the reckoning unbounded. See
    # tests/test_sync_offset_modes.py for the wrap behaviour itself.
    app._track_duration = duration
    return app


def _ref(artist="A", title="B", offset=30.0, mono=100.0):
    from karaoke.identify import SongRef
    return SongRef(artist=artist, title=title, source="songrec",
                   offset=offset, offset_mono=mono)


def test_mic_elapsed_dead_reckons_from_the_anchor():
    """No MPRIS position in radio mode: offset + time since + the lead."""
    from karaoke.player import DEFAULT_LEAD_S

    app = _mic_app(_ref(offset=30.0, mono=100.0))
    assert app.mic_elapsed(now=100.0) == 30.0 + DEFAULT_LEAD_S
    assert app.mic_elapsed(now=112.5) == 42.5 + DEFAULT_LEAD_S


def test_mic_elapsed_applies_the_same_lead_as_the_cli():
    """songrec listens ~10s before answering, so its offset is already stale.

    `karaoke -r` compensates with DEFAULT_LEAD_S; radio mode in the TUI was
    missing it entirely and ran that far behind.
    """
    from karaoke.player import DEFAULT_LEAD_S

    assert DEFAULT_LEAD_S == 12.6
    app = _mic_app(_ref(offset=0.0, mono=0.0))
    assert app.mic_elapsed(now=0.0) == DEFAULT_LEAD_S


def test_mic_elapsed_is_none_without_an_identification():
    assert _mic_app(None).mic_elapsed() is None


def test_mic_elapsed_is_none_when_songrec_gave_no_offset():
    """songrec can name a track without locating the playhead in it."""
    app = _mic_app(_ref(offset=None, mono=None))
    assert app.mic_elapsed() is None


def test_mic_identification_wins_over_mpris(monkeypatch):
    """The mic hears the room — that is the point of switching it on."""
    from karaoke import detect

    app = _mic_app(_ref("Otis Redding", "Dock of the Bay"))
    monkeypatch.setattr(detect, "detect_active",
                        lambda *a: detect.Detection(mode="scan", artist="Other",
                                                    title="Wrong Song"))
    det = app._effective_detection()
    assert det.mode == "radio"
    assert (det.artist, det.title) == ("Otis Redding", "Dock of the Bay")


def test_mpris_is_used_when_the_mic_is_off(monkeypatch):
    from karaoke import detect

    app = _mic_app(None)
    monkeypatch.setattr(detect, "detect_active",
                        lambda *a: detect.Detection(mode="scan", artist="X", title="Y"))
    assert app._effective_detection().mode == "scan"


def test_radio_detection_counts_as_active():
    """There is no player to control, but there is a song to follow."""
    from karaoke import detect
    assert detect.Detection(mode="radio", artist="A", title="B").is_active


def test_toggle_mic_refuses_when_the_cli_radio_holds_it(monkeypatch):
    from karaoke import tui

    app = _mic_app(None)
    monkeypatch.setattr(tui.shutil if hasattr(tui, "shutil") else tui,
                        "__name__", "tui", raising=False)
    monkeypatch.setattr(tui, "_radio_cli_running", lambda: True)
    notes = []
    monkeypatch.setattr(app, "notify",
                        lambda m, **k: notes.append((m, k.get("severity"))),
                        raising=False)
    started = []
    monkeypatch.setattr(app, "run_worker",
                        lambda *a, **k: started.append(a), raising=False)

    app.action_toggle_mic()
    assert not started
    assert "already using the mic" in notes[0][0]


def test_toggle_mic_off_clears_state(monkeypatch):
    import threading
    app = _mic_app(_ref(), stopped=False)
    notes = []
    monkeypatch.setattr(app, "notify", lambda m, **k: notes.append(m), raising=False)
    app._sync_key = ("a", "b")

    app.action_toggle_mic()
    assert app._mic_stop.is_set()
    assert app._mic_ref is None
    assert app._sync_key is None          # forces a re-resolve back to MPRIS
    assert notes == ["Mic off"]


def test_context_window_scales_to_the_panel_height():
    """A full-height pane showed 8 lines and left the rest empty."""
    from rich.text import Text
    from karaoke.player import LyricTimeline, _render_body

    lines = [(float(i * 4), f"lyric line {i}") for i in range(40)]
    tl = LyricTimeline(lines)

    def shown(rows):
        before, after = (3, 5) if rows < 10 else (rows // 3, rows - rows // 3)
        body = Text()
        _render_body(body, tl, 60.0, before=before, after=after)
        return len(body.plain.rstrip().splitlines())

    assert shown(12) > 8            # more than the old hard-coded window
    assert shown(40) > shown(12)    # and it keeps scaling


# --- figlet title banner ---------------------------------------------------

def _banner(title, width, height=99, artist="Gotye"):
    from karaoke.tui import KaraokeTui
    return KaraokeTui.__new__(KaraokeTui).title_banner(artist, title, width, height)


def test_title_renders_as_block_type_when_it_fits():
    """A header is one short string with space around it — block type suits it."""
    from karaoke import bigtext

    out = _banner("Golden Brown", 150)
    assert "♪" not in out
    assert out.count("\n") >= 3          # several block rows
    # The artist sits under the title in a smaller face, so the tail of the
    # banner is its block rows rather than the literal name.
    byline = bigtext.render_line("Gotye", bigtext.SMALL_FONT).rows
    assert out.splitlines()[-len(byline):] == list(byline)


def test_title_falls_back_to_plain_when_too_narrow():
    assert _banner("Somebody That I Used To Know", 60) == \
        "♪ Gotye - Somebody That I Used To Know"


def test_title_falls_back_when_the_header_is_compacted():
    """On a short terminal now-playing shrinks to 3 rows; a banner cannot fit."""
    assert _banner("Golden Brown", 150, height=3).startswith("♪ ")


def test_title_falls_back_for_a_title_too_long_to_fit_one_line():
    long_title = "A Really Very Extremely Long Song Title That Cannot Possibly Fit"
    assert _banner(long_title, 120).startswith("♪ ")


def test_title_banner_never_wraps_to_a_second_block():
    """max_rows=1: a header that wrapped would push the status line out.

    The banner is now two blocks -- the title, then the artist in a smaller
    face -- so the title's own rows are checked rather than every row but the
    last.
    """
    from karaoke import bigtext

    out = _banner("Disappearer", 150)
    title_rows = len(bigtext.render_line("Disappearer").rows)
    rows = out.splitlines()[:title_rows]
    assert len({len(r) for r in rows}) == 1          # block rows equal width


# --- cover art source ------------------------------------------------------

def test_cover_prefers_the_players_own_art(monkeypatch, tmp_path):
    """Browsers write the cover locally, so nothing has to be downloaded."""
    from karaoke import playerctl, tui
    from karaoke.tui import KaraokeTui

    art = tmp_path / "cover.png"
    art.write_bytes(b"x")
    monkeypatch.setattr(playerctl, "art_url", lambda *a, **k: f"file://{art}")

    app = KaraokeTui.__new__(KaraokeTui)
    monkeypatch.setattr(app, "_control_player", lambda: "", raising=False)
    assert app.cover_source("") == art


def test_cover_falls_back_to_the_cached_video(monkeypatch, tmp_path):
    """No cover: the first frame of the downloaded media stands in."""
    import types
    from karaoke import config, playerctl
    from karaoke.tui import KaraokeTui

    monkeypatch.setattr(playerctl, "art_url", lambda *a, **k: "")
    # settings is a frozen dataclass, so swap the object rather than a field.
    monkeypatch.setattr(config, "settings",
                        types.SimpleNamespace(youtube_dir=str(tmp_path)))
    media = tmp_path / "_3tkup9b-iM.webm"
    media.write_bytes(b"x")

    app = KaraokeTui.__new__(KaraokeTui)
    monkeypatch.setattr(app, "_control_player", lambda: "", raising=False)
    assert app.cover_source("https://youtu.be/_3tkup9b-iM") == media


def test_cover_source_is_none_when_there_is_nothing(monkeypatch, tmp_path):
    import types
    from karaoke import config, playerctl
    from karaoke.tui import KaraokeTui

    monkeypatch.setattr(playerctl, "art_url", lambda *a, **k: "")
    monkeypatch.setattr(config, "settings",
                        types.SimpleNamespace(youtube_dir=str(tmp_path)))

    app = KaraokeTui.__new__(KaraokeTui)
    monkeypatch.setattr(app, "_control_player", lambda: "", raising=False)
    assert app.cover_source("https://youtu.be/_3tkup9b-iM") is None


# --- track info read-out ---------------------------------------------------

def test_track_info_lines_up_in_one_column():
    from karaoke.tui import track_info

    out = track_info(source="lrclib", duration=245.0, offset=-0.3,
                     pending=["analysis"], lyric_lines=42)
    starts = {len(line) - len(line.lstrip()) or line.index(line.split()[1])
              for line in out.splitlines()}
    assert len(starts) == 1
    assert out.isascii()          # nothing mis-measurable in the labels


def test_track_info_formats_duration_as_minutes():
    from karaoke.tui import track_info
    assert "4:05" in track_info(duration=245.0)
    assert "0:07" in track_info(duration=7.0)


def test_track_info_shows_what_postprocessing_is_left():
    from karaoke.tui import track_info
    assert "analysis, timings" in track_info(pending=["analysis", "timings"])
    assert "done" in track_info(pending=[])


def test_track_info_omits_what_is_not_known():
    """A track with nothing known should not render empty labels."""
    from karaoke.tui import track_info

    out = track_info(offset=0.2)
    assert "source" not in out and "length" not in out and "postproc" not in out
    assert "+0.2s" in out


def test_track_info_shows_genre_and_sound():
    from karaoke.tui import track_info

    out = track_info(genre="blues", sound="delta blues (acoustic)")
    assert "genre" in out
    assert "blues" in out
    assert "sound" in out
    assert "delta blues (acoustic)" in out


def test_more_like_this_binding_exists():
    from karaoke.tui import KaraokeTui

    actions = {b[1] if isinstance(b, tuple) else b.action for b in KaraokeTui.BINDINGS}
    assert "more_like_this" in actions


def test_an_error_replaces_the_block(): 
    """When something is wrong, that is the thing worth reading."""
    from karaoke.tui import track_info

    out = track_info(source="lrclib", duration=100.0, error="broker down")
    assert out == "! broker down"
    assert "lrclib" not in out


def test_offset_sign_is_always_shown():
    from karaoke.tui import track_info
    assert "+0.0s" in track_info(offset=0.0)
    assert "-1.5s" in track_info(offset=-1.5)


# --- stats screen ----------------------------------------------------------

def _stub_stats():
    from karaoke.librarystats import LibraryStats
    return LibraryStats(
        tracks=100, with_lyrics=90, synced=80, plain_only=10, sources=60,
        staged=5, analysed=50, word_timed=3,
        lyric_sources=[("lrclib", 70), ("youtube_caption_manual_en_enhanced", 20)],
        gaps=[("processed", 30), ("pending", 10)],
        keys=[("E minor", 12)], tempo_bands=[("moderato 100-129", 40)],
    )


def _stub_summary():
    from karaoke import localcache
    return localcache.CacheSummary(
        total_events=100, plays=50, discoveries=20, cache_hits=30,
        cache_misses=10, distinct_tracks=40, distinct_artists=25,
        top_tracks=[], top_artists=[], by_mode=[],
    ) if hasattr(localcache, "CacheSummary") else None


def test_library_stats_derive_backlog_and_share():
    lib = _stub_stats()
    assert lib.unanalysed == 50
    assert lib.synced_share == 0.8


def test_bar_is_ascii_so_it_cannot_shift():
    """Block glyphs are ambiguous width — the sentiment-bar lesson."""
    from karaoke.librarystats import bar
    out = bar(5, 10, width=10)
    assert out.isascii() and len(out) == 10
    assert bar(0, 10) == "." * 12
    assert bar(10, 10) == "#" * 12


def test_bar_handles_a_zero_total():
    from karaoke.librarystats import bar
    assert len(bar(0, 0, width=8)) == 8


def test_stats_panels_cover_every_section():
    summary = _stub_summary()
    if summary is None:
        return
    from karaoke.tui import stats_panels
    titles = [t for t, _ in stats_panels(_stub_stats(), summary)]
    for expected in ("library", "pipeline", "listening", "lyric sources",
                     "gaps", "keys", "tempo"):
        assert expected in titles


def test_stats_panels_truncate_long_source_names():
    """Caption names run to 35 chars and would push the bars out of line."""
    summary = _stub_summary()
    if summary is None:
        return
    from karaoke.tui import stats_panels
    rows = dict(stats_panels(_stub_stats(), summary))["lyric sources"]
    assert all(len(label) <= 16 for label, _ in rows)


def test_stats_panels_work_without_a_broker():
    """A broker outage must not hide the library and listening numbers."""
    summary = _stub_summary()
    if summary is None:
        return
    from karaoke.tui import stats_panels
    panels = dict(stats_panels(_stub_stats(), summary, None))
    assert "workers" not in dict(panels["pipeline"])
    assert panels["library"]


# --- Spotify filter --------------------------------------------------------

def test_spotify_is_a_filter_option():
    from karaoke.tui import FILTER_OPTIONS
    assert ("Spotify tracks", "spotify") in FILTER_OPTIONS


def test_load_spotify_returns_only_spotify_sourced_rows(tmp_path):
    """The rows must carry the *Spotify* url/kind, so Enter opens the web player.

    _load_tracks ranks spotify below every browser-openable kind, which is why
    these tracks are otherwise unreachable from the library.
    """
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.executescript(
            """
            INSERT INTO tracks (track_id, artist, title) VALUES
                (1, 'A', 'Has Both'), (2, 'B', 'YouTube Only');
            INSERT INTO sources (track_id, kind, url) VALUES
                (1, 'youtube', 'https://youtu.be/xyz'),
                (1, 'spotify', 'spotify:track:abc'),
                (2, 'youtube', 'https://youtu.be/def');
            """
        )
        conn.commit()
        app = KaraokeTui.__new__(KaraokeTui)
        app._song_data = []
        app._load_spotify(conn)
    finally:
        conn.close()

    assert [r["title"] for r in app._song_data] == ["Has Both"]
    assert app._song_data[0]["kind"] == "spotify"
    assert app._song_data[0]["url"] == "spotify:track:abc"


# --- the beat animation runs without lyrics --------------------------------

def _ticking_app(monkeypatch, *, lines):
    """A KaraokeTui wired just enough to run _tick_lyrics."""
    from karaoke import detect, playerctl, tui
    from karaoke.player import LyricTimeline
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._det = detect.Detection(mode="spotify", player="spotify",
                                artist="A", title="B")
    app._timeline = LyricTimeline(lines=lines)
    app._sync_offset = 0.0
    app._elapsed = 0.0
    app._sync_mood = "neutral"
    monkeypatch.setattr(app, "_control_player", lambda: "spotify", raising=False)
    monkeypatch.setattr(playerctl, "position", lambda p="": 42.0)
    monkeypatch.setattr(tui, "browser_playback", lambda: None)
    rendered = []
    monkeypatch.setattr(app, "_render_visuals",
                        lambda song, preview, elapsed: rendered.append(elapsed),
                        raising=False)
    monkeypatch.setattr(app, "_render_synced",
                        lambda e: rendered.append(("synced", e)), raising=False)
    return app, rendered


def test_visuals_tick_without_synced_lyrics(monkeypatch):
    """The regression: the rhythm bar froze whenever a track had no lyrics.

    It only needs BPM and elapsed time, but it was gated on the timeline, so in
    Spotify mode the BPM read-out kept updating beside a motionless animation.
    """
    app, rendered = _ticking_app(monkeypatch, lines=[])
    app._tick_lyrics()
    assert rendered == [42.0]
    assert app._elapsed == 42.0


def test_synced_lyrics_still_drive_the_visuals(monkeypatch):
    """With a timeline, _render_synced runs and refreshes the visuals itself."""
    app, rendered = _ticking_app(monkeypatch, lines=[(0.0, "hello")])
    app._tick_lyrics()
    assert rendered == [("synced", 42.0)]


def test_nothing_ticks_when_no_player_is_active(monkeypatch):
    from karaoke import detect

    app, rendered = _ticking_app(monkeypatch, lines=[])
    app._det = detect.Detection(mode="browse")
    app._tick_lyrics()
    assert rendered == []


def test_nothing_ticks_without_a_position(monkeypatch):
    from karaoke import playerctl

    app, rendered = _ticking_app(monkeypatch, lines=[])
    monkeypatch.setattr(playerctl, "position", lambda p="": None)
    app._tick_lyrics()
    assert rendered == []


# --- mood art --------------------------------------------------------------

def _mood_app(monkeypatch, *, art=None, source="", shown=""):
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._mood_art = art
    app._mood_source = source
    app._mood_shown = shown
    updates = []

    class _Panel:
        def update(self, value):
            updates.append(value)

    monkeypatch.setattr(app, "query_one", lambda *a, **kw: _Panel(), raising=False)
    monkeypatch.setattr(app, "_refresh_mood_art",
                        lambda mood: updates.append(("refresh", mood)),
                        raising=False)
    return app, updates


def test_mood_falls_back_to_the_glyph_block_before_art_arrives(monkeypatch):
    """Rendering happens off the UI thread; the panel must not be blank meanwhile."""
    app, updates = _mood_app(monkeypatch)
    app._update_mood("happy")
    assert ("refresh", "happy") in updates

def test_a_new_mood_triggers_a_rerender(monkeypatch):
    app, updates = _mood_app(monkeypatch, shown="sad")
    app._update_mood("angry")
    assert ("refresh", "angry") in updates
    assert app._mood_shown == "angry"


def test_the_same_mood_does_not_rerender(monkeypatch):
    """Scoring the pool costs ffmpeg calls; it must not run on every tick."""
    from rich.text import Text

    app, updates = _mood_app(monkeypatch, art=Text("x"), source="cover",
                             shown="happy")
    app._update_mood("happy")
    assert not any(isinstance(u, tuple) for u in updates)


def test_the_mood_word_survives_alongside_the_picture(monkeypatch):
    """The word is what makes a wrong match obviously wrong."""
    from rich.text import Text

    app, updates = _mood_app(monkeypatch, art=Text("art"), source="generated",
                             shown="tender")
    app._update_mood("tender")
    rendered = str(updates[-1])
    assert "generated" not in rendered


# --- the classification read-out under the cover art ----------------------

def _genre_row(genre="trip hop", score=0.62, runner_up="post-punk",
               runner_up_score=0.45):
    """A stand-in for a track_genre row."""
    return {"genre": genre, "score": score, "runner_up": runner_up,
            "runner_up_score": runner_up_score}


def test_a_clear_specific_label_is_shown_plainly():
    from karaoke.tui import genre_line

    assert genre_line(_genre_row()) == "trip hop"


def test_a_narrow_margin_shows_the_runner_up():
    """"heavy metal, ~punk rock" describes sludge better than either alone."""
    from karaoke.tui import genre_line

    line = genre_line(_genre_row(genre="heavy metal", score=0.708,
                                 runner_up="punk rock", runner_up_score=0.689))
    assert line == "heavy metal ~punk rock"


def test_a_clear_win_on_a_broad_label_is_not_marked_doubtful():
    """Marking every broad label was tried and was wrong.

    The four clearest decisions in the library are "pop" for Lily Allen, Tove
    Lo, UPSAHL and Lola Young -- pop songs -- so a question mark put doubt
    exactly where the classifier was most right. A thin margin already says
    "uncertain"; the label itself does not.
    """
    from karaoke.tui import genre_line

    assert genre_line(_genre_row(genre="pop", score=0.65, runner_up="folk",
                                 runner_up_score=0.40)) == "pop"


def test_a_thin_win_on_a_broad_label_shows_its_runner_up():
    from karaoke.tui import genre_line

    assert genre_line(_genre_row(genre="pop", score=0.60, runner_up="punk rock",
                                 runner_up_score=0.595)) == "pop ~punk rock"


def test_no_genre_shows_nothing():
    from karaoke.tui import genre_line

    assert genre_line(None) == ""


def test_the_read_out_puts_genre_first():
    """Directly under the cover art, above the lyric provenance."""
    from karaoke.tui import track_info

    text = track_info(genre="trip hop", source="lrclib", lyric_lines=42)
    lines = text.splitlines()
    assert lines[0].startswith("genre")
    assert any(ln.startswith("source") for ln in lines)


def test_a_track_without_a_genre_still_renders():
    from karaoke.tui import track_info

    text = track_info(source="lrclib", lyric_lines=42)
    assert "genre" not in text
    assert "lrclib" in text


def _tone_row(tone="cynical and sarcastic", score=0.42,
              runner_up="sad and mournful", runner_up_score=0.30):
    return {"tone": tone, "score": score, "runner_up": runner_up,
            "runner_up_score": runner_up_score}


def test_a_clear_tone_qualifies_the_genre_in_the_readout():
    from karaoke.tui import classification_line

    assert classification_line(_genre_row(genre="pop", runner_up="folk",
                                          runner_up_score=0.4),
                               _tone_row()) == "cynical pop"


def test_an_unclear_tone_does_not_qualify_it():
    from karaoke.tui import classification_line

    close = _tone_row(score=0.42, runner_up_score=0.415)
    assert classification_line(_genre_row(genre="pop", runner_up="folk",
                                          runner_up_score=0.4),
                               close) == "pop"


def test_a_track_with_no_tone_shows_its_genre():
    from karaoke.tui import classification_line

    assert classification_line(_genre_row(), None) == "trip hop"


def test_a_track_with_only_a_tone_shows_that():
    """The axes are independent: audio may never have been embedded."""
    from karaoke.tui import classification_line

    assert classification_line(None, _tone_row()) == "cynical"


def test_the_title_banner_is_not_only_for_synced_tracks():
    """It is what is playing, so it should not depend on the lyrics.

    Drawing it only on the synced branch meant the title silently dropped to a
    one-line "artist - title" for every track without timings -- the state a
    track is in before any work has been done on it.
    """
    import inspect

    from karaoke.tui import KaraokeTui

    src = inspect.getsource(KaraokeTui._poll_detection)
    body = src.split("state = lyric_display_state(lyrics)", 1)[1]
    # One banner, computed before the branches, used by all of them.
    assert body.count("self.title_banner(") == 1
    assert body.count('f"{banner}\\n') == 3


# --- the header banner ----------------------------------------------------

def test_the_artist_is_set_smaller_than_the_title():
    """At the same size they compete; the point is a heading and its byline."""
    from karaoke import bigtext

    assert bigtext.SMALL_FONT != bigtext.FONT
    title = bigtext.render_line("Swans")
    byline = bigtext.render_line("Swans", bigtext.SMALL_FONT)
    assert len(byline.rows) < len(title.rows)


def test_the_banner_drops_the_artist_rather_than_the_whole_block():
    """Both fonts grow a row for descenders, so a fixed header cannot always
    hold both in block type. Losing one line beats losing the banner."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    # 6 rows: enough for the 5-row title plus a plain artist line, but not for
    # the title plus a 2-row byline.
    tight = app.title_banner("Portishead", "Glory Box", 90, 6)
    # Title still in block type, artist demoted to a plain line.
    assert len(tight.splitlines()) <= 6
    assert tight.splitlines()[-1].strip() == "Portishead"
    assert "Portishead" in tight
    assert not tight.startswith("♪")


def test_the_banner_fits_the_header_it_is_drawn_in():
    """Box height 9 leaves 7 content rows, one of which is the status line."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    for artist, title in (("Swans", "Screen Shot"), ("Portishead", "Glory Box"),
                          ("Ween", "Bananas and Blow")):
        rows = len(app.title_banner(artist, title, 90, 6).splitlines())
        assert rows <= 6, f"{artist} - {title} took {rows} rows"


def test_a_narrow_panel_still_falls_back_to_plain_text():
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    assert app.title_banner("A", "B", 20, 6).startswith("♪")


def test_the_mood_panel_does_not_label_where_the_art_came_from():
    """"cover" sat next to the picture competing with it for attention, and is
    not something to read on every track."""
    import inspect

    from karaoke.tui import KaraokeTui

    src = inspect.getsource(KaraokeTui._update_mood)
    assert "_mood_source" not in src


def test_the_plain_fallback_says_why(caplog):
    """Three thresholds can drop the banner and the only visible sign is that
    the block type is gone. "the terminal is two rows too short" is actionable;
    "the figlet disappeared" is not."""
    import logging

    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    with caplog.at_level(logging.DEBUG, logger="karaoke"):
        app.title_banner("A", "B", 20, 99)          # too narrow
        app.title_banner("A", "B", 200, 2)          # too short
    text = caplog.text
    assert "cols" in text and "rows" in text


def test_a_long_title_uses_a_smaller_face_rather_than_giving_up():
    """"A Little God in My Hands" wants 100 columns in the big face and the
    panel has 74, which is why the banner kept vanishing on ordinary songs. A
    smaller title still reads as a title; plain text does not."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    out = app.title_banner("Swans", "A Little God in My Hands", 74, 6)
    assert not out.startswith("♪")


def test_a_shrunken_title_does_not_get_a_block_byline():
    """Two lines in the same small face read as one wrapped title."""
    from karaoke import bigtext
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    out = app.title_banner("Swans", "A Little God in My Hands", 74, 6)
    assert out.splitlines()[-1].strip() == "Swans"


def test_a_short_title_still_gets_the_big_face_and_a_block_byline():
    from karaoke import bigtext
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    out = app.title_banner("Swans", "Screen Shot", 74, 6)
    byline = bigtext.render_line("Swans", bigtext.SMALL_FONT).rows
    assert out.splitlines()[-len(byline):] == list(byline)
