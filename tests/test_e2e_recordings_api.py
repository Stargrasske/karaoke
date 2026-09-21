"""End-to-end integration test for recording management & processing APIs.

Exercises the full lifecycle of recordings over the Library & Control APIs:
1. Listing and searching recordings (`GET /api/recordings`)
2. Starting and checking capture status (`POST /api/record/start`, `GET /api/record/status`)
3. Stopping capture (`POST /api/record/stop`)
4. Updating recording metadata (`PATCH /api/recordings/{id}`)
5. Inspecting session details and track slices (`GET /api/recordings/{id}`)
6. Analysing and decompiling recordings into track analysis (`POST /api/recordings/{id}/analyse`)
7. Serving track page & audio (`GET /recordings/{id}/tracks/{idx}`, `GET /api/recordings/{id}/tracks/{idx}/audio`)
8. Discarding audio while preserving markers (`DELETE /api/recordings/{id}/audio`)
9. Verifying the unified `ApiClient` wrapper methods
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from karaoke.api import app as lib_app
from karaoke.api_client import ApiClient
from karaoke.ctrl_api import app as ctrl_app
from karaoke import localcache, recorder, recording_worker


@pytest.fixture()
def lib_client():
    return TestClient(lib_app)


@pytest.fixture()
def ctrl_client():
    return TestClient(ctrl_app)


@pytest.fixture()
def test_db_and_recording(tmp_path, monkeypatch):
    """Set up a test database with a recording session and sample audio files."""
    db = tmp_path / "test_e2e_karaoke.db"
    recording_dir = tmp_path / "recordings" / "rec-20260906-120000"
    recording_dir.mkdir(parents=True, exist_ok=True)

    # Create dummy FLAC segment file
    seg_file = recording_dir / "seg-20260906-120000.flac"
    seg_file.write_bytes(b"FLAC_DUMMY_AUDIO_DATA_FOR_TESTING")

    _real_connect = localcache.connect
    conn = _real_connect(db)

    # Insert test recording row
    now = time.time()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO recordings (recording_id, started_at, ended_at, status, source, dir, keep_audio, note)
        VALUES (99, %s, %s, 'complete', 'alsa_output.pci-0000_00_1f.3.analog-stereo.monitor', %s, 1, 'Initial e2e session note')
        """,
        (now - 300, now, str(recording_dir)),
    )

    # Insert test markers for two tracks
    cur.execute(
        """
        INSERT INTO recording_marks (recording_id, artist, title, at_wall, at_mono, at_offset, ok)
        VALUES
            (99, 'Swans', 'A Little God In My Hands', %s, 10.0, 5.0, 1),
            (99, 'Swans', 'A Little God In My Hands', %s, 20.0, 15.0, 1),
            (99, 'Portishead', 'Glory Box', %s, 60.0, 5.0, 1),
            (99, 'Portishead', 'Glory Box', %s, 70.0, 15.0, 1)
        """,
        (now - 290, now - 280, now - 200, now - 190),
    )
    conn.commit()
    conn.close()

    # Monkeypatch database connection and settings across modules
    monkeypatch.setattr(localcache, "connect", lambda *args, **kwargs: _real_connect(db))
    monkeypatch.setattr("karaoke.api.localcache.connect", lambda *args, **kwargs: _real_connect(db))
    monkeypatch.setattr("karaoke.recording_worker.localcache.connect", lambda *args, **kwargs: _real_connect(db))

    return {
        "db": db,
        "dir": recording_dir,
        "seg_file": seg_file,
        "recording_id": 99,
    }


