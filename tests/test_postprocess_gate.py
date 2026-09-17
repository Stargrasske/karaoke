"""Only enqueue post-processing the worker could actually complete.

Both task kinds need fetchable audio: analysis needs the file to examine, and
the word-timing upgrade needs YouTube captions. A Spotify URL has neither, so
those tracks were enqueued, failed "no watchable URL", were retried and dropped
-- on every track change, forever.
"""
import pytest

from karaoke import localcache
from karaoke import postprocess_queue as q


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc123",
    "https://youtu.be/abc123",
    "https://www.youtube.com/embed/abc123",
])
def test_youtube_urls_are_downloadable(url):
    assert q.is_downloadable(url) is True


@pytest.mark.parametrize("url", [
    "https://open.spotify.com/track/abc123",
    "spotify:track:abc123",
    "",
])
def test_spotify_and_empty_urls_are_not(url):
    assert q.is_downloadable(url) is False


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "t.db")
    c.execute("""
        INSERT INTO tracks (track_id, artist, title) VALUES
            (1, 'A', 'Spotify Only'), (2, 'B', 'Has YouTube'),
            (3, 'C', 'Local File'), (4, 'D', 'No Sources');
        INSERT INTO sources (track_id, kind, url) VALUES
            (1, 'spotify', 'https://open.spotify.com/track/x'),
            (2, 'youtube', 'https://youtu.be/y'),
            (3, 'local', '/music/z.flac');
    """)
    c.commit()
    yield c
    c.close()


def test_a_spotify_only_track_has_no_downloadable_source(conn):
    assert q.has_downloadable_source(1, conn) is False


def test_a_youtube_source_counts(conn):
    assert q.has_downloadable_source(2, conn) is True


def test_a_local_file_counts(conn):
    """There is a real file to analyse, whatever its url column says."""
    assert q.has_downloadable_source(3, conn) is True


def test_a_track_with_no_sources_has_none(conn):
    assert q.has_downloadable_source(4, conn) is False


# -- the gate itself ----------------------------------------------------

def test_a_spotify_only_track_is_not_enqueued(conn, monkeypatch):
    """The regression: this fired on every track change and always failed."""
    published = []
    monkeypatch.setattr(q, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t)) or True)
    assert q.enqueue_if_needed("A", "Spotify Only",
                               "https://open.spotify.com/track/x", conn) is False
    assert published == []


def test_a_youtube_track_is_still_enqueued(conn, monkeypatch):
    published = []
    monkeypatch.setattr(q, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t)) or True)
    assert q.enqueue_if_needed("B", "Has YouTube", "https://youtu.be/y", conn) is True
    assert published == [("B", "Has YouTube")]


def test_a_downloadable_url_wins_over_stored_sources(conn, monkeypatch):
    """The caller may know a URL the database does not."""
    published = []
    monkeypatch.setattr(q, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t)) or True)
    assert q.enqueue_if_needed("A", "Spotify Only",
                               "https://youtu.be/new", conn) is True


def test_an_unknown_track_with_a_spotify_url_is_not_enqueued(conn, monkeypatch):
    """Nothing to resolve and nothing to download: it would only fail."""
    published = []
    monkeypatch.setattr(q, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t)) or True)
    assert q.enqueue_if_needed("Unknown", "Track",
                               "https://open.spotify.com/track/z", conn) is False
    assert published == []


def test_an_unknown_track_with_no_url_is_still_enqueued(conn, monkeypatch):
    """The worker can search for it; that path is unchanged."""
    published = []
    monkeypatch.setattr(q, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t)) or True)
    assert q.enqueue_if_needed("Unknown", "Track", "", conn) is True
