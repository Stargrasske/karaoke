#!/usr/bin/env python3
"""Karaoke platform health check.

Verifies every moving part of the local karaoke stack and prints a compact,
journald-friendly report. Exits non-zero if any REQUIRED check fails, so it can
back a systemd oneshot unit (``systemctl --user is-failed karaoke-healthcheck``)
and a timer.

Checks:
  - Library API      http://127.0.0.1:8000/health   (required)
  - Control API      http://127.0.0.1:8765/health    (optional: desktop session)
  - RabbitMQ AMQP    localhost:5672 reachable         (required)
  - RabbitMQ mgmt    http://127.0.0.1:15672           (optional)
  - kind cluster     karaoke ns pods Running          (required)
  - Kiosk Chrome CDP http://localhost:9222/json       (optional)
  - Postgres DB      openable + track count           (required)
  - Relay backlog    outbox draining, no dead letters (optional)

Env overrides: KARAOKE_API_PORT (8000), KARAOKE_CTRL_PORT (8765),
RABBITMQ_HOST (localhost), KUBE_CONTEXT (kind-karaoke), K8S_NAMESPACE (karaoke),
KARAOKE_HEALTH_RELAY_MAX_AGE (600).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request

API_PORT = os.environ.get("KARAOKE_API_PORT", "8000")
CTRL_PORT = os.environ.get("KARAOKE_CTRL_PORT", "8765")
MQ_HOST = os.environ.get("RABBITMQ_HOST", "localhost")
KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-karaoke")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
MCP_PORT = int(os.environ.get("KARAOKE_MCP_PORT", "8888"))
WEBTUI_PORT = int(os.environ.get("KARAOKE_WEBTUI_PORT", "8001"))
OLLAMA_PORT = int(os.environ.get("KARAOKE_OLLAMA_PORT", "11434"))
K8S_NS = os.environ.get("K8S_NAMESPACE", "karaoke")
# How stale the oldest pending event may get before the relay counts as stalled.
# Generous by default: a bulk import queues a large backlog that the relay
# drains in seconds, and a brief consumer outage is normal.
RELAY_MAX_AGE = float(os.environ.get("KARAOKE_HEALTH_RELAY_MAX_AGE", "600"))

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_MARK = {OK: "✓", WARN: "!", FAIL: "✗"}


def _http_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_library_api() -> tuple[str, str]:
    url = f"http://127.0.0.1:{API_PORT}/health"
    return (OK, url) if _http_ok(url) else (FAIL, f"{url} unreachable")


def check_control_api() -> tuple[str, str]:
    url = f"http://127.0.0.1:{CTRL_PORT}/health"
    # Optional: only meaningful with a desktop session.
    return (OK, url) if _http_ok(url) else (WARN, f"{url} down (no desktop session?)")


def check_mq_amqp() -> tuple[str, str]:
    return (OK, f"{MQ_HOST}:5672") if _tcp_ok(MQ_HOST, 5672) else (
        FAIL, f"{MQ_HOST}:5672 unreachable (start karaoke-mq-forward)")


def check_mq_mgmt() -> tuple[str, str]:
    url = "http://127.0.0.1:15672"
    return (OK, url) if _tcp_ok("127.0.0.1", 15672) else (WARN, f"{url} down")


def check_kind_pods() -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["kubectl", "--context", KUBE_CONTEXT, "-n", K8S_NS,
             "get", "pods", "--no-headers",
             "-o", "custom-columns=NAME:.metadata.name,PHASE:.status.phase"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:
        return FAIL, f"kubectl failed: {exc}"
    if out.returncode != 0:
        return FAIL, (out.stderr or "kubectl error").strip().splitlines()[-1]
    rows = [l.split() for l in out.stdout.strip().splitlines() if l.strip()]
    if not rows:
        return FAIL, f"no pods in ns/{K8S_NS}"
    bad = [f"{n}={p}" for n, p in rows if p != "Running"]
    if bad:
        return FAIL, "not Running: " + ", ".join(bad)
    return OK, f"{len(rows)} pod(s) Running"


def check_kiosk_chrome() -> tuple[str, str]:
    url = "http://localhost:9222/json"
    return (OK, "CDP :9222") if _http_ok(url, timeout=2.0) else (
        WARN, "kiosk Chrome CDP :9222 down (unified player off)")


def check_database() -> tuple[str, str]:
    try:
        sys.path.insert(0, "/home/tina/karaoke/src")
        from karaoke import localcache
        with localcache.connect() as conn:
            # Rows come back as dicts (the pool sets row_factory=dict_row), so
            # this must be keyed by name -- row[0] raises KeyError.
            row = conn.execute("SELECT count(*) AS n FROM tracks").fetchone()
        return OK, f"{row['n']} tracks"
    except Exception as exc:
        return FAIL, f"DB error: {exc}"


def check_relay_backlog() -> tuple[str, str]:
    """Is the outbox relay actually draining, and has it parked anything?

    Optional rather than required: a stalled relay stops *forwarding* events,
    but nothing is lost -- they stay in Postgres with ``published_at IS NULL``
    and deliver once it recovers. Playback, search and the TUI are unaffected,
    so this must not mark the whole platform DEGRADED.
    """
    try:
        sys.path.insert(0, "/home/tina/karaoke/src")
        from datetime import datetime, timezone

        from karaoke import event_store, localcache

        with localcache.connect() as conn:
            if not localcache.table_exists(conn, "events"):
                return WARN, "events table missing (run the migration)"
            pending = event_store.unpublished_count(conn)
            dead = event_store.dead_letter_count(conn)
            oldest = event_store.oldest_unpublished(conn)
    except Exception as exc:
        return WARN, f"relay check failed: {exc}"

    if dead:
        # Never self-heals: an operator must fix the consumer and requeue.
        return WARN, (f"{dead} dead letter(s) — "
                      f"karaoke-relay --status, then --retry-dead")

    claimable = pending - dead
    if not claimable:
        return OK, "backlog empty"

    age = (datetime.now(timezone.utc) - oldest).total_seconds() if oldest else 0
    if age > RELAY_MAX_AGE:
        return WARN, (f"{claimable} event(s) pending, oldest {int(age)}s "
                      f"(> {RELAY_MAX_AGE}s) — is karaoke-relay running?")
    return OK, f"{claimable} pending, oldest {int(age)}s"


def check_opensearch() -> tuple[str, str]:
    """Vector search. Reached through a kind extraPortMapping, not a
    port-forward, so it comes back with the cluster container rather than
    needing a host service."""
    url = OPENSEARCH_URL
    return (OK, url) if _http_ok(url) else (
        FAIL, f"{url} unreachable (is the kind cluster up?)")


def check_mcp() -> tuple[str, str]:
    """The MCP server, which is how Claude and Obot reach the library."""
    url = f"http://127.0.0.1:{MCP_PORT}/health"
    return (OK, url) if _http_ok(url) else (
        FAIL, f"{url} unreachable (start karaoke-mcp)")


def check_webtui() -> tuple[str, str]:
    return (OK, f"127.0.0.1:{WEBTUI_PORT}") if _tcp_ok("127.0.0.1", WEBTUI_PORT) else (
        WARN, f"127.0.0.1:{WEBTUI_PORT} down (start karaoke-webtui)")


def check_ollama() -> tuple[str, str]:
    """Only a warning: the DJ booth falls back to deterministic library tools
    when the model is unavailable, so the room keeps working without it."""
    return (OK, f"127.0.0.1:{OLLAMA_PORT}") if _tcp_ok("127.0.0.1", OLLAMA_PORT) else (
        WARN, f"127.0.0.1:{OLLAMA_PORT} down (DJ chat degrades to library tools)")


def check_obot_tunnel() -> tuple[str, str]:
    """The gateway's only route to the library: Obot rejects MCP URLs that
    resolve to a private IP, so it reaches :8888 through this outbound tunnel."""
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "karaoke-obot-tunnel"],
            capture_output=True, text=True, timeout=5,
        )
        state = proc.stdout.strip() or "unknown"
    except Exception as exc:
        return WARN, f"could not query unit: {exc}"
    return (OK, "karaoke-obot-tunnel active") if state == "active" else (
        WARN, f"karaoke-obot-tunnel {state} (Obot cannot reach the library)")


CHECKS = [
    ("library-api", check_library_api, True),
    ("control-api", check_control_api, False),
    ("rabbitmq-amqp", check_mq_amqp, True),
    ("rabbitmq-mgmt", check_mq_mgmt, False),
    ("kind-pods", check_kind_pods, True),
    ("kiosk-chrome", check_kiosk_chrome, False),
    ("postgres-db", check_database, True),
    ("relay-backlog", check_relay_backlog, False),
    ("opensearch", check_opensearch, True),
    ("mcp-server", check_mcp, True),
    ("web-tui", check_webtui, False),
    ("ollama", check_ollama, False),
    ("obot-tunnel", check_obot_tunnel, False),
]


def main() -> int:
    # Brief settle retry: at boot the timer/target may fire before ports finish
    # binding. Retry the whole sweep a few times so a transient not-yet-ready
    # state doesn't raise a false alarm; give up (report DEGRADED) after that.
    import time

    attempts = int(os.environ.get("KARAOKE_HEALTH_RETRIES", "6"))
    delay = float(os.environ.get("KARAOKE_HEALTH_RETRY_DELAY", "5"))
    lines: list[str] = []
    worst_required_ok = True
    for attempt in range(1, attempts + 1):
        lines = []
        worst_required_ok = True
        for name, fn, required in CHECKS:
            try:
                status, detail = fn()
            except Exception as exc:  # never let a check crash the report
                status, detail = (FAIL if required else WARN), f"check raised: {exc}"
            req = "req" if required else "opt"
            lines.append(f"[{_MARK[status]}] {name:<14} ({req})  {detail}")
            if required and status == FAIL:
                worst_required_ok = False
        if worst_required_ok or attempt == attempts:
            break
        time.sleep(delay)

    header = "karaoke health: " + ("HEALTHY" if worst_required_ok else "DEGRADED")
    print(header)
    for l in lines:
        print("  " + l)
    return 0 if worst_required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
