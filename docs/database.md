# Database backend

The karaoke platform stores its operational data (tracks, sources, lyrics,
analysis, genre/tone, recordings, play stats, saved playlists) in a local
**PostgreSQL** database. OpenSearch remains a *derived* index used only for
semantic/vector search and is always rebuilt from this database.

!!! info "Migration status — complete (cutover)"
    The SQLite → Postgres migration has been **cut over**. Postgres is now the
    only backend the application opens at runtime; SQLite is no longer used
    outside the one-time migration script. See
    [Migration status](#migration-status) for what moved and what is left.

## PostgreSQL (current backend)

| Setting | Value |
|---|---|
| Server | PostgreSQL 18.4 |
| Database / role | `karaoke` / `karaoke` |
| Driver | `psycopg` 3.3.5 (`psycopg[binary,pool]>=3.0.0`) |
| Pool | `psycopg_pool.ConnectionPool` — `min_size=1`, `max_size=500` |
| Connection style | `autocommit=True`, `row_factory=dict_row` |

`localcache.connect()` leases a connection from a process-wide pool created by
`get_pool()`. The returned connection's `close()` is rebound to return it to the
pool (rolling back first) rather than actually closing the socket, so existing
`with connect() as conn:` call sites keep working unchanged.

Rows come back as dicts (`row_factory=dict_row`), which is why call sites use
`row["column"]` rather than tuple indexing.

### Configuration

The connection string is read **directly from the environment** by
`localcache.get_pool()`:

| Env var | Default | Purpose |
|---|---|---|
| `KARAOKE_PG_URL` | `postgresql://karaoke:karaoke@localhost:5432/karaoke` | Postgres connection string. This is the only variable that affects which database is opened. |
| `KARAOKE_DATA_DIR` | `~/.local/share/karaoke` | Caches, artwork and the legacy `karaoke.db` snapshot. |

!!! warning "`KARAOKE_DB_BACKEND` and `KARAOKE_DB_URL` are vestigial"
    `config.py` still parses `KARAOKE_DB_BACKEND` (default `sqlite`) and
    `KARAOKE_DB_URL` into `settings`, and `settings.uses_postgres` still exists.
    **Nothing in the connection path reads them.** `localcache` sets a
    module-level `_IS_PG = True` and `connect()` always returns a pooled
    Postgres connection — including when passed an explicit `db_path`, which is
    now ignored. Setting `KARAOKE_DB_BACKEND=sqlite` does *not* give you SQLite.
    Use `KARAOKE_PG_URL` to point at a different database; the older two
    variables are pending removal.

### Checking the connection

```bash
psql "$KARAOKE_PG_URL" -c '\dt'                       # 29 tables expected
psql "$KARAOKE_PG_URL" -c 'SELECT count(*) FROM tracks;'
```

## Migration status

### What moved

`scripts/migrate_sqlite_to_postgres.py` creates the Postgres schema and copies
28 tables. Current row counts (Postgres, versus the frozen SQLite snapshot):

| Table | Postgres | SQLite snapshot |
|---|---|---|
| `tracks` | 18 242 | 18 244 |
| `sources` | 38 075 | 38 084 |
| `lyrics` | 10 372 | 10 359 |
| `track_analysis` | 17 803 | 17 805 |
| `track_analysis_history` | 22 380 | 22 365 |
| `track_genre` | 13 516 | 13 515 |
| `lyric_gaps` | 1 399 | 1 346 |
| `cover_art` | 1 290 | 1 263 |
| `track_art` | 1 550 | 1 508 |
| `track_tone` | 1 166 | 1 134 |
| `recording_marks` | 1 084 | 1 084 |
| `artist_genres` | 923 | 923 |
| `saved_playlist_tracks` | 701 | 701 |
| `queue_events` | 652 | 594 |
| `alignment_support` | 369 | 375 |
| `play_events` | 2 385 | 2 380 |
| `saved_searches` | 68 | 46 |
| `radio_session_tracks` | 67 | 67 |
| `saved_playlists` | 24 | 24 |
| `track_sync_offsets` | 25 | 24 |

Postgres now leads the snapshot on most tables because it has been the live
store since cutover; `~/.local/share/karaoke/karaoke.db` is a frozen
2026-09-16 copy kept only as a fallback.

The tables originally omitted from the migrator — `saved_playlists`,
`saved_playlist_tracks`, `artist_genres`, `saved_searches`, `radio_sessions`,
`radio_session_tracks` and `queue_events` — are now migrated and populated. The
"No saved playlists found" bug behind the TUI `L` key is resolved: all 24
playlists load.

### Where Postgres counts are lower

A few tables have marginally fewer rows than the snapshot. This is deliberate,
not data loss: the migrator pre-fetches valid `tracks.track_id` and
`recordings.recording_id` keys and **skips orphan rows** that would violate the
Postgres foreign keys. SQLite never enforced those references, so the snapshot
contains rows pointing at deleted parents. The residue also includes test
fixtures (`Alpha :: Song A`, `A Rocker 00 :: Rock Song 0`) that leaked into the
SQLite file from earlier test runs.

### Schema translation notes

- `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`.
- `?` placeholders → `%s`; named placeholders use `%(name)s`.
- `PRAGMA table_info(...)` introspection → `information_schema.columns`.
- `PRAGMA`/WAL tuning is gone; `_tune_connection()` is now a no-op.
- Epoch timestamps on `saved_searches` / `saved_playlists` use
  `DOUBLE PRECISION`, not `REAL`. Postgres `REAL` is single-precision and
  rounds epoch values (~1.78e9) to ~128-second buckets, which collapsed
  `created_at`/`updated_at`/`last_used_at` into collisions and broke sorting.
- `clean_postgres_string()` strips `\x00` bytes, which SQLite tolerates in TEXT
  but Postgres rejects.
- The staging cache key separator changed from a literal `NUL` to `U+2400`
  (`␀`) for the same reason.

### Remaining work

- `active_queue_state` is not in the migrator's `TABLES` list. It is created and
  populated by the application's own schema setup, so this is harmless for a
  fresh cutover but means the table is not carried across by a re-run.
- Remove the vestigial `KARAOKE_DB_BACKEND` / `KARAOKE_DB_URL` settings and the
  `uses_postgres` property, or wire them to `get_pool()`. Two
  `tests/test_config.py` cases still assert the old selector semantics.
- The connection pool is never explicitly closed, so interpreter shutdown can
  emit `PythonFinalizationError: cannot join thread` from
  `ConnectionPool.__del__`. Cosmetic — it happens after all work completes —
  but it pollutes CLI and test output.
- Celery results still persist to a separate `celery-results.sqlite`.

## Unified event store

`karaoke.event_store` implements the single `events` table from the
[Events & Postgres migration plan](events_and_postgres_plan.md). Every domain
appends to it, so a track's history can be replayed in order across domains
instead of being reassembled from four tables with four different timestamp
columns.

!!! note "Not `karaoke.events`"
    `karaoke.events` is a different module: it *reads* the derived
    `karaoke-events` OpenSearch index that Argo Events writes from Celery task
    lifecycle. `event_store` is the durable system-of-record on the host.

### Schema

| Column | Type | Purpose |
|---|---|---|
| `seq` | `BIGSERIAL` | Insertion order, for ordered replay of one stream. |
| `event_id` | `UUID` PK | Idempotency key. |
| `aggregate_type` | `TEXT` | `track`, `track_key`, `queue`, `session`, `playlist`. |
| `aggregate_id` | `TEXT` | Which entity the event is about. |
| `event_type` | `TEXT` | `TRACK_PLAYED`, `QUEUE_ADVANCED`, `ANALYSIS_COMPLETED`, … |
| `payload` | `JSONB` | Event-specific data. |
| `created_at` | `TIMESTAMPTZ` | When it happened. |
| `published_at` | `TIMESTAMPTZ` | Outbox state; `NULL` means not yet forwarded. |
| `attempts` | `INTEGER` | Failed delivery attempts; past `MAX_ATTEMPTS` the event is a dead letter. |
| `last_error` | `TEXT` | Why the last attempt failed. |
| `failed_at` | `TIMESTAMPTZ` | When it last failed. |

The plan keyed events on `aggregate_id` alone. That is ambiguous — a bare `42`
could be track 42 or radio session 42 — so `aggregate_type` is stored alongside
it and every index leads with it.

`seq` is deliberately **not** the relay cursor. Concurrent inserts make sequence
values visible out of order, so a high-water-mark cursor can skip a straggler
that commits late. The relay claims on `published_at IS NULL` instead, which is
gap-safe.

### Transactional guarantee

The pool runs `autocommit=True`, so a bare `append()` commits on its own. The
outbox guarantee needs an explicit transaction:

```python
with conn.transaction():
    conn.execute("UPDATE tracks SET play_count = play_count + 1 WHERE ...")
    event_store.append(conn, "track", track_id, event_store.TRACK_PLAYED, {...})
```

If either statement raises, neither is durable and no half-published event
escapes. `localcache.log_event` and `localcache.record_queue_event` are both
wired this way — the legacy row, the denormalised counter and the unified event
commit together.

### Aggregate keying, and `track_key`

Some producers never learn a `track_id`: a radio `cache_miss` fires for songs
that are not in the library at all. Those events are keyed by normalised
artist/title under `aggregate_type = 'track_key'` rather than being dropped.
`event_store.resolve_track_keys()` folds them onto the real id once the scanner
imports the track, so replay sees one ordered history instead of two fragments.

`log_event` gets the id for free via `RETURNING` on the play-count `UPDATE` it
already ran, so the common path costs no extra query.

### Idempotency

`deterministic_id()` derives a `uuid5` from a natural key, so a replayable
producer generates the same id for the same logical event and
`ON CONFLICT DO NOTHING` makes the write a no-op. This is what makes the
backfill re-runnable.

### Backfilling history

```bash
python scripts/backfill_event_store.py --dry-run   # report, write nothing
python scripts/backfill_event_store.py             # append + re-key
```

It reads `play_events`, `queue_events` and `track_analysis_history`, leaving
the legacy tables untouched — they remain the system of record until the relay
and its consumers are proven against the unified log. Current state after
backfill: **25 439 events**, of which 1 794 were re-keyed from `track_key` onto
real track ids.

| Event type | Count |
|---|---|
| `ANALYSIS_COMPLETED` | 22 386 |
| `TRACK_PLAYED` | 1 070 |
| `TRACK_RELOCKED` | 691 |
| `QUEUE_ADVANCED` | 474 |
| `TRACK_CACHE_MISS` | 288 |
| `TRACK_DISCOVERED` | 183 |
| `QUEUE_FOLLOWED` | 151 |
| `TRACK_LYRICS_MISSING` | 87 |
| `TRACK_CACHE_HIT` | 70 |
| `QUEUE_SYNCED` | 22 |
| `QUEUE_LOADED` | 17 |

## The relay worker

`karaoke.relay` is the consumer half of the outbox: it tails `events` and
forwards each one onward, giving consumers at-least-once delivery without a
two-phase commit.

```bash
karaoke-relay                       # run until SIGINT/SIGTERM
karaoke-relay --once                # drain the backlog and exit
karaoke-relay --status              # backlog + dead-letter counts
karaoke-relay --sink log --echo     # dry run: print events, deliver nowhere
karaoke-relay --retry-dead          # requeue dead letters after a fix
```

| Flag | Default | Purpose |
|---|---|---|
| `--sink` | `opensearch` | Destination: `opensearch` or `log`. |
| `--batch-size` | 200 | Events claimed per cycle. |
| `--interval` | 5.0 | Idle wait if no `NOTIFY` arrives. |
| `--max-attempts` | 5 | Failures before an event is parked. |

### Delivery loop

Each cycle runs in one transaction: **claim** (`FOR UPDATE SKIP LOCKED`) →
**publish** → **mark published**. If the publish raises, the transaction rolls
back, nothing is marked, the locks drop and the batch retries next cycle.

Because the publish happens *before* the commit, a crash in that window
redelivers the event. That is the at-least-once guarantee the plan asked for,
and it is why sinks must be idempotent — `OpenSearchSink` uses the event id as
the document id, so a redelivery overwrites rather than duplicates. Verified:
publishing the same batch three times leaves the document count unchanged.

### Dead letters

The attempt counter is bumped in a **separate** transaction after the failed
one rolls back. Inside it, the increment would roll back too and the event
would retry forever without ever reaching the threshold.

Past `--max-attempts` an event stops being claimed and becomes a dead letter,
so one undeliverable event cannot starve everything queued behind it.
`karaoke-relay --status` lists them with the error that parked them;
`--retry-dead` resets the counter once the consumer is fixed.

!!! warning "A non-zero dead-letter count is the thing to alert on"
    Events are never dropped — they stay in `events` with `published_at IS
    NULL` — but a parked event is not reaching consumers and will not retry on
    its own.

### Waking up

A statement-level trigger on `events` fires `pg_notify`, and the relay
`LISTEN`s on `karaoke_events`. `NOTIFY` is transactional — it fires on commit
and not on rollback — which is the outbox's own guarantee, so a rolled-back
event never wakes anyone. The trigger is statement-level, not row-level, so a
1000-row backfill batch sends one notification rather than a thousand.

The poll interval remains as a backstop, so correctness never depends on the
notification arriving.

!!! danger "Never abandon the `notifies()` generator early"
    `_wait_for_event` consumes `conn.notifies(timeout=…, stop_after=1)` to
    exhaustion via `list()`. Breaking or returning out of the loop instead
    triggers generator close, which **blocks indefinitely** — wedging the relay
    on the exact wake-up path it exists to serve. `tests/test_relay.py`
    pins this.

### Sinks

`OpenSearchSink` (default) bulk-indexes into **`karaoke-event-log`**. That is
deliberately *not* `karaoke-events`, which Argo Events owns and fills with
Celery task documents of a different shape (`task_id`, `task_name`, `state`);
mixing two schemas into one index would break both the mapping and
`karaoke.events.recent_events`.

`LogSink` delivers nowhere and is the dry-run path. Adding a RabbitMQ sink
means implementing the `Sink` protocol — `name` plus `publish(events)` that
raises to trigger a retry — and registering it in `SINKS`.

### Current state

The full backlog has been relayed: **25 444 events** in Postgres, 25 444
documents in `karaoke-event-log`, 0 claimable, 0 dead letters. The initial
drain took 4.7s at `--batch-size 500`.

### Running it as a service

`karaoke-relay.service` runs the relay under `systemd --user`, bound to
`karaoke.target` like every other host-side service:

```bash
make systemd-install                      # symlink + daemon-reload
systemctl --user start karaoke-relay      # or: make systemd-up
systemctl --user status karaoke-relay
```

It sits in `karaoke-postprocess.slice` at `Nice=10`, so relaying never competes
with playback. At idle it is parked on a Postgres `NOTIFY` and costs
essentially nothing (~30 MB resident).

`Restart=always` with `RestartSec=10` covers the dependency ordering that a
user unit cannot express: Postgres and OpenSearch are *system* services, so
this unit cannot `After=` them. It does not need to — a Postgres that is not
up yet fails the initial connect and systemd retries, and an OpenSearch that is
down is a `SinkUnavailable` that the relay backs off on.

### Monitoring

The platform health check probes the relay as `relay-backlog` — it flags a
stalled relay (backlog older than 10 minutes) and any dead letters:

```bash
python scripts/healthcheck.py | grep relay-backlog
karaoke-relay --status                    # detail, including parked events
```

See [the probe's behaviour](howto-systemd-services.md#the-relay-backlog-probe)
for the three states and why it is an optional rather than required check.

!!! tip "Logs go to the log file, not the journal"
    The platform logger writes to `~/.local/share/karaoke/logs/karaoke.log`, so
    `journalctl --user -u karaoke-relay` shows only start/stop lines. For
    delivery activity:

    ```bash
    grep 'relay:' ~/.local/share/karaoke/logs/karaoke.log | tail
    ```

`SIGTERM` is handled: the relay finishes its current batch and exits cleanly
(measured ~1.4s), so a restart never abandons a claimed batch mid-publish.

### Outages versus poison events

The relay distinguishes two failure kinds, and the difference decides whether
an attempt is charged against the event:

| Failure | Sink raises | Effect |
|---|---|---|
| Destination unreachable | `SinkUnavailable` | Backoff, retried forever, **attempts unchanged** |
| Batch rejected | any other exception | Attempt counted, dead-letters past `--max-attempts` |

Without that split, a routine OpenSearch restart would park a healthy backlog:
at a 5-second loop and 5 attempts, everything claimable dead-letters inside a
minute. Instead the relay backs off exponentially from `--interval` to a
60-second ceiling and leaves the backlog claimable. Verified: ten cycles
against a down sink leave every attempt counter at zero, and all events deliver
once it returns.

### Remaining work

- Emit from the remaining producers. `alignment_support` and the analysis
  writers still only write their own tables at runtime; only their *history*
  is backfilled.
- Decide the retention story. `events` grows without bound and
  `ANALYSIS_COMPLETED` is already 88% of it, so it will distort relay
  throughput before it distorts storage. Note the `relay-backlog` probe
  measures backlog age, not table size, so it will not warn about this.
- Once consumers read only from `events`, retire the legacy tables.

## Re-running the migration

The migrator is idempotent on conflicts but expects to load into the existing
schema. To rebuild from the SQLite snapshot:

```bash
python scripts/migrate_sqlite_to_postgres.py
```

It reads `~/.local/share/karaoke/karaoke.db` and writes to
`postgresql://karaoke@localhost/karaoke` (both currently hardcoded in `main()`).
After a reload, rebuild the derived OpenSearch index — it is never migrated.