def test_e2e_recording_lifecycle(test_db_and_recording, lib_client, ctrl_client):
    rec_id = test_db_and_recording["recording_id"]

    # 1. List recordings via Library API with filters
    list_resp = lib_client.get("/api/recordings?limit=10")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["count"] >= 1
    found = [r for r in data["recordings"] if r["recording_id"] == rec_id]
    assert len(found) == 1
    rec = found[0]
    assert rec["status"] == "complete"
    assert rec["marks"] == 4
    assert rec["identified"] == 4
    assert rec["keep_audio"] is True
    assert rec["note"] == "Initial e2e session note"

    # Search filter test
    q_resp = lib_client.get("/api/recordings?q=Initial")
    assert q_resp.status_code == 200
    assert any(r["recording_id"] == rec_id for r in q_resp.json()["recordings"])

    # 2. Inspect session details and track slices via Library API
    detail_resp = lib_client.get(f"/api/recordings/{rec_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["recording_id"] == rec_id
    assert detail["marks"] == 4
    assert detail["identified"] == 4
    assert len(detail["tracks"]) == 2
    track1 = detail["tracks"][0]
    assert track1["artist"] == "Swans"
    assert track1["title"] == "A Little God In My Hands"
    assert track1["confident"] is True

    # Test confident_only filter on details
    confident_resp = lib_client.get(f"/api/recordings/{rec_id}?confident_only=true")
    assert confident_resp.status_code == 200
    assert len(confident_resp.json()["tracks"]) == 2

    # 3. Update recording metadata (note, keep_audio) via Library API
    patch_resp = lib_client.patch(
        f"/api/recordings/{rec_id}",
        json={"note": "Updated e2e session note", "keep_audio": False},
    )
    assert patch_resp.status_code == 200
    patch_data = patch_resp.json()
    assert patch_data["recording_id"] == rec_id
    assert patch_data["note"] == "Updated e2e session note"
    assert patch_data["keep_audio"] is False

    # Verify updated metadata reflected in GET
    updated_rec = lib_client.get(f"/api/recordings/{rec_id}").json()
    assert updated_rec["note"] == "Updated e2e session note"
    assert updated_rec["keep_audio"] is False

    # 4. Control API - Start, check status, stop capture session
    with patch("karaoke.recorder.start") as mock_start, \
         patch("karaoke.recorder.is_running", return_value=True), \
         patch("karaoke.recorder.stop") as mock_stop:
        mock_session = MagicMock()
        mock_session.recording_id = 100
        mock_session.directory = Path("/tmp/rec-100")
        mock_start.return_value = mock_session

        start_resp = ctrl_client.post("/api/record/start", json={"note": "Live capture test"})
        assert start_resp.status_code == 200
        assert start_resp.json()["status"] == "recording"
        assert start_resp.json()["recording_id"] == 100

        status_resp = ctrl_client.get("/api/record/status")
        assert status_resp.status_code == 200
        assert "recording" in status_resp.json()

        stop_resp = ctrl_client.post("/api/record/stop", json={"recording_id": 100})
        assert stop_resp.status_code == 200
        assert stop_resp.json()["status"] == "stopped"

    # 5. Control API - Analyse / Decompile recording
    with patch("karaoke.recording_worker.analyse", return_value=["ok Swans - A Little God In My Hands (key=G Major bpm=83)"]):
        analyse_resp = ctrl_client.post(f"/api/recordings/{rec_id}/analyse?keep=true")
        assert analyse_resp.status_code == 200
        assert analyse_resp.json()["status"] in ("accepted", "analysed")

    # 6. Control API & Page Endpoints - Serve track page and track audio
    with patch("karaoke.recording_audio.track_segment") as mock_seg, \
         patch("karaoke.recording_audio.track_audio") as mock_aud:
        
        mock_seg.return_value = MagicMock(artist="Swans", title="A Little God In My Hands")
        mock_aud.return_value = test_db_and_recording["seg_file"]

        page_resp = ctrl_client.get(f"/recordings/{rec_id}/tracks/0")
        assert page_resp.status_code == 200
        assert "Swans" in page_resp.text
        assert "A Little God In My Hands" in page_resp.text

        audio_resp = ctrl_client.get(f"/api/recordings/{rec_id}/tracks/0/audio")
        assert audio_resp.status_code == 200
        assert audio_resp.content == b"FLAC_DUMMY_AUDIO_DATA_FOR_TESTING"

    # 7. Control API - Discard recording audio
    discard_resp = ctrl_client.delete(f"/api/recordings/{rec_id}/audio")
    assert discard_resp.status_code == 200
    assert discard_resp.json()["status"] == "discarded"
    assert test_db_and_recording["seg_file"].exists() is False


def test_e2e_api_client_recording_integration(test_db_and_recording, lib_client, ctrl_client):
    """Verify ApiClient methods execute recording operations against the API TestClients."""
    rec_id = test_db_and_recording["recording_id"]
    client = ApiClient(fallback_local=False)

    # Route client HTTP calls to TestClients bound to test DB
    def fake_get(base, path, params=None):
        c = ctrl_client if base == client.ctrl_url else lib_client
        clean = {k: v for k, v in (params or {}).items() if v is not None} if params else None
        resp = c.get(path, params=clean)
        return resp.json() if resp.status_code == 200 else None

    def fake_patch(base, path, body=None):
        c = ctrl_client if base == client.ctrl_url else lib_client
        resp = c.patch(path, json=body)
        return resp.json() if resp.status_code == 200 else None

    def fake_post(base, path, body=None):
        c = ctrl_client if base == client.ctrl_url else lib_client
        resp = c.post(path, json=body)
        return resp.json() if resp.status_code == 200 else None

    def fake_delete(base, path):
        c = ctrl_client if base == client.ctrl_url else lib_client
        resp = c.delete(path)
        return resp.json() if resp.status_code == 200 else None

    with patch.object(client, "_http_get", side_effect=fake_get), \
         patch.object(client, "_http_patch", side_effect=fake_patch), \
         patch.object(client, "_http_post", side_effect=fake_post), \
         patch.object(client, "_http_delete", side_effect=fake_delete):

        # list_recordings
        recs = client.list_recordings(q="Initial")
        assert recs["count"] >= 1

        # get_recording
        rec_detail = client.get_recording(rec_id)
        assert rec_detail is not None
        assert rec_detail["recording_id"] == rec_id

        # update_recording
        updated = client.update_recording(rec_id, note="Updated via ApiClient", keep_audio=True)
        assert updated is not None
        assert updated["note"] == "Updated via ApiClient"

        # record_status
        status = client.record_status()
        assert "recording" in status

        # record_analyse
        with patch("karaoke.recording_worker.analyse", return_value=["ok Swans - A Little God In My Hands"]):
            res = client.record_analyse(rec_id, keep=True)
            assert res["status"] in ("accepted", "analysed")

        # record_discard_audio
        discard_res = client.record_discard_audio(rec_id)
        assert discard_res["status"] == "discarded"
