"""Outbox relay: delivery, failure isolation, dead-lettering and recovery."""
from __future__ import annotations

import pytest

from karaoke import event_store as es
from karaoke import relay


@pytest.fixture
def conn():
    from karaoke import localcache

    c = localcache.connect()
    es.ensure_schema(c, force=True)
    try:
        yield c
    finally:
        c.close()


class BoomSink:
    """A consumer that is always down."""

    name = "boom"

    def __init__(self):
        self.calls = 0

    def publish(self, events):
        self.calls += 1
        raise RuntimeError("consumer down")


class FlakySink:
    """Fails the first ``fail_times`` batches, then recovers."""

    name = "flaky"

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0
        self.published = []

    def publish(self, events):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("temporarily down")
        self.published.extend(events)


def _seed(conn, n: int, aggregate_id: int = 1):
    for i in range(n):
        es.append(conn, es.AGG_TRACK, aggregate_id, es.TRACK_PLAYED, {"n": i})


def test_drain_publishes_and_marks(conn):
    """A successful batch reaches the sink and leaves the backlog empty."""
    _seed(conn, 3)
    sink = relay.LogSink()

    assert relay.Relay(sink, batch_size=10).drain_once(conn) == 3
    assert len(sink.published) == 3
    assert es.unpublished_count(conn) == 0


def test_drain_on_empty_queue_is_a_noop(conn):
    sink = relay.LogSink()
    assert relay.Relay(sink).drain_once(conn) == 0
    assert sink.published == []


def test_drain_does_not_redeliver(conn):
    """Each event is handed to the sink once across successive batches."""
    _seed(conn, 6)
    sink = relay.LogSink()
    r = relay.Relay(sink, batch_size=2)

    r.drain(conn)

    ids = [str(e["event_id"]) for e in sink.published]
    assert len(ids) == 6
    assert len(set(ids)) == 6, "an event was delivered twice"


def test_drain_respects_batch_size(conn):
    _seed(conn, 5)
    sink = relay.LogSink()

    assert relay.Relay(sink, batch_size=2).drain_once(conn) == 2
    assert es.unpublished_count(conn) == 3


def test_failed_publish_marks_nothing(conn):
    """The core safety property: a failed sink never marks events published."""
    _seed(conn, 3)
    r = relay.Relay(BoomSink(), batch_size=10)

    assert r.drain_once(conn) == 0
    assert r.published == 0
    assert es.unpublished_count(conn) == 3, "events were lost on failure"


def test_failed_publish_increments_attempts(conn):
    """The attempt counter survives the rolled-back publish transaction."""
    _seed(conn, 2)
    r = relay.Relay(BoomSink(), batch_size=10)

    r.drain_once(conn)
    r.drain_once(conn)

    rows = conn.execute("SELECT attempts, last_error FROM events").fetchall()
    assert all(row["attempts"] == 2 for row in rows)
    assert all("consumer down" in row["last_error"] for row in rows)


def test_events_dead_letter_after_max_attempts(conn):
    """A permanently undeliverable event is parked, not retried forever."""
    _seed(conn, 2)
    r = relay.Relay(BoomSink(), batch_size=10, max_attempts=3)

    for _ in range(5):
        r.drain_once(conn)

    assert es.dead_letter_count(conn, max_attempts=3) == 2
    # Claiming skips them, so they no longer occupy the relay.
    assert es.claim_unpublished(conn, 10, max_attempts=3) == []


def test_poison_event_does_not_starve_the_queue(conn):
    """Events behind an undeliverable batch still get delivered.

    Without an attempt cap the first batch would be reclaimed forever and
    nothing after it would ever reach a consumer.
    """
    _seed(conn, 2, aggregate_id=1)   # these will poison
    r = relay.Relay(BoomSink(), batch_size=2, max_attempts=2)
    for _ in range(3):
        r.drain_once(conn)
    assert es.dead_letter_count(conn, max_attempts=2) == 2

    _seed(conn, 2, aggregate_id=2)   # arrive after the poison
    good = relay.LogSink()
    delivered = relay.Relay(good, batch_size=10, max_attempts=2).drain(conn)

    assert delivered == 2
    assert {e["aggregate_id"] for e in good.published} == {"2"}


def test_relay_recovers_when_sink_comes_back(conn):
    """A transient outage retries rather than dead-letters."""
    _seed(conn, 3)
    sink = FlakySink(fail_times=2)
    r = relay.Relay(sink, batch_size=10)

    assert r.drain_once(conn) == 0
    assert r.drain_once(conn) == 0
    assert r.drain_once(conn) == 3

    assert len(sink.published) == 3
    assert es.unpublished_count(conn) == 0


def test_retry_dead_letters_requeues(conn):
    """Operator recovery: fix the consumer, requeue, deliver."""
    _seed(conn, 2)
    r = relay.Relay(BoomSink(), batch_size=10, max_attempts=2)
    for _ in range(3):
        r.drain_once(conn)
    assert es.dead_letter_count(conn, max_attempts=2) == 2

    assert es.retry_dead_letters(conn, max_attempts=2) == 2
    assert es.dead_letter_count(conn, max_attempts=2) == 0

    good = relay.LogSink()
    assert relay.Relay(good, batch_size=10, max_attempts=2).drain(conn) == 2


