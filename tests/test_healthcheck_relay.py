"""The healthcheck's relay-backlog probe: stall, dead letters and recovery."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from karaoke import event_store as es
from karaoke import localcache


def _load_healthcheck():
    """Load scripts/healthcheck.py, which is a script rather than a module."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "healthcheck.py"
    spec = importlib.util.spec_from_file_location("karaoke_healthcheck", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load_healthcheck()


@pytest.fixture
def conn():
    c = localcache.connect()
    es.ensure_schema(c, force=True)
    try:
        yield c
    finally:
        c.close()


def test_empty_backlog_is_ok(conn):
    status, detail = hc.check_relay_backlog()
    assert status == hc.OK
    assert "empty" in detail


def test_fresh_backlog_is_ok(conn):
    """A just-queued event is not a stall — this is the false-alarm guard.

    A bulk import legitimately queues a large backlog that drains in seconds;
    alerting on size alone would fire every time.
    """
    for i in range(500):
        es.append(conn, es.AGG_TRACK, 40, es.TRACK_PLAYED, {"n": i})

    status, detail = hc.check_relay_backlog()
    assert status == hc.OK
    assert "500 pending" in detail


def test_stale_backlog_warns(conn, monkeypatch):
    """An old pending event means the relay is not draining."""
    from datetime import datetime, timezone

    es.append(
        conn, es.AGG_TRACK, 41, es.TRACK_PLAYED, {},
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(hc, "RELAY_MAX_AGE", 600.0)

    status, detail = hc.check_relay_backlog()
    assert status == hc.WARN
    assert "karaoke-relay" in detail


def test_dead_letters_warn_with_recovery_command(conn):
    """A parked event needs an operator, so the probe names the command."""
    es.append(conn, es.AGG_TRACK, 42, es.TRACK_PLAYED, {})
    conn.execute("UPDATE events SET attempts = %s", (es.MAX_ATTEMPTS,))

    status, detail = hc.check_relay_backlog()
    assert status == hc.WARN
    assert "dead letter" in detail
    assert "--retry-dead" in detail


def test_probe_is_optional_so_a_stalled_relay_is_not_degraded():
    """A stalled relay forwards nothing but loses nothing, so it must not
    mark the whole platform DEGRADED."""
    entry = [c for c in hc.CHECKS if c[0] == "relay-backlog"]
    assert len(entry) == 1
    assert entry[0][2] is False, "relay-backlog must be optional"


def test_database_probe_uses_dict_rows(conn):
    """Regression: the pool returns dict rows, so row[0] raises KeyError.

    This probe is required, so getting it wrong reported the whole platform
    DEGRADED on every run.
    """
    status, detail = hc.check_database()
    assert status == hc.OK
    assert "tracks" in detail
