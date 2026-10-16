"""Durable pipeline job queue on the application database.

A submitted case becomes a row in `pipeline_jobs`. Workers claim rows with
`SELECT ... FOR UPDATE SKIP LOCKED`, so any number of workers can poll without claiming the same
job, and hold a lease they renew while the pipeline runs. If a worker dies, its lease lapses and
another worker picks the job up. After `job_max_attempts` the case is moved to
NEEDS_MANUAL_PROCESSING with a message a person can act on - never left spinning.

No broker to run, and enqueueing commits in the same database as the case itself.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Engine, and_, insert, or_, select, update

from app.db import is_postgres, jobs, sqlite_write_lock

log = logging.getLogger("artha.jobs")

ACTIVE_STATES = ("queued", "running")


@dataclass(frozen=True)
class Claim:
    job_id: str
    case_id: str
    attempt: int
    exhausted: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobQueue:
    def __init__(self, engine: Engine, *, lease_seconds: int = 300, max_attempts: int = 3) -> None:
        self.engine = engine
        self.lease = timedelta(seconds=lease_seconds)
        self.max_attempts = max_attempts
        self._pg = is_postgres(engine)

    def _guard(self):  # type: ignore[no-untyped-def]
        return nullcontext() if self._pg else sqlite_write_lock

    def enqueue(self, case_id: str) -> str:
        """Queue a case. Idempotent while a job for the case is still queued or running."""
        with self._guard(), self.engine.begin() as conn:
            existing = conn.execute(select(jobs.c.job_id).where(jobs.c.case_id == case_id,
                                                                jobs.c.state.in_(ACTIVE_STATES))
                                    .with_for_update()).scalar()
            if existing:
                return str(existing)
            job_id = f"job-{uuid.uuid4().hex[:16]}"
            now = _now()
            conn.execute(insert(jobs).values(job_id=job_id, case_id=case_id, state="queued", attempts=0,
                                             created_at=now, updated_at=now))
            return job_id

    def claim(self, worker_id: str) -> Claim | None:
        now = _now()
        with self._guard(), self.engine.begin() as conn:
            row = conn.execute(
                select(jobs.c.job_id, jobs.c.case_id, jobs.c.attempts)
                .where(or_(jobs.c.state == "queued",
                           and_(jobs.c.state == "running", jobs.c.lease_until < now)))
                .order_by(jobs.c.created_at).limit(1)
                .with_for_update(skip_locked=True)).first()
            if row is None:
                return None
            if row.attempts >= self.max_attempts:
                conn.execute(update(jobs).where(jobs.c.job_id == row.job_id).values(
                    state="failed", lease_until=None, updated_at=now,
                    last_error="gave up after repeated worker failures"))
                return Claim(row.job_id, row.case_id, row.attempts, exhausted=True)
            conn.execute(update(jobs).where(jobs.c.job_id == row.job_id).values(
                state="running", attempts=row.attempts + 1, worker_id=worker_id[:80],
                lease_until=now + self.lease, updated_at=now))
            return Claim(row.job_id, row.case_id, row.attempts + 1)

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        """Extend the lease. False means another worker has taken the job over."""
        now = _now()
        with self._guard(), self.engine.begin() as conn:
            result = conn.execute(update(jobs).where(jobs.c.job_id == job_id, jobs.c.worker_id == worker_id[:80],
                                                     jobs.c.state == "running")
                                  .values(lease_until=now + self.lease, updated_at=now))
            return result.rowcount > 0

    def finish(self, job_id: str, worker_id: str, *, ok: bool, error: str | None = None) -> None:
        with self._guard(), self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.job_id == job_id, jobs.c.worker_id == worker_id[:80]).values(
                state="done" if ok else "failed", lease_until=None, updated_at=_now(),
                last_error=(error or None) and error[:300]))

    def state(self, job_id: str) -> str | None:
        with self.engine.connect() as conn:
            return conn.execute(select(jobs.c.state).where(jobs.c.job_id == job_id)).scalar()

    def depth(self) -> dict[str, int]:
        from sqlalchemy import func

        with self.engine.connect() as conn:
            rows = conn.execute(select(jobs.c.state, func.count()).group_by(jobs.c.state)).all()
        return {r[0]: int(r[1]) for r in rows}


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        from app.config import get_settings
        from app.db import get_engine

        settings = get_settings()
        _queue = JobQueue(get_engine(), lease_seconds=settings.job_lease_seconds,
                          max_attempts=settings.job_max_attempts)
    return _queue
