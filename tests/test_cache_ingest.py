"""Tests for the unified YouTube cache ingestion and enrichment pipeline."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from karaoke import cache_ingest, localcache
from karaoke.identify import SongRef
from karaoke.musictheory import parse_key


class FakeGenreVerdict:
    def __init__(self, genre="pop", score=0.85, runner_up="rock", runner_up_score=0.7):
        self.genre = genre
        self.score = score
        self.runner_up = runner_up
        self.runner_up_score = runner_up_score
        self.clear = True
        self.confident = True


class FakeAudioAnalysis:
    def __init__(self):
        self.key = parse_key("A minor")
        self.key_confidence = 0.9
        self.key_agreement = "5/6"
        self.bpm = 120.0
        self.energy = 0.75
        self.brightness = 0.6
        self.method = "librosa"


class FakeOpenSearchClient:
    def __init__(self):
        self.indices = MagicMock()
        self.indices.exists.return_value = True
        self.docs: dict[tuple[str, str], dict] = {}

    def search(self, index, body):
        hits = []
        for (idx, doc_id), data in self.docs.items():
            if idx == index:
                hits.append({"_source": data, "_id": doc_id})
        return {"hits": {"hits": hits}}

    def index(self, index, id, body):
        self.docs[(index, id)] = dict(body)
        return {"result": "created"}

    def update(self, index, id, body):
        doc = self.docs.setdefault((index, id), {})
        doc.update(body.get("doc", {}))
        return {"result": "updated"}

    def get(self, index, id, **kwargs):
        doc = self.docs.get((index, id))
        if not doc:
            raise KeyError(f"Not found: {index}/{id}")
        return {"_source": doc}


def test_process_youtube_cache_empty_dir(tmp_path):
    """Empty or missing directory returns zero counts."""
    stats = cache_ingest.process_youtube_cache(yt_dir=tmp_path / "nonexistent")
    assert stats.total_files == 0
    assert stats.new_tracks == 0


def test_process_youtube_cache_e2e(tmp_path, monkeypatch):
    """End-to-end cache ingestion, audio analysis, CLAP embedding, and genre tagging."""
    # 1. Setup temporary directories and files
    cache_dir = tmp_path / "youtube"
    cache_dir.mkdir()
    audio_file = cache_dir / "dQw4w9WgXcQ.webm"
    audio_file.write_bytes(b"dummy audio content")

    # 2. Setup temporary SQLite DB
    db_file = tmp_path / "karaoke.db"
    conn = localcache.connect(db_file)

    # 3. Setup Fake OpenSearch client
    fake_os = FakeOpenSearchClient()

    # 4. Mock upstream components
    monkeypatch.setattr(
        cache_ingest,
        "resolve_youtube",
        lambda url, download=False: SongRef(artist="Rick Astley", title="Never Gonna Give You Up", duration=213.0),
    )
    monkeypatch.setattr(
        cache_ingest.analyze,
        "analyze_audio",
        lambda path: FakeAudioAnalysis(),
    )
    monkeypatch.setattr(cache_ingest.clap_vector, "available", lambda: True)
    monkeypatch.setattr(cache_ingest.clap_vector, "embed_audio", lambda path: [0.1] * 512)
    monkeypatch.setattr(cache_ingest.genre, "label_vectors", lambda: {"rock": [0.1] * 512, "pop": [0.05] * 512})
    monkeypatch.setattr(cache_ingest.genre, "classify", lambda vec, labels: FakeGenreVerdict("pop", 0.9, "rock"))

    progress_reports = []
    def on_progress(stage, cur, total, detail):
        progress_reports.append((stage, detail))

    # 5. Run first pass
    stats = cache_ingest.process_youtube_cache(
        yt_dir=cache_dir,
        conn=conn,
        os_client=fake_os,
        embed_clap=True,
        classify_genres=True,
        analyze_audio_features=True,
        enqueue_postprocessing=False,
        progress_callback=on_progress,
    )

    assert stats.total_files == 1
    assert stats.new_tracks == 1
    assert stats.analyzed == 1
    assert stats.clap_embedded == 1
    assert stats.genres_labeled == 1
    assert stats.errors == 0
    assert len(progress_reports) > 0

    # 6. Verify SQLite tables
    cur = conn.cursor()
    cur.execute("SELECT track_id, artist, title FROM tracks")
    track = cur.fetchone()
    assert track["artist"] == "Rick Astley"
    assert track["title"] == "Never Gonna Give You Up"
    track_id = track["track_id"]

    cur.execute("SELECT url, kind FROM sources WHERE track_id = %s", (track_id,))
    source = cur.fetchone()
    assert "dQw4w9WgXcQ" in source["url"]
    assert source["kind"] == "youtube"

    cur.execute("SELECT bpm, detected_key FROM track_analysis WHERE track_id = %s", (track_id,))
    analysis = cur.fetchone()
    assert analysis["bpm"] == 120.0
    assert analysis["detected_key"] == "A minor"

    cur.execute("SELECT genre FROM track_genre WHERE track_id = %s", (track_id,))
    genre_row = cur.fetchone()
    assert genre_row["genre"] == "pop"

    # 7. Verify OpenSearch documents
    clap_doc = fake_os.docs.get(("karaoke-clap", f"clap:{track_id}"))
    assert clap_doc is not None
    assert clap_doc["artist"] == "Rick Astley"
    assert clap_doc["genre"] == "pop"

    # 8. Run second pass (idempotency / cumulative check)
    stats2 = cache_ingest.process_youtube_cache(
        yt_dir=cache_dir,
        conn=conn,
        os_client=fake_os,
        embed_clap=True,
        classify_genres=True,
        analyze_audio_features=True,
        enqueue_postprocessing=False,
    )

    # In second run, nothing new should be ingested or re-analyzed
    assert stats2.total_files == 1
    assert stats2.skipped == 1
    assert stats2.new_tracks == 0
    assert stats2.analyzed == 0
    assert stats2.clap_embedded == 0
    assert stats2.genres_labeled == 0

    conn.close()
