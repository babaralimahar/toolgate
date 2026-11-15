from __future__ import annotations

import asyncio
import uuid
from collections import Counter

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.db import jobs
from app.adapters.db.jobs import ClaimedJob
from app.worker import Worker

SM = async_sessionmaker[AsyncSession]


async def status_of(sm: SM, job_id: uuid.UUID) -> dict[str, object]:
    async with sm() as s:
        job = await jobs.get_job(s, job_id)
    assert job is not None
    return job


async def test_enqueue_claim_complete(sm: SM) -> None:
    async with sm() as s:
        job_id, created = await jobs.enqueue(s, "echo", {"n": 1})
    assert created
    async with sm() as s:
        claimed = await jobs.claim(s, "w1")
    assert claimed is not None and claimed.id == job_id
    assert claimed.payload == {"n": 1} and claimed.attempts == 1
    async with sm() as s:
        assert await jobs.claim(s, "w2") is None  # nothing else queued
    async with sm() as s:
        assert await jobs.complete(s, job_id, "w1")
    assert (await status_of(sm, job_id))["status"] == "done"


async def test_idempotency_key_prevents_duplicates(sm: SM) -> None:
    async with sm() as s:
        a, created_a = await jobs.enqueue(s, "echo", {}, idempotency_key="invoice-123")
    async with sm() as s:
        b, created_b = await jobs.enqueue(s, "echo", {}, idempotency_key="invoice-123")
    assert a == b and created_a and not created_b
    async with sm() as s:
        count = await s.scalar(text("SELECT count(*) FROM jobs"))
    assert count == 1


async def test_two_workers_claim_100_jobs_exactly_once(sm: SM) -> None:
    async with sm() as s:
        await s.execute(
            text(
                "INSERT INTO jobs (kind, payload) "
                "SELECT 'echo', '{}'::jsonb FROM generate_series(1, 100)"
            )
        )
        await s.commit()

    async def drain(worker: str) -> list[uuid.UUID]:
        got: list[uuid.UUID] = []
        while True:
            async with sm() as s:
                job = await jobs.claim(s, worker)
            if job is None:
                return got
            got.append(job.id)
            await asyncio.sleep(0)  # let the other worker interleave
            async with sm() as s:
                assert await jobs.complete(s, job.id, worker)

    a, b = await asyncio.gather(drain("worker-a"), drain("worker-b"))
    counts = Counter(a + b)
    assert len(counts) == 100 and max(counts.values()) == 1
    assert a and b, "both workers should have claimed some jobs"
    async with sm() as s:
        done = await s.scalar(text("SELECT count(*) FROM jobs WHERE status = 'done'"))
    assert done == 100


async def test_stale_lock_is_reaped(sm: SM) -> None:
    async with sm() as s:
        job_id, _ = await jobs.enqueue(s, "echo")
    async with sm() as s:
        assert await jobs.claim(s, "dead-worker") is not None
    async with sm() as s:
        await s.execute(
            text("UPDATE jobs SET locked_at = now() - interval '10 minutes' WHERE id = :id"),
            {"id": job_id},
        )
        await s.commit()
    async with sm() as s:
        assert await jobs.reap(s, timeout_s=60) == 1
    assert (await status_of(sm, job_id))["status"] == "queued"
    # The dead worker can no longer complete it; a new worker can.
    async with sm() as s:
        assert not await jobs.complete(s, job_id, "dead-worker")
    async with sm() as s:
        again = await jobs.claim(s, "w2")
    assert again is not None and again.attempts == 2


async def test_fresh_lock_is_not_reaped(sm: SM) -> None:
    async with sm() as s:
        await jobs.enqueue(s, "echo")
    async with sm() as s:
        await jobs.claim(s, "w1")
    async with sm() as s:
        assert await jobs.reap(s, timeout_s=60) == 0


async def test_fail_backs_off_then_goes_dead(sm: SM) -> None:
    async with sm() as s:
        job_id, _ = await jobs.enqueue(s, "echo", max_attempts=2)
    async with sm() as s:
        await jobs.claim(s, "w1")
    async with sm() as s:
        assert await jobs.fail(s, job_id, "w1", "boom 1") == "queued"
    async with sm() as s:
        assert await jobs.claim(s, "w1") is None  # backoff: run_after is in the future
        await s.execute(text("UPDATE jobs SET run_after = now() WHERE id = :id"), {"id": job_id})
        await s.commit()
    async with sm() as s:
        await jobs.claim(s, "w1")
    async with sm() as s:
        assert await jobs.fail(s, job_id, "w1", "boom 2") == "dead"
    job = await status_of(sm, job_id)
    assert job["status"] == "dead" and job["last_error"] == "boom 2" and job["attempts"] == 2


async def test_reaper_marks_exhausted_jobs_dead(sm: SM) -> None:
    async with sm() as s:
        job_id, _ = await jobs.enqueue(s, "echo", max_attempts=1)
    async with sm() as s:
        await jobs.claim(s, "w1")
        await s.execute(
            text("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": job_id},
        )
        await s.commit()
    async with sm() as s:
        await jobs.reap(s, timeout_s=60)
    assert (await status_of(sm, job_id))["status"] == "dead"


async def test_worker_runs_handlers(sm: SM) -> None:
    seen: list[dict[str, object]] = []

    async def ok(job: ClaimedJob) -> None:
        seen.append(job.payload)

    async def broken(job: ClaimedJob) -> None:
        raise RuntimeError("nope")

    worker = Worker(sm, {"ok": ok, "broken": broken}, worker_id="w-test")
    async with sm() as s:
        good, _ = await jobs.enqueue(s, "ok", {"x": 1})
    async with sm() as s:
        bad, _ = await jobs.enqueue(s, "broken", max_attempts=1)
    async with sm() as s:
        unknown, _ = await jobs.enqueue(s, "no-such-kind", max_attempts=1)

    assert await worker.run_once()
    assert await worker.run_once()
    assert await worker.run_once()
    assert not await worker.run_once()

    assert seen == [{"x": 1}]
    assert (await status_of(sm, good))["status"] == "done"
    assert (await status_of(sm, bad))["status"] == "dead"
    unknown_job = await status_of(sm, unknown)
    assert unknown_job["status"] == "dead" and "UnknownJobKind" in str(unknown_job["last_error"])


async def test_worker_loop_stops_on_event(sm: SM) -> None:
    worker = Worker(sm, {}, poll_s=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
