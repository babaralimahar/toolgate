"""Concurrency-safe Postgres job queue (FOR UPDATE SKIP LOCKED).

Delivery is at-least-once: a job can run twice if a worker dies after doing the work but
before ``complete``. Handlers' side effects must therefore be idempotent.

Every function commits its own unit of work. A session the caller already used (e.g. the
request session that ran the auth lookup) is fine: its open read transaction is committed too.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def _unit_of_work(session: AsyncSession) -> AsyncIterator[None]:
    """Commit on success, roll back on error, whether or not a transaction was already open."""
    try:
        yield
    except BaseException:
        await session.rollback()
        raise
    await session.commit()


ENQUEUE = text("""
INSERT INTO jobs (kind, payload, idempotency_key, max_attempts, run_after)
VALUES (:kind, CAST(:payload AS jsonb), :ikey, :max_attempts,
        now() + make_interval(secs => :delay_s))
ON CONFLICT (idempotency_key) DO NOTHING
RETURNING id
""")

CLAIM = text("""
UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = now(), attempts = attempts + 1
WHERE id = (
    SELECT id FROM jobs
    WHERE status = 'queued' AND run_after <= now()
    ORDER BY run_after
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, kind, payload, attempts, max_attempts
""")

COMPLETE = text("""
UPDATE jobs SET status = 'done', locked_by = NULL, locked_at = NULL, finished_at = now(),
                last_error = NULL
WHERE id = :id AND status = 'running' AND locked_by = :worker
""")

FAIL = text("""
UPDATE jobs SET
  status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
  run_after = now() + make_interval(secs => LEAST(300, power(2, attempts) * 5)),
  finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
  locked_by = NULL, locked_at = NULL, last_error = :err
WHERE id = :id AND status = 'running' AND locked_by = :worker
RETURNING status
""")

# A job whose lock is older than the timeout belonged to a worker that died or hung.
# Jobs that already used all attempts go to 'dead' instead of looping forever.
REAP = text("""
UPDATE jobs SET
  status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
  locked_by = NULL, locked_at = NULL,
  last_error = COALESCE(last_error, 'lock expired')
WHERE status = 'running' AND locked_at < now() - make_interval(secs => :timeout_s)
""")


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    max_attempts: int = 5,
    delay_s: float = 0.0,
) -> tuple[uuid.UUID, bool]:
    """Insert a job. Returns (job_id, created). A repeated idempotency key returns the
    existing job with ``created=False`` instead of inserting a duplicate."""
    async with _unit_of_work(session):
        new_id = await session.scalar(
            ENQUEUE,
            {
                "kind": kind,
                "payload": json.dumps(payload or {}),
                "ikey": idempotency_key,
                "max_attempts": max_attempts,
                "delay_s": float(delay_s),
            },
        )
        if new_id is not None:
            return new_id, True
        existing = await session.scalar(
            text("SELECT id FROM jobs WHERE idempotency_key = :ikey"), {"ikey": idempotency_key}
        )
        if existing is None:  # only possible if the conflicting row was deleted concurrently
            raise RuntimeError("idempotent enqueue found neither a new nor an existing job")
        return existing, False


async def claim(session: AsyncSession, worker: str) -> ClaimedJob | None:
    async with _unit_of_work(session):
        row = (await session.execute(CLAIM, {"worker": worker})).mappings().first()
    if row is None:
        return None
    return ClaimedJob(
        id=row["id"],
        kind=row["kind"],
        payload=_as_dict(row["payload"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


async def complete(session: AsyncSession, job_id: uuid.UUID, worker: str) -> bool:
    """Mark done. Returns False if this worker no longer holds the lock (it was reaped)."""
    async with _unit_of_work(session):
        res = cast(
            CursorResult[Any], await session.execute(COMPLETE, {"id": job_id, "worker": worker})
        )
    return res.rowcount == 1


async def fail(session: AsyncSession, job_id: uuid.UUID, worker: str, err: str) -> str | None:
    """Record a failure and schedule a retry with backoff (or mark dead).
    Returns the new status, or None if this worker no longer holds the lock."""
    async with _unit_of_work(session):
        status = await session.scalar(FAIL, {"id": job_id, "worker": worker, "err": err[:2000]})
    return str(status) if status is not None else None


async def reap(session: AsyncSession, timeout_s: float) -> int:
    """Re-queue jobs whose lock has expired. Returns how many were reaped."""
    async with _unit_of_work(session):
        res = cast(CursorResult[Any], await session.execute(REAP, {"timeout_s": float(timeout_s)}))
    return res.rowcount


REQUEUE = text("""
UPDATE jobs SET status = 'queued', attempts = 0, run_after = now(), locked_by = NULL,
                locked_at = NULL, finished_at = NULL
WHERE id = :id AND status = 'dead'
""")


async def requeue(session: AsyncSession, job_id: uuid.UUID) -> bool:
    """Operator action: give a dead-lettered job a fresh set of attempts."""
    async with _unit_of_work(session):
        res = cast(CursorResult[Any], await session.execute(REQUEUE, {"id": job_id}))
    return res.rowcount == 1


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, kind, status, attempts, max_attempts, last_error, created_at, "
                    "finished_at FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None
