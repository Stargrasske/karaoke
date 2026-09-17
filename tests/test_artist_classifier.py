"""Tests for the Artist Classifier, Broad Genres taxonomy, and Dual-Signal Filtering."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from karaoke import artist_classifier, librarysearch, localcache
from karaoke.artist_classifier import (
    BROAD_GENRES,
    CURATED_SEED_ARTISTS,
    classify_artist,
    classify_subgenre_to_broad,
    normalize_artist,
    seed_database,
)


def test_normalize_artist():
    assert normalize_artist("  Django Reinhardt  ") == "django reinhardt"
    assert normalize_artist("B.B. King") == "b.b. king"
    assert normalize_artist("The   Rolling   Stones") == "the rolling stones"
    assert normalize_artist("“The Beatles”") == '"the beatles"'
    assert normalize_artist("Bob Marley & The Wailers") == "bob marley & the wailers"
    assert normalize_artist("") == ""


def test_classify_subgenre_to_broad_direct_mappings():
    assert classify_subgenre_to_broad("delta blues") == ["blues"]
    assert classify_subgenre_to_broad("chicago blues") == ["blues"]
    assert classify_subgenre_to_broad("electric blues") == ["blues"]
    assert classify_subgenre_to_broad("gypsy jazz") == ["jazz"]
    assert classify_subgenre_to_broad("bebop") == ["jazz"]
    assert classify_subgenre_to_broad("shoegaze") == ["rock"]
    assert classify_subgenre_to_broad("death metal") == ["metal"]
    assert classify_subgenre_to_broad("trip hop") == ["electronic", "hip hop"]
    assert classify_subgenre_to_broad("bluegrass") == ["country", "folk"]


def test_classify_subgenre_to_broad_canonical_pass_through():
    for g in BROAD_GENRES:
        assert g in classify_subgenre_to_broad(g)


def test_classify_subgenre_to_broad_stem_heuristics():
    assert "blues" in classify_subgenre_to_broad("contemporary acoustic blues")
    assert "jazz" in classify_subgenre_to_broad("avant-garde jazz swing")
    assert "metal" in classify_subgenre_to_broad("symphonic speed metal")
    assert "punk" in classify_subgenre_to_broad("skate punk")
    assert "electronic" in classify_subgenre_to_broad("minimal techno house")


def test_curated_seed_artists():
    assert "django reinhardt" in CURATED_SEED_ARTISTS
    spec, broad = CURATED_SEED_ARTISTS["django reinhardt"]
    assert "jazz" in broad
    assert "blues" not in broad
    assert "gypsy jazz" in spec

    spec, broad = CURATED_SEED_ARTISTS["b.b. king"]
    assert "blues" in broad
    assert "electric blues" in spec

    spec, broad = CURATED_SEED_ARTISTS["eric clapton"]
    assert "blues" in broad
    assert "rock" in broad


def test_classify_artist_curated_offline():
    spec, broad = classify_artist("Django Reinhardt", online=False)
    assert broad == ["jazz"]
    assert "gypsy jazz" in spec

    spec, broad = classify_artist("Muddy Waters", online=False)
    assert broad == ["blues"]

    spec, broad = classify_artist("Black Sabbath", online=False)
    assert "metal" in broad
    assert "rock" in broad


def test_classify_artist_collaborations_and_ensembles():
    # Hot Club ensemble
    spec, broad = classify_artist(
        "Django Reinhardt & le quintette du Hot club de France", online=False
    )
    assert broad == ["jazz"]

    # Comma separation
    spec, broad = classify_artist(
        "Django Reinhardt, Tony Proteau & son orchestre", online=False
    )
    assert broad == ["jazz"]

    # Parenthetical ensemble
    spec, broad = classify_artist(
        "Janis Joplin (with the kozmic blues band)", online=False
    )
    assert "blues" in broad
    assert "rock" in broad


def test_classify_artist_musicbrainz_mock():
    mb_response = {
        "artists": [
            {
                "id": "12345",
                "name": "Sonny Boy Williamson",
                "tags": [
                    {"name": "chicago blues", "count": 10},
                    {"name": "harmonica blues", "count": 8},
                ],
            }
        ]
    }
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(mb_response).encode("utf-8")
    fake_resp.__enter__.return_value = fake_resp

    with patch("urllib.request.urlopen", return_value=fake_resp):
        spec, broad = classify_artist("Sonny Boy Williamson II", online=True)
        assert "chicago blues" in spec
        assert "blues" in broad


def test_seed_database_and_localcache_integration(tmp_path):
    db_path = tmp_path / "test_artist_genres.db"
    conn = localcache.connect(db_path)
    try:
        count = seed_database(conn)
        assert count > 50

        # Query back via localcache.get_artist_genres
        spec, broad = localcache.get_artist_genres("Django Reinhardt", conn)
        assert broad == ["jazz"]
        assert "gypsy jazz" in spec

        # Query whole map
        genres_map = localcache.get_all_artist_genres_map(conn)
        assert "django reinhardt" in genres_map
        assert genres_map["django reinhardt"]["broad"] == ["jazz"]
        assert "b.b. king" in genres_map
        assert genres_map["b.b. king"]["broad"] == ["blues"]
    finally:
        conn.close()


def test_search_dual_signal_artist_consensus_vs_title(tmp_path):
    """Ensure Django Reinhardt's 'Django's blues' is NOT filtered as Blues,
    is found when searching by title query 'django blues',
    and unclassified acoustic CLAP blues tracks ARE found under Blues.
    """
    db_path = tmp_path / "test_dual_signal.db"
    conn = localcache.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO tracks (track_id, artist, title, album, duration) VALUES
                (1, 'Django Reinhardt', 'Django''s blues', 'The Classic Sessions', 180),
                (2, 'Django Reinhardt', 'Minor Swing', 'The Classic Sessions', 190),
                (3, 'Muddy Waters', 'Mannish Boy', 'The Blues Anthology', 210),
                (4, 'Unknown Busker', 'Corner Blues', 'The Street Tapes', 150);

            -- Audio CLAP acoustic signal
            INSERT INTO track_genre (track_id, genre, score, labelled_at) VALUES
                (1, 'blues', 0.88, 1),
                (2, 'jazz', 0.95, 1),
                (3, 'punk rock', 0.70, 1),  -- misclassified by audio CLAP!
                (4, 'blues', 0.85, 1);       -- acoustic fallback

            -- Artist consensus genres
            INSERT INTO artist_genres (artist_normalized, genre, broad_genre, weight, source, fetched_at) VALUES
                ('django reinhardt', 'gypsy jazz', 'jazz', 1.0, 'seed', 1),
                ('muddy waters', 'chicago blues', 'blues', 1.0, 'seed', 1);
        """)
        conn.commit()

        # 1. Filter by Blues:
        # - Track 1 (Django): excluded because artist consensus is Jazz (not Blues).
        # - Track 3 (Muddy Waters): included because artist consensus is Blues (overriding CLAP's misclassification).
        # - Track 4 (Unknown Busker): included via acoustic CLAP fallback.
        blues_hits = librarysearch.search("the", conn, genre="blues")
        blues_ids = {h.track_id for h in blues_hits}
        assert 1 not in blues_ids, "Django Reinhardt must not appear in Blues genre filter"
        assert 3 in blues_ids, "Muddy Waters must appear in Blues genre filter via artist consensus"
        assert 4 in blues_ids, "Unknown Busker must appear in Blues genre filter via CLAP audio fallback"

        # 2. Filter by Jazz:
        # - Track 1 (Django): included because artist consensus is Jazz.
        # - Track 2 (Django): included because artist consensus is Jazz.
        jazz_hits = librarysearch.search("the", conn, genre="jazz")
        jazz_ids = {h.track_id for h in jazz_hits}
        assert 1 in jazz_ids
        assert 2 in jazz_ids
        assert 3 not in jazz_ids
        assert 4 not in jazz_ids

        # 3. Search query "django blues" with no genre filter:
        # Full text query matching track title and artist
        query_hits = librarysearch.search("django blues", conn)
        assert len(query_hits) > 0
        assert query_hits[0].track_id == 1
        assert "title" in query_hits[0].fields
        assert "artist" in query_hits[0].fields
    finally:
        conn.close()
