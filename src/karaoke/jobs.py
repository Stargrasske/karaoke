"""Shared in-memory job-status registry for long-running background operations.

Each FastAPI process (the read-only Library API and the host-side Control
API) gets its own registry instance — jobs created in one process are only
visible to `GET /api/jobs/{job_id}` in that same process, which is why each
app mounts its own copy of the `/api/jobs/{job_id}` route backed by this
module. This is intentionally simple (no persistence, no cross-process
sharing): a single desktop/container instance is the only deployment target
today. See docs/api.md and docs/control-api.md.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Optional

JobStatus = str  # "pending" | "running" | "done" | "error"

_JOBS: dict[str, dict[str, Any]] = {}


def create_job() -> str:
    """Register a new pending job and return its id."""
    job_id = uuid.uuid4().hex[:16]
    _JOBS[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "progress": None,
        "result": None,
        "error": None,
        "created_at": time.time(),
    }
    return job_id


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    return _JOBS.get(job_id)


def run_job(job_id: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run `fn` synchronously (call from a BackgroundTasks worker), recording status.

    Intended as the `background.add_task` target in place of calling `fn` directly.
    """
    job = _JOBS.setdefault(job_id, {"job_id": job_id, "status": "pending"})
    job["status"] = "running"
    try:
        result = fn(*args, **kwargs)
        job["status"] = "done"
        job["result"] = result
    except Exception as exc:  # best-effort: never let a background task crash the loop
        job["status"] = "error"
        job["error"] = str(exc)
