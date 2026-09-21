"""Tests for SQLite-derived OpenSearch vector indexing."""
from __future__ import annotations

from pathlib import Path

from karaoke import localcache, vector_index
from karaoke.lyrics import Lyrics


class FakeIndices:
    def __init__(self):
        self.created = []
        self.created_bodies = {}
        self.refreshed = []

    def exists(self, index):
        return index in self.created

    def create(self, index, body):
        self.created.append(index)
        self.created_bodies[index] = body

    def refresh(self, index):
        self.refreshed.append(index)


class FakeOpenSearch:
    def __init__(self):
        self.indices = FakeIndices()
        self.docs = []
        self.bulk_calls = 0

    def index(self, index, id, body):
        self.docs.append((index, id, body))

    def bulk(self, body, **kwargs):
        # body is [action_meta, source, action_meta, source, ...]; record each
        # (index, id, source) in order so tests can assert on document shape.
        self.bulk_calls += 1
        items = []
        for i in range(0, len(body), 2):
            meta = body[i]["index"]
            source = body[i + 1]
            self.docs.append((meta["_index"], meta["_id"], source))
            items.append({"index": {"status": 201}})
        return {"errors": False, "items": items}


def _seed_db(path: Path) -> None:
    conn = localcache.connect(path)
    try:
        ly = Lyrics(
            plain="first line\nsecond line",
            synced_raw="[00:01.00] first line\n[00:04.00] second line",
            source="whisper",
        )
        localcache.add_track_and_lyrics(
            "Tom Waits",
            "Army Ants",
            ly,
            album="Real Gone",
            duration=210.0,
            url="https://www.youtube.com/watch?v=_3tkup9b-iM",
            conn=conn,
        )
    finally:
        conn.close()


def test_build_track_doc_from_sqlite(tmp_path):
    db = tmp_path / "karaoke.db"
    _seed_db(db)
    conn = localcache.connect(db)
    try:
        rows = list(vector_index.iter_track_rows(conn))
    finally:
        conn.close()

    assert len(rows) == 1
    doc = vector_index.build_track_doc(rows[0], embed=False)
    assert doc["track_id"] == 1
    assert doc["artist"] == "Tom Waits"
    assert doc["title"] == "Army Ants"
    assert doc["source"] == "sqlite"
    assert doc["source_kind"] == "youtube"
    assert doc["source_url"] == "https://www.youtube.com/watch?v=_3tkup9b-iM"
    assert doc["has_synced"] is True
    assert doc["lyrics_source"] == "whisper"
    assert "lyrics_vector" not in doc


def test_rebuild_from_sqlite_indexes_tracks_and_lines(tmp_path):
    db = tmp_path / "karaoke.db"
    _seed_db(db)
    fake = FakeOpenSearch()

    stats = vector_index.rebuild_from_sqlite(
        db_path=str(db),
        embed=False,
        include_lines=True,
        os_client=fake,
    )

    assert stats.seen == 1
    assert stats.indexed == 1
    assert stats.errors == 0
    assert stats.line_docs == 2
    indexes = [item[0] for item in fake.docs]
    assert indexes == ["tracks", "tracks-lines", "tracks-lines"]
    assert fake.docs[0][1] == "sqlite:1"
    assert fake.docs[1][1] == "sqlite-line:1:0"
    assert fake.docs[1][2]["duration_s"] == 3.0
    assert fake.docs[2][2]["duration_s"] is None
    line_mapping = fake.indices.created_bodies["tracks-lines"]["mappings"]["properties"]
    assert line_mapping["text"]["search_analyzer"] == "synonym_analyzer"
    assert line_mapping["context"]["search_analyzer"] == "synonym_analyzer"




def test_rebuild_batches_writes_into_bulk_requests(tmp_path):
    """All docs for a small library go out in a single _bulk call, not per-doc."""
    db = tmp_path / "karaoke.db"
    _seed_db(db)
    fake = FakeOpenSearch()

    stats = vector_index.rebuild_from_sqlite(
        db_path=str(db),
        embed=False,
        include_lines=True,
        os_client=fake,
        chunk_size=500,
    )

    # 1 track + 2 lines, comfortably under one chunk -> exactly one bulk request.
    assert fake.bulk_calls == 1
    assert stats.indexed == 1
    assert stats.line_docs == 2
    assert stats.errors == 0
    # Per-doc index() must no longer be used on the write path.
    assert len(fake.docs) == 3


