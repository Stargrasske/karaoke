# Event Architecture Audit & Postgres Readiness Mapping

!!! note "Partially superseded — the Postgres cutover has happened"
    Verified 2026-09-17. The SQLite → Postgres migration described under
    "Stage 2" is **done** (see [Database backend](database.md)): the app runs
    on PostgreSQL 18.4 via a `psycopg3` connection pool, which resolves the
    write-concurrency and `database is locked` limitations below.

    The **unified `events` table** (`karaoke.event_store`) and the **outbox
    relay** (`karaoke.relay`) are both implemented. 25 444 events are
    backfilled from the legacy tables and fully relayed into the
    `karaoke-event-log` OpenSearch index. The relay wakes on `pg_notify`,
    retries failures and dead-letters undeliverable events. See
    [Unified event store](database.md#unified-event-store) and
    [The relay worker](database.md#the-relay-worker).

    The relay runs as `karaoke-relay.service` under `karaoke.target`.

    Still **not** implemented: a RabbitMQ sink, runtime emission from the
    analysis and alignment producers, logical-decoding publication, and any
    retention policy. The legacy per-domain tables are still written and remain
    the system of record.

    Note the audit below names `track_play_history` and
    `track_sources_history`, which exist in neither database — the real tables
    are `play_events`, `queue_events`, `track_analysis_history` and
    `alignment_support`.

## Current State

The karaoke system currently relies on a hybrid of SQLite, RabbitMQ, and in-memory streams for event handling. This creates several silos and scalability issues.

### 1. SQLite Event Tables
SQLite serves as the primary durable store for historical events.
- `track_play_history`: Records play count, duration, completion status, and user skips.
- `track_sources_history`: Tracks the provenance of sources (local, YouTube, Spotify, etc.) and changes to their status.
- `track_analysis_history`: Versions and records agreement on Key/BPM/Energy analysis results.
- `queue_events`: Logs user actions related to the queue (adds, removes, reorders, playlist synchronizations).
- `alignment_support`: Stores confidence and coverage metrics for lyric line-anchoring over time.

**Limitation**: SQLite's limited write concurrency (even in WAL mode) causes lock contention during bulk updates, such as batch background analysis.

### 2. Celery / RabbitMQ Asynchronous Events
Celery manages asynchronous background tasks (like post-processing and vector embedding) via RabbitMQ.
- **Task States**: RabbitMQ tracks task lifecycle events (`PENDING`, `STARTED`, `SUCCESS`, `FAILURE`, `RETRY`).
- **Flower Monitoring**: Flower consumes these events for monitoring, acting as a real-time but ephemeral observer.

**Limitation**: Task results and histories are currently persisted to a local SQLite database (`celery-results.sqlite`), splitting the operational event history from the main application database.

### 3. Real-Time Client Events (Web TUI & Kiosk)
Real-time interaction relies on ephemeral transport mechanisms.
- **WebSocket**: Synchronizes playback state between the server and the Web TUI.
- **Chrome DevTools Protocol (CDP)**: Manages kiosk status, browser automation, and screen synchronizations.

**Limitation**: There is no durable event log for client interactions. If a client disconnects, recovering the exact state requires querying the database rather than replaying an event stream.

---

## Stage 2: PostgreSQL Migration Target

To address these limitations and unify the architecture, Stage 2 will migrate the system to PostgreSQL, utilizing a unified event broker model.

### Unified Event Store
We will move away from disparate tracking tables in favor of a single `events` table (or a partitioned event store) in PostgreSQL.
- **Schema**:
  - `event_id` (UUID, Primary Key)
  - `aggregate_id` (UUID or String, e.g., `track_id`, `queue_id`)
  - `event_type` (String, e.g., `TRACK_PLAYED`, `QUEUE_ADDED`, `ANALYSIS_COMPLETED`)
  - `payload` (JSONB): Highly flexible storage for event-specific data.
  - `created_at` (Timestamp, Indexed)

### Outbox Pattern for External Consumers
To guarantee at-least-once delivery of events to external systems (like search indices or analytics pipelines) without two-phase commits:
- Application code will write domain changes and an `event` record to PostgreSQL in a single transaction.
- A background worker (the "Relay") will tail the `events` table (or use Postgres Logical Decoding / `pg_notify`) and publish these events to RabbitMQ or directly to consumers (like OpenSearch).

### Connection Pooling & Concurrency
- Replace standard synchronous SQLite connections with an async-compatible pool (e.g., `asyncpg` combined with SQLAlchemy 2.0 or `psycopg3`).
- This will eliminate the `database is locked` errors currently experienced during heavy bulk ingestion or concurrent Web TUI usage.

### Migration Strategy
1. **Schema Translation**: Map SQLite tables to PostgreSQL equivalents, converting boolean integers to true/false and utilizing `JSONB` for unstructured data.
2. **Dual-Write (Optional)**: Implement a transitional phase where writes go to both SQLite and PostgreSQL to verify integrity.
3. **Cutover**: Point the application's read/write connections entirely to PostgreSQL, migrating historical data via a bulk import script.
