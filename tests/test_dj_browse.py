"""Tests for the DJ booth's library browsing and playlist commands."""
from __future__ import annotations

import pytest

from karaoke.dj_chat import DJChatSession


@pytest.fixture
def session() -> DJChatSession:
    return DJChatSession()


def test_parse_browse_reads_filter_tokens(session):
    kwargs, _ = session._parse_browse("genre=rock key=8A bpm=120-140 synced=yes")
    assert kwargs["genre"] == "rock"
    assert kwargs["key"] == "8A"
    assert kwargs["min_bpm"] == 120.0
    assert kwargs["max_bpm"] == 140.0
    assert kwargs["only_synced_lyrics"] is True


def test_parse_browse_accepts_open_ended_bpm(session):
    lower, _ = session._parse_browse("bpm=140-")
    assert lower["min_bpm"] == 140.0
    assert "max_bpm" not in lower

    upper, _ = session._parse_browse("bpm=-90")
    assert upper["max_bpm"] == 90.0
    assert "min_bpm" not in upper


def test_parse_browse_treats_unknown_tokens_as_free_text(session):
    """`/browse bowie` has to keep working, and so does a typo'd filter."""
    kwargs, query = session._parse_browse("bowie colour=blue")
    assert query == "bowie colour=blue"
    assert kwargs["query"] == "bowie colour=blue"
    assert "genre" not in kwargs


def test_parse_browse_rejects_non_numeric_bpm(session):
    """A bad bpm value must not raise -- it falls back to free text."""
    kwargs, query = session._parse_browse("bpm=fast")
    assert "min_bpm" not in kwargs
    assert "bpm=fast" in query


def test_bare_playlist_still_shows_the_dj_list(session, monkeypatch):
    """`/playlist` meant the DJ list before named playlists existed."""
    called = []
    monkeypatch.setattr(session, "cmd_dj_list", lambda: called.append("dj") or "dj")
    monkeypatch.setattr(session, "cmd_open_playlist", lambda a: called.append("open") or "open")

    session.ask("/playlist")
    assert called == ["dj"]


def test_playlist_with_argument_opens_that_playlist(session, monkeypatch):
    seen = []
    monkeypatch.setattr(session, "cmd_dj_list", lambda: seen.append("dj") or "dj")
    monkeypatch.setattr(session, "cmd_open_playlist", lambda a: seen.append(a) or "open")

    session.ask("/playlist Smoking")
    assert seen == ["Smoking"]


def test_browse_more_advances_the_page(session, monkeypatch):
    """`more` continues the previous filters instead of restarting."""
    calls = []

    def fake_search(**kwargs):
        calls.append(kwargs)
        return '{"count": 0, "offset": 0, "has_more": false, "tracks": []}'

    monkeypatch.setattr("karaoke.mcp_server.search_songs", fake_search)

    session.cmd_browse("genre=rock")
    session.cmd_browse("more")

    assert calls[0]["offset"] == 0
    assert calls[1]["offset"] == session._BROWSE_PAGE
    # The filter has to survive into the second page.
    assert calls[1]["genre"] == "rock"


def test_browse_more_without_a_previous_browse(session):
    assert "Nothing to continue" in session.cmd_browse("more")


def test_open_unknown_playlist_reports_rather_than_raises(session):
    out = session.cmd_open_playlist("definitely-not-a-playlist-xyz")
    assert "No saved playlist" in out