def test_rebuild_flushes_multiple_chunks_when_over_chunk_size(tmp_path):
    """A chunk_size smaller than the doc count forces multiple bulk flushes."""
    db = tmp_path / "karaoke.db"
    _seed_db(db)
    fake = FakeOpenSearch()

    stats = vector_index.rebuild_from_sqlite(
        db_path=str(db),
        embed=False,
        include_lines=True,
        os_client=fake,
        chunk_size=2,
    )

    # 3 docs at chunk_size=2 -> two flushes (2 + 1).
    assert fake.bulk_calls == 2
    assert stats.errors == 0
    assert len(fake.docs) == 3


def test_track_index_uses_synonym_analyzer_for_keyword_fields():
    from karaoke import osclient

    body = osclient.index_body()
    analysis = body["settings"]["analysis"]
    fields = body["mappings"]["properties"]
    assert "synonym_filter" in analysis["filter"]
    assert "melancholy" in ", ".join(analysis["filter"]["synonym_filter"]["synonyms"])
    assert fields["plain_lyrics"]["search_analyzer"] == "synonym_analyzer"
    assert fields["title"]["search_analyzer"] == "synonym_analyzer"


def test_vector_index_main_dry_run_no_cluster(tmp_path, capsys):
    db = tmp_path / "karaoke.db"
    _seed_db(db)

    rc = vector_index.vector_index_main([
        "--dry-run",
        "--no-embed",
        "--lines",
        "--db",
        str(db),
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run: seen=1" in out
    assert "line_docs=2" in out


def test_progress_read_write(tmp_path):
    p = tmp_path / "progress.json"
    assert vector_index.get_progress(p) is None

    sample = {"status": "running", "percent": 50.0, "total_tracks": 100}
    vector_index._write_progress(sample, p)

    loaded = vector_index.get_progress(p)
    assert loaded is not None
    assert loaded["status"] == "running"
    assert loaded["percent"] == 50.0


def test_vector_index_status_and_format(tmp_path, monkeypatch):
    db = tmp_path / "karaoke.db"
    _seed_db(db)

    class FakeCat:
        def indices(self, **kwargs):
            return [
                {"index": "tracks", "docs.count": "1", "store.size": "10kb", "health": "green", "status": "open"},
                {"index": "tracks-lines", "docs.count": "2", "store.size": "20kb", "health": "green", "status": "open"},
            ]

    fake = FakeOpenSearch()
    fake.cat = FakeCat()

    monkeypatch.setattr("karaoke.lockfile.ProcessLock.is_locked", lambda self: False)
    status = vector_index.vector_index_status(db_path=str(db), os_client=fake)
    assert status["is_running"] is False
    assert status["db_tracks"] == 1
    assert status["os_connected"] is True
    assert status["tracks_indexed"] == 1
    assert status["lines_indexed"] == 2
    assert status["percent_complete"] == 100.0

    report = vector_index.format_status_report(status)
    assert "Vector Index Status:" in report
    assert "SQLite library:  1 tracks" in report
    assert "tracks:       1 docs (100.0% of SQLite library)" in report
    assert "tracks-lines: 2 docs" in report


def test_vector_index_main_status_cli(tmp_path, capsys):
    db = tmp_path / "karaoke.db"
    _seed_db(db)

    class FakeCat:
        def indices(self, **kwargs):
            return [{"index": "tracks", "docs.count": "1"}]

    fake = FakeOpenSearch()
    fake.cat = FakeCat()

    # Plain text status
    rc = vector_index.vector_index_main(["--status", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Vector Index Status:" in out

    # JSON status
    rc = vector_index.vector_index_main(["--status", "--json", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"is_running":' in out
    assert '"db_tracks": 1' in out

