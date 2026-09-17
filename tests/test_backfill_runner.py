"""Tests for the backfill runner."""
from unittest.mock import patch, MagicMock
from karaoke import backfill_runner

@patch("karaoke.backfill_runner.localcache.connect")
def test_run_processes_gaps(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    mock_conn.__enter__.return_value.execute.return_value.fetchall.return_value = [
        {'gap_id': 1, 'artist': 'A', 'title': 'B'}
    ]

    with patch("karaoke.backfill_runner._process_gap") as mock_process_gap:
        backfill_runner.run()

    assert mock_process_gap.call_count == 1
    mock_process_gap.assert_called_with(1, 'A', 'B')


@patch("karaoke.backfill_runner.localcache.connect")
def test_run_default_selects_only_pending(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    execute = mock_conn.__enter__.return_value.execute
    execute.return_value.fetchall.return_value = []

    backfill_runner.run()

    sql, params = execute.call_args[0]
    assert "status IN (%s)" in sql
    assert params == ["pending"]


@patch("karaoke.backfill_runner.localcache.connect")
def test_run_retry_failed_includes_failed_gaps(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    execute = mock_conn.__enter__.return_value.execute
    execute.return_value.fetchall.return_value = []

    backfill_runner.run(retry_failed=True, limit=5)

    sql, params = execute.call_args[0]
    assert "status IN (%s,%s)" in sql
    assert params == ["pending", "failed", 5]


@patch("karaoke.backfill_runner._update_gap_status")
@patch("karaoke.backfill_runner.localcache.connect")
def test_run_records_error_on_failure(mock_connect, mock_update):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    mock_conn.__enter__.return_value.execute.return_value.fetchall.return_value = [
        {'gap_id': 7, 'artist': 'A', 'title': 'B'}
    ]

    with patch("karaoke.backfill_runner._process_gap",
               side_effect=RuntimeError("boom")):
        backfill_runner.run()

    mock_update.assert_called_once_with(7, 'failed', error="RuntimeError: boom")


@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_prefers_lrclib(mock_lrclib):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics(plain="line one\nline two", source="lrclib")
    assert backfill_runner._find_lyrics("Artist", "Song").plain == "line one\nline two"


@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_preserves_lrclib_timings(mock_lrclib):
    """The LRC must survive: it is what lets us skip download + Whisper."""
    from karaoke.lyrics import Lyrics
    lrc = "[00:01.00] line one\n[00:05.00] line two"
    mock_lrclib.return_value = Lyrics(plain="line one\nline two", synced_raw=lrc,
                                      source="lrclib")
    assert backfill_runner._find_lyrics("Artist", "Song").synced_raw == lrc


@patch("karaoke.backfill_runner.web.fetch_genius_lyrics")
@patch("karaoke.backfill_runner.web.search")
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_falls_back_to_genius(mock_lrclib, mock_search, mock_genius):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()  # LRCLIB miss
    mock_search.return_value = [
        {"url": "https://genius.com/Artist-song-lyrics", "title": "x"}
    ]
    body = "\n".join(f"genius line {i}" for i in range(40))
    mock_genius.return_value = body
    ly = backfill_runner._find_lyrics("Artist", "Song")
    assert ly.plain == body
    assert ly.synced_raw == ""   # Genius has no timings
    mock_genius.assert_called_once()


@patch("karaoke.backfill_runner.web.search", return_value=[])
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_returns_empty_when_all_miss(mock_lrclib, _mock_search):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()
    ly = backfill_runner._find_lyrics("Artist", "Song")
    assert not (ly.plain or ly.synced_raw)


@patch("karaoke.backfill_runner._find_lyrics")
def test_process_gap_raises_without_lyrics(mock_find):
    import pytest
    from karaoke.lyrics import Lyrics
    mock_find.return_value = Lyrics()
    with pytest.raises(RuntimeError, match="No lyrics found"):
        backfill_runner._process_gap(1, "A", "B")


@patch("karaoke.backfill_runner._verify_stored")
@patch("karaoke.backfill_runner.localcache.add_track_and_lyrics")
@patch("karaoke.backfill_runner.localcache.connect")
@patch("karaoke.backfill_runner.youtube.search")
@patch("karaoke.backfill_runner._find_lyrics")
def test_process_gap_stores_synced_without_downloading(
    mock_find, mock_yt, _mock_connect, mock_store, _mock_verify
):
    """Already-timed lyrics must skip YouTube download and Whisper entirely."""
    from karaoke.lyrics import Lyrics
    lrc = "[00:01.00] line one\n[00:05.00] line two"
    mock_find.return_value = Lyrics(plain="line one\nline two", synced_raw=lrc,
                                    source="lrclib")

    backfill_runner._process_gap(1, "A", "B")

    mock_yt.assert_not_called()          # no YouTube search
    mock_store.assert_called_once()
    assert mock_store.call_args[0][2].synced_raw == lrc


@patch("karaoke.backfill_runner._verify_stored")
@patch("karaoke.backfill_runner.get_synced")
@patch("karaoke.backfill_runner.youtube.download", return_value="/tmp/a.webm")
@patch("karaoke.backfill_runner.youtube.search")
@patch("karaoke.backfill_runner._find_lyrics")
def test_process_gap_transcribes_when_only_plain_text(
    mock_find, mock_yt, _mock_dl, mock_get_synced, _mock_verify
):
    """Plain-text-only sources still need Whisper alignment against audio."""
    from karaoke.lyrics import Lyrics
    mock_find.return_value = Lyrics(plain="line one\nline two", source="genius")
    mock_yt.return_value = [{"url": "https://youtu.be/x"}]

    backfill_runner._process_gap(1, "A", "B")

    mock_yt.assert_called_once()
    assert mock_get_synced.call_args.kwargs["force_transcribe"] is True


# --- Genius fallback validation -------------------------------------------

def test_genius_song_url_rejects_index_pages():
    ok = backfill_runner._is_genius_song_url
    assert ok("https://genius.com/Fugazi-waiting-room-lyrics")
    assert not ok("https://genius.com/")
    assert not ok("https://genius.com/artists/Culprit")
    assert not ok("https://genius.com/albums/Igorrr/Amen")


def test_genius_url_matches_rejects_other_artists_same_title():
    """Search returns a different artist's song of the same name."""
    m = backfill_runner._genius_url_matches
    assert not m("https://genius.com/J-cole-life-sentence-lyrics",
                 "Seven Hells", "Life Sentence")
    assert not m("https://genius.com/Reese-lansangan-mall-rats-lyrics",
                 "Dead Mall", "RATS")


def test_genius_url_matches_accepts_real_matches():
    m = backfill_runner._genius_url_matches
    assert m("https://genius.com/Fugazi-waiting-room-lyrics", "Fugazi", "Waiting Room")
    assert m("https://genius.com/The-slits-i-heard-it-through-the-grapevine-lyrics",
             "The Slits", "I Heard It Through The Grapevine")
    assert m("https://genius.com/Red-hot-chili-peppers-suck-my-kiss-lyrics",
             "Red Hot Chili Peppers", "Suck My Kiss")


@patch("karaoke.backfill_runner.web.fetch_genius_lyrics")
@patch("karaoke.backfill_runner.web.search")
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_rejects_too_short_genius_text(mock_lrclib, mock_search, mock_genius):
    """A stub/banner parse must not be stored as a song's lyrics."""
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()
    mock_search.return_value = [
        {"url": "https://genius.com/Fugazi-waiting-room-lyrics", "title": "x"}
    ]
    mock_genius.return_value = "Waiting Room Lyrics"     # far too short
    assert backfill_runner._find_lyrics("Fugazi", "Waiting Room").plain == ""


@patch("karaoke.backfill_runner.web.fetch_genius_lyrics")
@patch("karaoke.backfill_runner.web.search")
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_accepts_long_enough_genius_text(mock_lrclib, mock_search, mock_genius):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()
    mock_search.return_value = [
        {"url": "https://genius.com/Fugazi-waiting-room-lyrics", "title": "x"}
    ]
    body = "\n".join(f"lyric line number {i}" for i in range(40))
    mock_genius.return_value = body
    assert backfill_runner._find_lyrics("Fugazi", "Waiting Room").plain == body


@patch("karaoke.backfill_runner.web.fetch_genius_lyrics")
@patch("karaoke.backfill_runner.web.search")
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_skips_unrelated_genius_result(mock_lrclib, mock_search, mock_genius):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()
    mock_search.return_value = [
        {"url": "https://genius.com/J-cole-life-sentence-lyrics", "title": "x"}
    ]
    assert backfill_runner._find_lyrics("Seven Hells", "Life Sentence").plain == ""
    mock_genius.assert_not_called()   # never even fetched
