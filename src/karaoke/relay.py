"""Outbox relay: forwards unified events to external consumers.

Application code writes a domain change and its event to Postgres in one
transaction (see :mod:`karaoke.event_store`). This worker is the other half of
that pattern — it tails the `events` table and publishes each event onward,
so consumers get at-least-once delivery without a two-phase commit.

Delivery loop
-------------
Each cycle runs in one transaction::

    claim a batch (FOR UPDATE SKIP LOCKED)  ->  publish  ->  mark published

If the publish raises, the transaction rolls back: nothing is marked, the locks
drop, and the batch is retried next cycle. The attempt counter is then bumped
in a *separate* transaction — inside the rolled-back one it would vanish with
everything else, and the event would retry forever without ever reaching the
dead-letter threshold.

Because the publish happens before the commit, a crash in the window between
them redelivers the event. That is the at-least-once guarantee the plan asked
for, and it is why sinks must be idempotent: :class:`OpenSearchSink` uses the
event id as the document id, so a redelivery overwrites rather than duplicates.

Waking up
---------
Postgres `NOTIFY` (fired by a statement-level trigger on `events`) wakes the
relay as soon as a batch commits, so an idle system delivers in milliseconds.
The poll interval remains as a backstop for a dropped notification or a
connection that was re-established mid-flight, so correctness never depends on
the notification arriving.
"""
from __future__ import annotations

import argparse
import json
import signal
import time
from typing import Any, Optional, Protocol

from . import event_store, localcache
from .logger import log

# Separate from the `karaoke-events` index that Argo Events writes: that one
# holds Celery task lifecycle documents with a different shape (task_id,
# task_name, state), and mixing two schemas into one index would break the
# mapping and `karaoke.events.recent_events`.
EVENT_LOG_INDEX = "karaoke-event-log"

DEFAULT_BATCH = 200
DEFAULT_INTERVAL = 5.0
# Ceiling for the exponential backoff applied while a sink is unavailable, so
# a relay that backed off during a long outage still recovers within a minute.
MAX_BACKOFF = 60.0


class SinkUnavailable(Exception):
    """The destination is unreachable, as opposed to the event being bad.

    The distinction decides whether an attempt is counted against the event. A
    rejected document is the event's fault and should eventually dead-letter; a
    consumer that is simply down is not, and counting it would let a routine
    OpenSearch restart park a whole backlog of perfectly good events in under a
    minute. On this the relay backs off and retries indefinitely instead.
    """


class Sink(Protocol):
    """Destination for relayed events."""

    name: str

    def publish(self, events: list[dict[str, Any]]) -> None:
        """Deliver a batch.

        Raise :class:`SinkUnavailable` if the destination is unreachable, or
        any other exception if the batch itself was rejected.
        """


