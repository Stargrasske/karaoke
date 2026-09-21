"""Tests for active-player detection and mode selection."""

from karaoke import detect
from karaoke.detect import Detection, classify, is_youtube_url
from karaoke.playerctl import PlayerMetadata


def test_no_metadata_is_browse():
    det = classify(None)
    assert det.mode == "browse"
    assert not det.is_active


def test_spotify_player_is_spotify_mode():
    meta = PlayerMetadata(artist="Ren", title="Hi Ren", player="spotify")
    det = classify(meta)
    assert det.mode == "spotify"
    assert det.is_active
    assert det.title == "Hi Ren"


def test_browser_youtube_is_scan_mode():
    meta = PlayerMetadata(
        artist="",
        title="Ren - The Tale of Jenny & Screech",
        url="https://www.youtube.com/watch?v=abc",
        player="firefox.instance123",
    )
    det = classify(meta)
    assert det.mode == "scan"
    assert det.is_active
    assert det.url.endswith("v=abc")


def test_empty_track_is_browse():
    meta = PlayerMetadata(artist="", title="", url="", player="firefox")
    assert classify(meta).mode == "browse"


def test_is_youtube_url():
    assert is_youtube_url("https://music.youtube.com/watch?v=x")
    assert is_youtube_url("https://youtu.be/x")
    assert not is_youtube_url("https://open.spotify.com/track/x")


def test_detection_is_active_flag():
    assert Detection("scan").is_active
    assert Detection("spotify").is_active
    assert not Detection("browse").is_active


# --- gap recording ---------------------------------------------------------

def test_record_gap_skips_when_player_gives_no_artist(tmp_path):
    """YouTube Music reports a title with no artist; such a gap is unfillable.

    Chromium exposes no xesam:url either, so nothing could ever resolve the
    artist later — the row would be retried forever against LRCLIB.
    """
    from karaoke import detect, localcache

    conn = localcache.connect(tmp_path / "karaoke.db")
    detect.record_gap(
        detect.Detection(mode="scan", player="chromium", artist="", title="September"),
        conn,
    )
    rows = conn.execute("SELECT artist, title FROM lyric_gaps").fetchall()
    assert rows == []


def test_record_gap_logs_when_artist_is_present(tmp_path):
    from karaoke import detect, localcache

    conn = localcache.connect(tmp_path / "karaoke.db")
    detect.record_gap(
        detect.Detection(mode="scan", player="chromium",
                         artist="Earth, Wind & Fire", title="September"),
        conn,
    )
    rows = conn.execute("SELECT artist, title FROM lyric_gaps").fetchall()
    assert [(r["artist"], r["title"]) for r in rows] == [("Earth, Wind & Fire", "September")]


# --- choosing between simultaneously-playing players -----------------------

def _meta(artist, title, mpris="spotify"):
    from karaoke.playerctl import PlayerMetadata
    return PlayerMetadata(artist=artist, title=title, album="", url="",
                          player=mpris, mpris_name=mpris)


def test_preferred_player_is_the_only_candidate():
    assert detect.preferred_player(["spotify"]) == "spotify"


def test_preferred_player_with_no_candidates_is_blank():
    assert detect.preferred_player([]) == ""


def test_preferred_player_follows_the_mic(monkeypatch):
    """The mic hears the room, so its track names the player making the sound."""
    meta = {"chromium.instance1": _meta("Macy Gray", "I Try", "chromium.instance1"),
            "spotify": _meta("Jethro Tull", "Aqualung")}
    monkeypatch.setattr(detect, "current_metadata", lambda p="": meta.get(p))

    assert detect.preferred_player(
        ["chromium.instance1", "spotify"], "Macy Gray", "I Try",
    ) == "chromium.instance1"


def test_preferred_player_defaults_to_spotify(monkeypatch):
    """No mic reference: Spotify reports an exact position, a browser tab does not."""
    monkeypatch.setattr(detect, "current_metadata", lambda p="": None)
    assert detect.preferred_player(["chromium.instance1", "spotify"]) == "spotify"


def test_preferred_player_is_order_independent(monkeypatch):
    """Otherwise the mode flaps with playerctl's listing order."""
    monkeypatch.setattr(detect, "current_metadata", lambda p="": None)
    a = detect.preferred_player(["chromium.instance1", "spotify"])
    b = detect.preferred_player(["spotify", "chromium.instance1"])
    assert a == b == "spotify"


def test_preferred_player_falls_back_to_the_first(monkeypatch):
    monkeypatch.setattr(detect, "current_metadata", lambda p="": None)
    assert detect.preferred_player(["vlc", "mpv"]) == "vlc"


