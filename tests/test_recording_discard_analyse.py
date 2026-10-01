"""Discarding recording audio and dispatching analysis, without a database.

Everything here runs against an in-memory fake connection and monkeypatched
lookups, so it is safe to run in isolation with ``pytest --noconftest``: the
shared conftest wipes and resets the configured test database.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from karaoke import jobs, recorder, recording_worker as rw


class FakeConn:
    """Just enough of a connection for load_recording/discard_audio/prune."""

    def __init__(self, rows):
        self.rows = {int(r["recording_id"]): dict(r) for r in rows}
        self.updates: list[tuple] = []
        self.commits = 0

    def execute(self, sql, params=()):
        conn = self

        class Result:
            def fetchone(self_):
                return conn.rows.get(int(params[0]))

            def fetchall(self_):
                return list(conn.rows.values())

        if sql.lstrip().upper().startswith("UPDATE"):
            self.updates.append((sql, params))
            rid = int(params[0])
            if rid in self.rows and self.rows[rid]["status"] != "recording":
                self.rows[rid]["status"] = "discarded"
        return Result()

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _row(rid, directory, *, status="analysed", age_days=30.0, keep=0):
    import time
    return {"recording_id": rid, "dir": str(directory), "status": status,
            "keep_audio": keep, "started_at": time.time() - age_days * 86400.0}


def _audio(directory: Path, *sizes: int) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    for i, size in enumerate(sizes):
        f = directory / f"seg-20260905-12000{i}.flac"
        f.write_bytes(b"x" * size)
        files.append(f)
    return files


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch):
    from karaoke import localcache

    def refuse(*a, **k):
        raise AssertionError("test touched the real database")
    monkeypatch.setattr(localcache, "connect", refuse)


# -- discard_audio --------------------------------------------------------

def test_discard_counts_freed_bytes_and_marks_discarded(tmp_path):
    directory = tmp_path / "rec1"
    _audio(directory, 100, 250)
    conn = FakeConn([_row(1, directory)])

    assert rw.discard_audio(1, conn=conn) == 350
    assert not directory.exists()
    assert conn.rows[1]["status"] == "discarded"


def test_a_failed_unlink_is_not_counted_and_is_raised(tmp_path, monkeypatch):
    directory = tmp_path / "rec1"
    stuck, gone = _audio(directory, 100, 250)
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self == stuck:
            raise PermissionError(13, "in use", str(self))
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    conn = FakeConn([_row(1, directory)])

    with pytest.raises(rw.DiscardError) as info:
        rw.discard_audio(1, conn=conn)

    assert info.value.freed == 250          # only the file really deleted
    assert info.value.remaining == [stuck]
    assert stuck.exists() and not gone.exists()
    assert conn.updates == []               # status untouched
    assert conn.rows[1]["status"] == "analysed"


def test_prune_reports_a_failed_discard_instead_of_claiming_success(
        tmp_path, monkeypatch):
    directory = tmp_path / "rec1"
    _audio(directory, 2048)
    conn = FakeConn([_row(1, directory, age_days=30.0)])

    def fail(rid, *, conn=None):
        raise rw.DiscardError(rid, 0, [directory / "seg-x.flac"], ["denied"])
    monkeypatch.setattr(rw, "discard_audio", fail)

    notes = rw.prune_recordings(conn=conn)
    assert len(notes) == 1 and "FAILED" in notes[0]
    assert not any("pruned recording" in n for n in notes)


# -- control API ----------------------------------------------------------

@pytest.fixture()
def ctrl(monkeypatch, tmp_path):
    from karaoke.ctrl_api import app
    monkeypatch.setattr(rw, "load_recording",
                        lambda rid, conn=None: _row(rid, tmp_path / "rec")
                        if rid == 1 else None)
    monkeypatch.setattr(recorder, "is_running", lambda rid: False)
    return TestClient(app)   # no context manager: skip the lifespan hook


@pytest.fixture()
def analyse_calls(monkeypatch):
    calls = []

    def analyse(recording_id, **kwargs):
        calls.append((recording_id, kwargs))
        return ["ok"]
    monkeypatch.setattr(rw, "analyse", analyse)
    return calls


def test_analyse_forwards_prune_after_and_tracks_the_job(ctrl, analyse_calls):
    resp = ctrl.post("/api/recordings/1/analyse?prune_after=true")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted" and body["job_id"]
    assert analyse_calls == [(1, {"prune_after": True})]
    job = jobs.get_job(body["job_id"])
    assert job["status"] == "done" and job["result"] == ["ok"]


@pytest.mark.parametrize("query", ["", "?keep=true", "?keep=false"])
def test_analyse_accepts_and_ignores_keep(ctrl, analyse_calls, query):
    resp = ctrl.post(f"/api/recordings/1/analyse{query}")
    assert resp.status_code == 200
    assert analyse_calls == [(1, {"prune_after": False})]


def test_discard_failure_is_an_explicit_error(ctrl, monkeypatch, tmp_path):
    left = tmp_path / "rec" / "seg-1.flac"

    def fail(rid, *, conn=None):
        raise rw.DiscardError(rid, 100, [left], [f"{left}: denied"])
    monkeypatch.setattr(rw, "discard_audio", fail)

    resp = ctrl.delete("/api/recordings/1/audio")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "status" not in detail           # the 500 already says "error"
    assert detail["recording_id"] == 1
    assert detail["freed_bytes"] == 100
    assert detail["remaining"] == [str(left)]


def test_discard_success_reports_freed_bytes(ctrl, monkeypatch):
    monkeypatch.setattr(rw, "discard_audio", lambda rid, *, conn=None: 4096)
    resp = ctrl.delete("/api/recordings/1/audio")
    assert resp.status_code == 200
    assert resp.json() == {"status": "discarded", "recording_id": 1,
                           "freed_bytes": 4096}


# -- client and CLI -------------------------------------------------------
#
# The client used to turn every HTTP error into "unreachable", so a server 500
# from a failed discard triggered a second, in-process discard attempt.

import io
import json
import urllib.error

from karaoke import api_client


def _http_error(code, detail):
    body = json.dumps({"detail": detail}).encode("utf-8")
    return urllib.error.HTTPError("http://ctrl/x", code, "err", {}, io.BytesIO(body))


@pytest.fixture()
def no_local_discard(monkeypatch):
    from karaoke import ctrl_api

    def refuse(rid):
        raise AssertionError("retried the discard locally after an HTTP error")
    monkeypatch.setattr(ctrl_api, "record_discard", refuse)


FAILED_DETAIL = {"recording_id": 3,
                 "message": "recording 3: 1 audio file(s) could not be deleted",
                 "freed_bytes": 2_000_000, "remaining": ["/r/seg-1.flac"]}


def test_client_keeps_an_http_error_and_does_not_retry_locally(
        monkeypatch, no_local_discard):
    def urlopen(req, timeout=None):
        raise _http_error(500, FAILED_DETAIL)
    monkeypatch.setattr(api_client.urllib.request, "urlopen", urlopen)

    res = api_client.ApiClient(ctrl_url="http://ctrl").record_discard_audio(3)
    assert res["status"] == "error"
    assert res["http_status"] == 500
    assert res["detail"] == FAILED_DETAIL
    assert "freed_bytes" not in res          # not success-shaped


def test_client_still_falls_back_when_the_server_is_unreachable(monkeypatch):
    from karaoke import ctrl_api

    def urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(api_client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ctrl_api, "record_discard",
                        lambda rid: {"status": "discarded", "freed_bytes": 7})

    res = api_client.ApiClient(ctrl_url="http://ctrl").record_discard_audio(3)
    assert res == {"status": "discarded", "freed_bytes": 7}


def test_a_local_fallback_failure_is_returned_not_raised(monkeypatch):
    from fastapi import HTTPException
    from karaoke import ctrl_api

    def urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    def fail(rid):
        raise HTTPException(status_code=500, detail=FAILED_DETAIL)
    monkeypatch.setattr(api_client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(ctrl_api, "record_discard", fail)

    res = api_client.ApiClient(ctrl_url="http://ctrl").record_discard_audio(3)
    assert res["status"] == "error" and res["detail"] == FAILED_DETAIL


def test_cli_discard_renders_the_server_error(monkeypatch, capsys,
                                              no_local_discard):
    def urlopen(req, timeout=None):
        raise _http_error(500, FAILED_DETAIL)
    monkeypatch.setattr(api_client.urllib.request, "urlopen", urlopen)

    assert rw.recording_main(["--discard", "3"]) == 1
    out, err = capsys.readouterr()
    assert "freed" not in out
    assert "discard failed" in err
    assert "could not be deleted" in err
    assert "freed 2 MB before failing" in err


def test_cli_discard_renders_a_plain_string_detail(monkeypatch, capsys,
                                                   no_local_discard):
    def urlopen(req, timeout=None):
        raise _http_error(409, "Recording is still capturing; stop it first")
    monkeypatch.setattr(api_client.urllib.request, "urlopen", urlopen)

    assert rw.recording_main(["--discard", "3"]) == 1
    assert "still capturing" in capsys.readouterr().err


# -- _http_delete call compatibility --------------------------------------

def test_only_record_discard_opts_into_http_errors(monkeypatch):
    """Test doubles with the old (base, path) signature keep working for every
    other DELETE caller; record_discard_audio is the one that needs the kwarg."""
    calls = []

    def fake_delete(base, path, **kwargs):
        calls.append(kwargs)
        return {"status": "ok"}
    client = api_client.ApiClient(ctrl_url="http://ctrl")
    monkeypatch.setattr(client, "_http_delete", fake_delete)

    client.stop_play_session("play_1")
    client.record_discard_audio(3)
    assert calls == [{}, {"http_errors": True}]


# -- turning an HTTPError into a result -----------------------------------

def _from_body(body, code=500, reason="Internal Server Error"):
    fp = None if body is None else io.BytesIO(body)
    return api_client._http_error_result(
        urllib.error.HTTPError("http://ctrl/x", code, reason, {}, fp))


def test_the_real_endpoint_body_yields_one_flat_error_shape(ctrl, monkeypatch):
    def fail(rid, *, conn=None):
        raise rw.DiscardError(rid, 5, [Path("/r/seg-1.flac")], ["denied"])
    monkeypatch.setattr(rw, "discard_audio", fail)
    body = ctrl.delete("/api/recordings/1/audio").content

    res = _from_body(body)
    assert set(res) == {"status", "http_status", "detail"}
    assert res["status"] == "error" and res["http_status"] == 500
    assert "status" not in res["detail"]
    assert res["detail"]["freed_bytes"] == 5


@pytest.mark.parametrize("body, detail", [
    (b"<html>Bad Gateway</html>", "<html>Bad Gateway</html>"),
    (b"", "Internal Server Error"),
    (None, "Internal Server Error"),
    (b"[1, 2]", [1, 2]),
    (b'{"error": "x"}', {"error": "x"}),
    (b'{"detail": "plain"}', "plain"),
])
def test_odd_error_bodies_never_raise(body, detail):
    assert _from_body(body) == {"status": "error", "http_status": 500,
                                "detail": detail}


def test_long_non_json_bodies_are_truncated():
    res = _from_body(b"x" * 5000)
    assert res["detail"] == "x" * 500


def test_invalid_utf8_is_replaced_not_raised():
    assert _from_body(b"\xff\xfe")["detail"] == "\ufffd\ufffd"


def test_a_real_http_500_round_trips_through_urllib(no_local_discard):
    """Loopback server on an ephemeral port: the genuine HTTPError path."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    payload = json.dumps({"detail": FAILED_DETAIL}).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_DELETE(self):
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        res = api_client.ApiClient(ctrl_url=url).record_discard_audio(3)
    finally:
        server.shutdown()
        server.server_close()
    assert res == {"status": "error", "http_status": 500,
                   "detail": FAILED_DETAIL, "recording_id": 3}