class LogSink:
    """Writes events to the log. The dry-run sink — drains nothing anywhere."""

    name = "log"

    def __init__(self, echo: bool = False) -> None:
        self.echo = echo
        self.published: list[dict[str, Any]] = []

    def publish(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            if self.echo:
                print(json.dumps({
                    "event_id": str(e["event_id"]),
                    "event_type": e["event_type"],
                    "aggregate": f"{e['aggregate_type']}/{e['aggregate_id']}",
                    "created_at": e["created_at"].isoformat(),
                }))
        self.published.extend(events)
        log.debug("relay: log sink took %d event(s)", len(events))


class OpenSearchSink:
    """Indexes events into OpenSearch for search and analytics.

    Idempotent by construction: the event id is the document id, so a
    redelivered event overwrites its own document instead of creating a
    duplicate.
    """

    name = "opensearch"

    def __init__(self, index: str = EVENT_LOG_INDEX) -> None:
        self.index = index
        self._ensured = False

    def _ensure_index(self, client) -> None:
        if self._ensured:
            return
        if not client.indices.exists(index=self.index):
            client.indices.create(index=self.index, body={
                "mappings": {
                    "properties": {
                        "event_id": {"type": "keyword"},
                        "aggregate_type": {"type": "keyword"},
                        "aggregate_id": {"type": "keyword"},
                        "event_type": {"type": "keyword"},
                        "created_at": {"type": "date"},
                        # Payload shape varies per event type, so it is stored
                        # but not indexed as a fixed schema.
                        "payload": {"type": "object", "enabled": True},
                    }
                }
            })
        self._ensured = True

    def publish(self, events: list[dict[str, Any]]) -> None:
        from opensearchpy.exceptions import (
            ConnectionError as OSConnectionError,
            ConnectionTimeout,
        )

        from .osclient import client

        try:
            c = client()
            self._ensure_index(c)
        except (OSConnectionError, ConnectionTimeout) as exc:
            raise SinkUnavailable(f"opensearch unreachable: {exc}") from exc

        lines: list[str] = []
        for e in events:
            lines.append(json.dumps({
                "index": {"_index": self.index, "_id": str(e["event_id"])}
            }))
            lines.append(json.dumps({
                "event_id": str(e["event_id"]),
                "aggregate_type": e["aggregate_type"],
                "aggregate_id": e["aggregate_id"],
                "event_type": e["event_type"],
                "payload": e["payload"],
                "created_at": e["created_at"].isoformat(),
            }))
        body = "\n".join(lines) + "\n"

        try:
            res = c.bulk(body=body, refresh=False)
        except (OSConnectionError, ConnectionTimeout) as exc:
            raise SinkUnavailable(f"opensearch unreachable: {exc}") from exc

        if res.get("errors"):
            # Surface the first real failure; the whole batch is retried, which
            # is safe because indexing by event id is idempotent.
            for item in res.get("items", []):
                err = item.get("index", {}).get("error")
                if err:
                    raise RuntimeError(
                        f"opensearch bulk index failed: {err.get('type')}: "
                        f"{err.get('reason')}"
                    )
            raise RuntimeError("opensearch bulk index reported errors")


SINKS = {"opensearch": OpenSearchSink, "log": LogSink}


class Relay:
    """Drains the outbox into a sink."""

    def __init__(
        self,
        sink: Sink,
        *,
        batch_size: int = DEFAULT_BATCH,
        max_attempts: int = event_store.MAX_ATTEMPTS,
    ) -> None:
        self.sink = sink
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.published = 0
        self.failed = 0
        # Set when the last cycle failed because the destination was down
        # rather than because the batch was rejected; run_forever backs off on
        # it instead of retrying at full speed.
        self.unavailable = False

    def drain_once(self, conn) -> int:
        """Claim, publish and mark one batch. Returns events published.

        Returns 0 both when the queue is empty and when the batch failed —
        callers distinguish via :attr:`failed`.
        """
        claimed: list[dict[str, Any]] = []
        self.unavailable = False
        try:
            with conn.transaction():
                claimed = event_store.claim_unpublished(
                    conn, self.batch_size, max_attempts=self.max_attempts
                )
                if not claimed:
                    return 0
                self.sink.publish(claimed)
                event_store.mark_published(
                    conn, [e["event_id"] for e in claimed]
                )
        except SinkUnavailable as exc:
            # Not the events' fault: leave the attempt counters alone so a
            # consumer outage cannot dead-letter a healthy backlog. The batch
            # stays claimable and is retried after a backoff.
            self.unavailable = True
            log.warning(
                "relay: %s sink unavailable, %d event(s) deferred: %s",
                self.sink.name, len(claimed), exc,
            )
            return 0
        except Exception as exc:
            # The transaction rolled back, so nothing was marked and the locks
            # are gone. Count the attempt separately or it rolls back too.
            self.failed += len(claimed)
            log.warning(
                "relay: batch of %d failed via %s sink: %s",
                len(claimed), self.sink.name, exc,
            )
            if claimed:
                try:
                    with conn.transaction():
                        event_store.record_failure(
                            conn, [e["event_id"] for e in claimed], str(exc)
                        )
                except Exception:
                    log.exception("relay: could not record failure")
            return 0

        self.published += len(claimed)
        log.info(
            "relay: published %d event(s) via %s sink",
            len(claimed), self.sink.name,
        )
        return len(claimed)

    def drain(self, conn, *, max_batches: Optional[int] = None) -> int:
        """Drain until the queue is empty or a batch fails."""
        total = 0
        batches = 0
        while max_batches is None or batches < max_batches:
            n = self.drain_once(conn)
            batches += 1
            if n == 0:
                break
            total += n
        return total


def _wait_for_event(conn, timeout: float) -> bool:
    """Block until an event is announced or ``timeout`` elapses.

    Returns True if a notification arrived. Falls back to a plain sleep if the
    connection cannot LISTEN, so the relay degrades to polling rather than
    spinning or dying.
    """
    try:
        conn.execute(f"LISTEN {event_store.NOTIFY_CHANNEL}")
    except Exception:
        log.debug("relay: LISTEN unavailable, polling instead", exc_info=True)
        time.sleep(timeout)
        return False

    try:
        # Consume the generator to exhaustion rather than breaking out of it.
        # `stop_after=1` ends it after the first notification; abandoning it
        # early instead (a `break` or a `return` inside the loop) triggers
        # generator close and blocks indefinitely, which wedges the relay on
        # exactly the wake-up path it exists to serve.
        return bool(list(conn.notifies(timeout=timeout, stop_after=1)))
    except TypeError:
        # psycopg < 3.2 has no timeout/stop_after on notifies().
        time.sleep(timeout)
        return False
    except Exception:
        log.debug("relay: notify wait failed, polling instead", exc_info=True)
        time.sleep(timeout)
        return False
    finally:
        try:
            conn.execute(f"UNLISTEN {event_store.NOTIFY_CHANNEL}")
        except Exception:
            pass


def run_forever(
    sink: Sink,
    *,
    batch_size: int = DEFAULT_BATCH,
    interval: float = DEFAULT_INTERVAL,
    max_attempts: int = event_store.MAX_ATTEMPTS,
) -> int:
    """Run the relay until SIGINT/SIGTERM. Returns events published."""
    relay = Relay(sink, batch_size=batch_size, max_attempts=max_attempts)
    stopping = False

    def _stop(signum, _frame):
        nonlocal stopping
        stopping = True
        log.info("relay: signal %s received, finishing current batch", signum)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    conn = localcache.connect()
    try:
        event_store.ensure_schema(conn)
        log.info(
            "relay: started (sink=%s batch=%d interval=%.1fs)",
            sink.name, batch_size, interval,
        )
        backoff = interval
        while not stopping:
            # Drain fully, then park on NOTIFY so an idle relay costs nothing.
            drained = relay.drain(conn)
            if stopping:
                break

            if relay.unavailable:
                # Exponential backoff while the consumer is down, capped so the
                # relay still recovers promptly once it returns. No NOTIFY wait
                # here: events are arriving fine, it is the sink that is not.
                log.info("relay: backing off %.0fs (sink unavailable)", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            elif drained == 0:
                backoff = interval
                _wait_for_event(conn, interval)
            else:
                backoff = interval
    finally:
        conn.close()

    log.info(
        "relay: stopped (published=%d failed=%d)", relay.published, relay.failed
    )
    return relay.published


def main() -> int:
    """`karaoke-relay` entry point."""
    ap = argparse.ArgumentParser(
        prog="karaoke-relay",
        description="Forward unified events to external consumers.",
    )
    ap.add_argument("--sink", choices=sorted(SINKS), default="opensearch",
                    help="destination (default: opensearch)")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH,
                    help=f"events per batch (default: {DEFAULT_BATCH})")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                    help="seconds to wait when idle if no NOTIFY arrives")
    ap.add_argument("--max-attempts", type=int, default=event_store.MAX_ATTEMPTS,
                    help="failures before an event becomes a dead letter "
                         f"(default: {event_store.MAX_ATTEMPTS})")
    ap.add_argument("--once", action="store_true",
                    help="drain the backlog and exit instead of running forever")
    ap.add_argument("--echo", action="store_true",
                    help="with --sink log, print each event as JSON")
    ap.add_argument("--status", action="store_true",
                    help="print backlog and dead-letter counts, then exit")
    ap.add_argument("--retry-dead", action="store_true",
                    help="requeue dead letters, then exit")
    args = ap.parse_args()

    if args.status or args.retry_dead:
        conn = localcache.connect()
        try:
            event_store.ensure_schema(conn)
            if args.retry_dead:
                n = event_store.retry_dead_letters(
                    conn, max_attempts=args.max_attempts
                )
                print(f"requeued {n} dead letter(s)")
                return 0

            s = event_store.stats(conn, max_attempts=args.max_attempts)
            print(f"events total   : {s['total']}")
            print(f"claimable      : {s['claimable']}")
            print(f"dead letters   : {s['dead_letters']}")
            for d in event_store.dead_letters(
                conn, max_attempts=args.max_attempts, limit=5
            ):
                print(f"  {d['event_type']:22} attempts={d['attempts']} "
                      f"{(d['last_error'] or '')[:80]}")
        finally:
            conn.close()
        return 0

    sink: Sink = (
        LogSink(echo=args.echo) if args.sink == "log" else OpenSearchSink()
    )

    if args.once:
        relay = Relay(sink, batch_size=args.batch_size,
                      max_attempts=args.max_attempts)
        conn = localcache.connect()
        try:
            event_store.ensure_schema(conn)
            n = relay.drain(conn)
        finally:
            conn.close()
        print(f"published {n} event(s) via {sink.name} sink")
        if relay.unavailable:
            print(f"{sink.name} sink unavailable; backlog left for retry")
        return 1 if (relay.failed or relay.unavailable) else 0

    run_forever(sink, batch_size=args.batch_size, interval=args.interval,
                max_attempts=args.max_attempts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