def test_retry_dead_letters_by_id(conn):
    """Requeueing one dead letter leaves the others parked."""
    _seed(conn, 3)
    r = relay.Relay(BoomSink(), batch_size=10, max_attempts=1)
    r.drain_once(conn)

    parked = es.dead_letters(conn, max_attempts=1)
    assert len(parked) == 3

    n = es.retry_dead_letters(
        conn, max_attempts=1, event_ids=[parked[0]["event_id"]]
    )
    assert n == 1
    assert es.dead_letter_count(conn, max_attempts=1) == 2


def test_stats_separates_claimable_from_dead(conn):
    """`claimable` is what the relay will attempt; `unpublished` is everything."""
    _seed(conn, 4)
    r = relay.Relay(BoomSink(), batch_size=2, max_attempts=1)
    r.drain_once(conn)

    s = es.stats(conn, max_attempts=1)
    assert s["unpublished"] == 4
    assert s["dead_letters"] == 2
    assert s["claimable"] == 2


def test_drain_stops_at_max_batches(conn):
    _seed(conn, 10)
    sink = relay.LogSink()

    n = relay.Relay(sink, batch_size=2).drain(conn, max_batches=2)
    assert n == 4
    assert es.unpublished_count(conn) == 6


def test_failed_batch_counts_toward_failed_total(conn):
    _seed(conn, 3)
    r = relay.Relay(BoomSink(), batch_size=10)
    r.drain_once(conn)
    assert r.failed == 3


class DownSink:
    """A destination that is unreachable, as opposed to rejecting the batch."""

    name = "down"

    def __init__(self):
        self.calls = 0

    def publish(self, events):
        self.calls += 1
        raise relay.SinkUnavailable("opensearch unreachable")


def test_sink_outage_does_not_count_attempts(conn):
    """A down consumer must not be charged against the events.

    Otherwise a routine OpenSearch restart dead-letters a healthy backlog: at
    a 5s loop and 5 attempts, everything claimable is parked inside a minute.
    """
    _seed(conn, 4)
    r = relay.Relay(DownSink(), batch_size=2, max_attempts=2)

    for _ in range(10):
        r.drain_once(conn)

    rows = conn.execute("SELECT attempts FROM events").fetchall()
    assert all(row["attempts"] == 0 for row in rows)
    assert es.dead_letter_count(conn, max_attempts=2) == 0
    assert r.unavailable is True


def test_events_survive_an_outage_and_deliver_on_recovery(conn):
    """Nothing is lost or parked while the consumer is down."""
    _seed(conn, 3)
    relay.Relay(DownSink(), batch_size=10, max_attempts=2).drain(conn)
    assert es.unpublished_count(conn) == 3

    good = relay.LogSink()
    assert relay.Relay(good, batch_size=10).drain(conn) == 3
    assert es.unpublished_count(conn) == 0


def test_rejected_batch_still_dead_letters(conn):
    """The outage path must not disable dead-lettering for real poison."""
    _seed(conn, 2)
    r = relay.Relay(BoomSink(), batch_size=10, max_attempts=2)

    for _ in range(3):
        r.drain_once(conn)

    assert es.dead_letter_count(conn, max_attempts=2) == 2
    assert r.unavailable is False


def test_unavailable_flag_clears_on_success(conn):
    """A recovered sink resets the backoff signal."""
    _seed(conn, 2)
    r = relay.Relay(DownSink(), batch_size=10)
    r.drain_once(conn)
    assert r.unavailable is True

    r.sink = relay.LogSink()
    r.drain_once(conn)
    assert r.unavailable is False


def test_wait_for_event_times_out_when_idle(conn):
    """An idle wait returns on its own deadline rather than blocking."""
    import time

    t0 = time.time()
    woke = relay._wait_for_event(conn, 1.0)
    elapsed = time.time() - t0

    assert woke is False
    assert 0.5 < elapsed < 5.0, f"timeout not honoured ({elapsed:.2f}s)"


def test_wait_for_event_wakes_on_commit(conn):
    """NOTIFY wakes a parked relay as soon as an event commits.

    Regression test: consuming the notifies() generator early (a `break` or a
    `return` inside the loop) blocks forever on generator close, wedging the
    relay on the exact path it exists to serve.
    """
    import threading
    import time

    from karaoke import localcache

    result = {}

    def wait():
        t0 = time.time()
        result["woke"] = relay._wait_for_event(conn, 15.0)
        result["secs"] = time.time() - t0

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    time.sleep(1.0)  # let LISTEN register

    writer = localcache.connect()
    try:
        es.append(writer, es.AGG_TRACK, 4242, es.TRACK_PLAYED, {})
    finally:
        writer.close()

    t.join(20)
    assert not t.is_alive(), "wait_for_event never returned"
    assert result["woke"] is True
    assert result["secs"] < 10.0, "woke on poll timeout, not on NOTIFY"


def test_dead_letters_report_the_error(conn):
    """The parked event carries why it failed, for the operator."""
    _seed(conn, 1)
    r = relay.Relay(BoomSink(), batch_size=10, max_attempts=1)
    r.drain_once(conn)

    d = es.dead_letters(conn, max_attempts=1)
    assert len(d) == 1
    assert "consumer down" in d[0]["last_error"]
    assert d[0]["failed_at"] is not None
