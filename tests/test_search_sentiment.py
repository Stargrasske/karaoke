from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from karaoke import osclient, search, vector_index
from karaoke.tasks import PostprocessContext, resolve_track_id
from karaoke.postprocess_worker import run_download_logic


def test_build_track_doc_includes_sentiment() -> None:
    row = {
        "track_id": 42,
        "source_url": "https://example.com/song.mp3",
        "source_kind": "local",
        "player_name": "",
        "title": "Happy Song",
        "artist": "Joyful Singer",
        "album": "Great Joy",
        "duration": 180.0,
        "lyrics_source": "lrclib",
        "synced_lyrics": "[00:10.00] happy sunshine joy smile\n[00:20.00] dancing celebrating",
        "plain_lyrics": "happy sunshine joy smile\ndancing celebrating",
    }
    doc = vector_index.build_track_doc(row, embed=False)
    assert doc["track_id"] == 42
    assert doc["dominant_mood"] == "happy"
    assert doc["sentiment_hits"] > 0
    assert isinstance(doc["sentiment_vector"], list)
    assert len(doc["sentiment_vector"]) == 4
    # (happy, sad, angry, tender) -> happy share should be 1.0
    assert doc["sentiment_vector"][0] == 1.0


def test_build_track_doc_no_lyrics_fallback() -> None:
    row = {
        "track_id": 99,
        "source_url": "",
        "source_kind": "sqlite",
        "player_name": "",
        "title": "Instrumental",
        "artist": "Band",
        "album": None,
        "duration": 200.0,
        "lyrics_source": None,
        "synced_lyrics": None,
        "plain_lyrics": None,
    }
    doc = vector_index.build_track_doc(row, embed=False)
    assert doc["dominant_mood"] == "neutral"
    assert doc["sentiment_hits"] == 0
    assert doc["sentiment_vector"] == [0.0, 0.0, 0.0, 0.0]


def test_osclient_mapping_contains_sentiment() -> None:
    body = osclient.index_body()
    props = body["mappings"]["properties"]
    assert "dominant_mood" in props
    assert props["dominant_mood"]["type"] == "keyword"
    assert "sentiment_vector" in props
    assert props["sentiment_vector"]["dimension"] == 4


def test_semantic_search_with_mood_filter() -> None:
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "hits": {
            "hits": [
                {
                    "_score": 0.95,
                    "_source": {
                        "artist": "Joyful Singer",
                        "title": "Happy Song",
                        "album": "Great Joy",
                        "source": "sqlite",
                        "has_synced": True,
                        "dominant_mood": "happy",
                    },
                }
            ]
        }
    }

    with patch("karaoke.embed.embed_text", return_value=[0.1] * 384):
        hits = search.semantic_search("sunny day", k=3, mood="happy", os_client=mock_client)

    assert len(hits) == 1
    assert hits[0].dominant_mood == "happy"
    mock_client.search.assert_called_once()
    body = mock_client.search.call_args[1]["body"]
    knn = body["query"]["knn"]["lyrics_vector"]
    assert knn["filter"]["bool"]["minimum_should_match"] == 1
    assert {"term": {"dominant_mood": "happy"}} in knn["filter"]["bool"]["should"]
    assert {"term": {"dominant_mood.keyword": "happy"}} in knn["filter"]["bool"]["should"]


def test_keyword_search_with_mood_filter() -> None:
    mock_client = MagicMock()
    mock_client.search.return_value = {"hits": {"hits": []}}

    search.keyword_search("sunshine", k=5, mood="happy", os_client=mock_client)
    mock_client.search.assert_called_once()
    body = mock_client.search.call_args[1]["body"]
    pos = body["query"]["boosting"]["positive"]
    assert "bool" in pos
    f = pos["bool"]["filter"][0]["bool"]
    assert f["minimum_should_match"] == 1
    assert {"term": {"dominant_mood": "happy"}} in f["should"]
    assert {"term": {"dominant_mood.keyword": "happy"}} in f["should"]


def test_hybrid_search_query_shape() -> None:
    mock_client = MagicMock()
    mock_client.search.return_value = {"hits": {"hits": []}}

    with patch("karaoke.embed.embed_text", return_value=[0.2] * 384):
        search.hybrid_search("summer fun", k=4, mood="tender", os_client=mock_client)

    mock_client.search.assert_called_once()
    body = mock_client.search.call_args[1]["body"]
    b = body["query"]["bool"]
    f = b["filter"][0]["bool"]
    assert f["minimum_should_match"] == 1
    assert {"term": {"dominant_mood": "tender"}} in f["should"]
    assert {"term": {"dominant_mood.keyword": "tender"}} in f["should"]
    should = b["should"]
    assert len(should) == 2  # keyword boosting and knn


def test_run_download_logic_handles_existing_file(tmp_path: Path) -> None:
    audio_file = tmp_path / "song.mp3"
    audio_file.write_bytes(b"dummy audio content")

    resolved = run_download_logic(str(audio_file), cookies_from_browser=None)
    assert resolved == audio_file


def test_resolve_track_id_handles_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from karaoke import localcache

    db_path = tmp_path / "test.db"
    audio_file = tmp_path / "local_tune.mp3"
    audio_file.write_bytes(b"audio data")

    test_conn = localcache.connect(db_path)
    tid = localcache.add_track_source("Test Artist", "Test Title", url=str(audio_file), kind="local", conn=test_conn)

    # Make resolve_track_id use the test database
    monkeypatch.setattr(localcache, "connect", lambda *args, **kwargs: test_conn)

    payload = {"artist": "Test Artist", "title": "Test Title", "url": str(audio_file)}
    res = resolve_track_id.run(payload)
    assert res["track_id"] == tid
    assert res["audio_path"] == str(audio_file)
    assert res["url"] == str(audio_file)
    test_conn.close()
