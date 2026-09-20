# How-To: systemd services & health checks

The karaoke platform has several host-side pieces that must all be running for
"unified playing" to work. Instead of juggling terminals, this ships a set of
**systemd `--user` units** so everything starts together, restarts on failure,
and logs to **journald**. A health-check service + timer verifies the whole
stack every 5 minutes.

## Services

| Unit | What it does | Port |
|---|---|---|
| `karaoke-api.service` | FastAPI library backend (read-only tracks/lyrics/stats) | 8000 |
| `karaoke-ctrl-api.service` | Host-side playback control API (opens browser/Spotify) | 8765 |
| `karaoke-mq-forward.service` | `kubectl port-forward` for in-cluster RabbitMQ | 5672 / 15672 |
| `karaoke-celery-worker.service` | Celery post-processing worker (key/BPM + word-timing workflow tasks) | — |
| `karaoke-celery-flower.service` | Flower dashboard for Celery tasks/workers | 5555 |
| `karaoke-kiosk.service` | Google Chrome kiosk window running YT Music | 9222 |
| `karaoke-webtui.service` | Textual Web TUI server for the browser interface | 8001 |
| `karaoke-relay.service` | Outbox relay — forwards unified events to OpenSearch ([details](database.md#running-it-as-a-service)) | — |
| `karaoke-postprocess@1..6.service` | Legacy pika post-processing workers (rollback/manual debugging) | — |
| `karaoke-postprocess.slice` | Shared CPU/memory cap for all worker instances | — |
| `karaoke-healthcheck.service` | One-shot health probe (run by the timer) | — |
| `karaoke-healthcheck.timer` | Fires the health check every 5 min | — |
| `karaoke.target` | Umbrella — start/stop all of the above at once | — |

All units live in `deploy/systemd/` and are symlinked into
`~/.config/systemd/user/` by `make systemd-install` (so edits to the repo files
take effect after `systemctl --user daemon-reload`).

## Install & run

```bash
make systemd-install   # symlink units, daemon-reload, enable target + timer
make systemd-up        # start everything
make systemd-status    # unit status + last health check
make health            # run the health check right now (ad-hoc)
make systemd-down      # stop everything
make systemd-uninstall # stop + remove the units
```

`make systemd-install` also `enable`s `karaoke.target` and the timer, so the
platform comes back after a reboot/login. (For it to run without you being
logged in, enable lingering once: `loginctl enable-linger $USER`.)

These units are only one of four layers that have to come back. PostgreSQL,
the kind cluster and Ollama are system-level and start by their own means —
see [What survives a reboot](platform-services.md#5-what-survives-a-reboot)
for the full picture and the one-command check.

## Post-processing runners

`karaoke.target` starts `karaoke-celery-worker.service` and
`karaoke-celery-flower.service`. Celery consumes the RabbitMQ-backed
`karaoke-postprocess-celery` queue, gives every task retry/backoff/visibility,
stores task results in `~/.local/share/karaoke/celery-results.sqlite`, and Flower
exposes the dashboard at http://127.0.0.1:5555.

The legacy `karaoke-postprocess@.service` template is still installed for
rollback/manual debugging (`KARAOKE_ORCHESTRATOR=legacy`) but is no longer started
by `karaoke.target`.

The Celery worker runs in `karaoke-postprocess.slice`, currently capped at
`CPUQuota=600%` with low CPU/IO weight, so background analysis does not starve
browser playback.

Useful controls:

```bash
systemctl --user start karaoke-celery-worker karaoke-celery-flower
systemctl --user stop karaoke-celery-worker karaoke-celery-flower
systemctl --user status karaoke-postprocess.slice
```

## Logs (journald)

Every service logs to the journal under its `SyslogIdentifier`:

```bash
journalctl --user -u karaoke-api -f                 # follow the library API
journalctl --user -u karaoke-celery-worker -f       # follow Celery worker
journalctl --user -u karaoke-celery-flower -f       # follow Flower dashboard
journalctl --user -u karaoke-healthcheck -n 20      # recent health reports
journalctl --user -u 'karaoke-*' --since '10 min ago'  # everything, last 10 min
```

## The health check

`scripts/healthcheck.py` verifies each moving part and prints a compact report.
**Required** checks failing make the unit exit non-zero (so
`systemctl --user is-failed karaoke-healthcheck` and the journal both flag a
degraded platform); **optional** checks only warn.

| Check | Required? | Notes |
|---|---|---|
| library-api `:8000/health` | ✅ | Core read API. |
| control-api `:8765/health` | ⛔️ opt | Needs a desktop session. |
| rabbitmq-amqp `:5672` | ✅ | Needs `karaoke-mq-forward` up. |
| rabbitmq-mgmt `:15672` | ⛔️ opt | Management UI. |
| kind pods `Running` | ✅ | `karaoke` namespace. |
| kiosk-chrome CDP `:9222` | ⛔️ opt | Unified player window. |
| postgres-db | ✅ | Opens the DB + counts tracks. |
| relay-backlog | ⛔️ opt | Outbox draining, no dead letters. |

At boot the timer may fire before ports finish binding, so the check retries the
whole sweep a few times (env-tunable: `KARAOKE_HEALTH_RETRIES`,
`KARAOKE_HEALTH_RETRY_DELAY`) before reporting `DEGRADED`.

Sample output:

```
karaoke health: HEALTHY
  [✓] library-api    (req)  http://127.0.0.1:8000/health
  [✓] control-api    (opt)  http://127.0.0.1:8765/health
  [✓] rabbitmq-amqp  (req)  localhost:5672
  [✓] rabbitmq-mgmt  (opt)  http://127.0.0.1:15672
  [✓] kind-pods      (req)  2 pod(s) Running
  [✓] kiosk-chrome   (opt)  CDP :9222
  [✓] postgres-db    (req)  18244 tracks
  [✓] relay-backlog  (opt)  backlog empty
```

### The relay-backlog probe

It reports on the [outbox relay](database.md#the-relay-worker) in three states:

| Report | Meaning |
|---|---|
| `backlog empty` / `N pending, oldest Ns` | Draining normally. |
| `N event(s) pending, oldest Ns (> 600s) — is karaoke-relay running?` | Stalled. |
| `N dead letter(s) — karaoke-relay --status, then --retry-dead` | Parked; needs an operator. |

The stall test is backlog **age**, not size. A bulk import or a backfill
legitimately queues tens of thousands of events that drain in seconds, so
alerting on a count would cry wolf every time while still missing a relay that
quietly died with three events pending. Tune the threshold with
`KARAOKE_HEALTH_RELAY_MAX_AGE` (default 600s).

The probe is **optional** on purpose. A stalled relay stops *forwarding*
events, but nothing is lost — they stay in Postgres with `published_at IS NULL`
and deliver once it recovers. Playback, search and the TUI are unaffected, so
this must not mark the whole platform `DEGRADED`.

!!! warning "`postgres-db` was reporting `DB error: 0`"
    The required DB probe used `fetchone()[0]`, which raises `KeyError: 0`
    now that the pool returns dict rows — so the health check reported
    `DEGRADED` on every run after the Postgres cutover. Fixed, and the check
    renamed from `sqlite-db` to match the backend.

## Reloading & Code Updates

### 1. Systemd Unit Configurations
Edits to systemd unit configurations (such as modifying `.service` files under `deploy/systemd/`) do not apply automatically. After refreshing or changing these repo files, you must instruct systemd to pick up the changes:
```bash
make systemd-install   # Automatically links and runs daemon-reload
# or manually:
systemctl --user daemon-reload
```

### 2. Python Code Changes (Editable Installs)
Because services run the host-side Python interpreter pointing directly to active files, systemd-managed processes do not automatically reload when you update python code. To activate newly edited code, the respective systemd service(s) must be explicitly restarted:
```bash
# Restart the core API, control API, and Celery worker
systemctl --user restart karaoke-api.service karaoke-ctrl-api.service karaoke-celery-worker.service
```

---

## Admin TUI Service Relations

The **Admin TUI** (`make admin`, backed by `src/karaoke/admin_tui.py`) serves as the active control center for the background platform services, allowing real-time monitoring and lifecycle operations.

### 1. Direct systemd Fallback Status Checking
The Admin TUI periodically polls the platform event ledger and worker pool status:
- Under normal operation, it polls these stats gracefully over HTTP from the Control API (`ctrl_api` on `:8765`).
- If the Control API is offline or unreachable, the Admin TUI seamlessly falls back to querying the host systemd state directly using direct `systemctl --user is-active` command shell-outs. This ensures you can always see whether background processes are alive.

### 2. Live Worker and Kiosk Control
The Admin TUI maps keyboard shortcuts directly to background service state changes via `ctrl_api` (routing requests to `/api/workers/scale` and `/api/players/window/restart` which wrap `systemctl` and process management under the hood):
- **Scale Up (`+` / Worker +1)**: Automatically spins up background post-processing by running `systemctl --user start karaoke-celery-worker.service`.
- **Scale Down (`-` / Worker -1)**: Shuts down background post-processing to conserve system resources by running `systemctl --user stop karaoke-celery-worker.service`.
- **Worker Restart (`R` / Restart Workers)**: Performs a clean reboot of worker processing capacity.
- **Restart Kiosk (`K` / Restart Kiosk Chrome)**: Restarts the Google Chrome kiosk playback window. It tries running `systemctl --user restart karaoke-kiosk.service` first; if the systemd unit is unavailable, it falls back to killing active CDP-enabled Chrome processes (`port :9222`) and spawning a fresh, isolated background process. Note that Chrome startup is fully decoupled from `make tui` and handled entirely via this background/systemd lifecycle.
- **Web UI Control (`X` / Stop Web UI)**: Shuts down active `web_serve.py` (textual-serve) server instances via standard `SIGTERM` signals, letting browser tab sockets disconnect gracefully.

### Web TUI reloads

The Web TUI is managed by `karaoke-webtui.service` on port `8001`:

```bash
systemctl --user restart karaoke-webtui.service
systemctl --user status karaoke-webtui.service --no-pager
journalctl --user -u karaoke-webtui.service -f
```

After changing Python code, restart the Web TUI service. The kiosk browser does
not need to be restarted unless its browser-side state or CDP connection needs
resetting. `make systemd-install` refreshes the symlinks and daemon metadata.


## Configuration

Units read the same env vars as the app; override per-unit with
`systemctl --user edit <unit>` drop-ins, or globally via the unit files:

| Var | Default | Used by |
|---|---|---|
| `KARAOKE_API_PORT` | `8000` | library API |
| `KARAOKE_CTRL_PORT` | `8765` | control API |
| `RABBITMQ_HOST` | `localhost` | worker, Flower |
| `KARAOKE_ORCHESTRATOR` | `celery` | publisher/worker selection (`legacy` rolls back to pika) |
| `KARAOKE_CELERY_POSTPROCESS_QUEUE` | `karaoke-postprocess-celery` | Celery worker queue |
| `CELERY_RESULT_BACKEND` | SQLite result DB | persistent Celery result backend override |
| `KARAOKE_CELERY_RESULT_DB` | `~/.local/share/karaoke/celery-results.sqlite` | default SQLite result DB |
| `KARAOKE_CELERY_RESULT_EXPIRES` | `604800` | result retention seconds |
| `KARAOKE_COOKIES_FROM_BROWSER` | `firefox` | worker (YouTube auth) |
| `KUBE_CONTEXT` | `kind-karaoke` | port-forward, health check |
| `K8S_NAMESPACE` | `karaoke` | port-forward, health check |

## Pitfalls

- **Port already in use:** if you previously ran `make api` / `make mq-port-forward`
  in a terminal, stop those first — systemd can't bind a port another process
  holds. `ss -lntp | grep -E ':8000|:8765|:5672'` shows the culprit.
- **`karaoke-mq-forward` restarts a lot:** expected if the kind cluster or the
  `karaoke` namespace isn't up yet. Bring the cluster up first
  (`kubectl --context kind-karaoke get ns karaoke`).
- **Health check fails right after `systemd-up`:** the boot-time run can race the
  ports; the built-in retry usually clears it. Re-run `make health` to confirm.
- **Nothing runs after reboot without a login:** enable lingering once with
  `loginctl enable-linger $USER`.