def test_detect_active_ignores_a_paused_player(monkeypatch):
    """End to end, the exact reported situation.

    A paused kiosk Chrome holding a stale track must not shadow the Spotify
    window that is actually playing.
    """
    from karaoke import playerctl

    monkeypatch.setattr(playerctl, "playing_players", lambda: ["spotify"])
    monkeypatch.setattr(
        detect, "current_metadata",
        lambda p="": _meta("Jethro Tull", "Aqualung") if p == "spotify" else None)

    det = detect.detect_active()
    assert det.mode == "spotify"
    assert det.title == "Aqualung"


# --- the album survives detection ------------------------------------------
#
# Players publish xesam:album, playerctl parsed it and normalize_player_track
# carried it as far as SongRef -- and then classify dropped it, one field short
# of being storable. Nothing downstream could pass on what it never received.

def test_a_detection_carries_the_album():
    meta = PlayerMetadata(artist="System Of A Down", title="B.Y.O.B.",
                          album="Mezmerize", url="", player="chromium",
                          mpris_name="chromium.instance1")
    assert classify(meta).album == "Mezmerize"


def test_spotify_detections_carry_it_too():
    meta = PlayerMetadata(artist="Portishead", title="Glory Box", album="Dummy",
                          url="", player="spotify", mpris_name="spotify")
    det = classify(meta)
    assert det.mode == "spotify" and det.album == "Dummy"


def test_a_player_with_no_album_is_not_a_problem():
    meta = PlayerMetadata(artist="A", title="B", album="", url="",
                          player="vlc", mpris_name="vlc")
    assert classify(meta).album == ""


def test_playing_a_track_fills_in_a_missing_album(tmp_path):
    """The library holds hundreds of rows with no album; playing one should
    fill it rather than requiring a re-import."""
    from karaoke import localcache
    from karaoke.detect import Detection, record_gap

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.execute("INSERT INTO tracks (artist, title, album)"
                     " VALUES ('System Of A Down', 'B.Y.O.B.', '')")
        conn.commit()
        record_gap(Detection(mode="scan", player="chromium",
                             artist="System Of A Down", title="B.Y.O.B.",
                             url="https://youtu.be/x", album="Mezmerize"), conn)
        rows = conn.execute("SELECT album FROM tracks").fetchall()
        assert len(rows) == 1                 # updated, not duplicated
        assert rows[0]["album"] == "Mezmerize"
    finally:
        conn.close()


def test_a_blank_album_does_not_erase_a_known_one(tmp_path):
    """A player that omits the album must not wipe what another one supplied."""
    from karaoke import localcache
    from karaoke.detect import Detection, record_gap

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.execute("INSERT INTO tracks (artist, title, album)"
                     " VALUES ('A', 'B', 'Known Album')")
        conn.commit()
        record_gap(Detection(mode="scan", player="vlc", artist="A", title="B",
                             url="https://youtu.be/x", album=""), conn)
        assert conn.execute("SELECT album FROM tracks").fetchone()["album"] == "Known Album"
    finally:
        conn.close()


def test_a_detection_carries_the_duration():
    """Without it the deduplicator's duration guard abstains on every pair."""
    meta = PlayerMetadata(artist="A", title="B", album="C", url="",
                          player="vlc", mpris_name="vlc", duration=205.5)
    assert classify(meta).duration == 205.5


def test_playing_a_track_fills_in_a_missing_duration(tmp_path):
    from karaoke import localcache
    from karaoke.detect import Detection, record_gap

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.execute("INSERT INTO tracks (artist, title, duration)"
                     " VALUES ('A', 'B', NULL)")
        conn.commit()
        record_gap(Detection(mode="scan", player="vlc", artist="A", title="B",
                             url="https://youtu.be/x", duration=205.5), conn)
        rows = conn.execute("SELECT duration FROM tracks").fetchall()
        assert len(rows) == 1 and rows[0]["duration"] == 205.5
    finally:
        conn.close()


def test_an_unknown_duration_does_not_erase_a_known_one(tmp_path):
    from karaoke import localcache
    from karaoke.detect import Detection, record_gap

    conn = localcache.connect(tmp_path / "t.db")
    try:
        conn.execute("INSERT INTO tracks (artist, title, duration)"
                     " VALUES ('A', 'B', 300.0)")
        conn.commit()
        record_gap(Detection(mode="scan", player="vlc", artist="A", title="B",
                             url="https://youtu.be/x", duration=None), conn)
        assert conn.execute("SELECT duration FROM tracks").fetchone()["duration"] == 300.0
    finally:
        conn.close()
