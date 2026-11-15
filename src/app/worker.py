"""Background worker: claims jobs, runs handlers, completes/fails them, reaps stale locks.

Runs embedded in the web process (WORKER_MODE=embedded, for free hosting tiers) or as its
own process: ``python -m app.worker`` (WORKER_MODE=standalone).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable, Mapping

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.db import jobs
from app.adapters.db.jobs import ClaimedJob
from app.observability.metrics import JOBS

Handler = Callable[[ClaimedJob], Awaitable[None]]
log = structlog.get_logger()


class UnknownJobKind(Exception): ...


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        handlers: Mapping[str, Handler],
        *,
        worker_id: str | None = None,
        poll_s: float = 1.0,
        lock_timeout_s: float = 300.0,
        reap_every_s: float = 30.0,
    ) -> None:
        self._sm = sessionmaker
        self._handlers = handlers
        self.id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{id(self) & 0xFFFF:04x}"
        self._poll_s = poll_s
        self._lock_timeout_s = lock_timeout_s
        # Stop a handler before its lock expires, so the reaper never hands a job that is
        # still running to a second worker.
        self._handler_timeout_s = lock_timeout_s * 0.8
        self._reap_every_s = reap_every_s

    async def run_once(self) -> bool:
        """Process at most one job. Returns True if a job was claimed."""
        async with self._sm() as s:
            job = await jobs.claim(s, self.id)
        if job is None:
            return False

        bound = log.bind(job_id=str(job.id), kind=job.kind, attempt=job.attempts)
        started = time.perf_counter()
        try:
            handler = self._handlers.get(job.kind)
            if handler is None:
                raise UnknownJobKind(job.kind)
            async with asyncio.timeout(self._handler_timeout_s):
                await handler(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            async with self._sm() as s:
                status = await jobs.fail(s, job.id, self.id, f"{type(e).__name__}: {e}")
            JOBS.labels(kind=job.kind, status=status or "lost_lock").inc()
            bound.warning("job_failed", error=type(e).__name__, new_status=status)
        else:
            async with self._sm() as s:
                ok = await jobs.complete(s, job.id, self.id)
            JOBS.labels(kind=job.kind, status="done" if ok else "lost_lock").inc()
            bound.info(
                "job_done" if ok else "job_done_lock_lost",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        return True

    async def reap(self) -> int:
        async with self._sm() as s:
            n = await jobs.reap(s, self._lock_timeout_s)
        if n:
            log.warning("jobs_reaped", count=n)
        return n

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("worker_started", worker_id=self.id)
        last_reap = 0.0
        while not stop.is_set():
            processed = False
            try:
                if time.monotonic() - last_reap >= self._reap_every_s:
                    await self.reap()
                    last_reap = time.monotonic()
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # e.g. DB unreachable: log, back off, keep going
                log.exception("worker_loop_error")
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._poll_s)
        log.info("worker_stopped", worker_id=self.id)


async def _main() -> None:
    from app.adapters.db.engine import create_engine, create_sessionmaker
    from app.config import get_settings
    from app.observability.logging import configure_logging
    from app.services.handlers import HANDLERS

    settings = get_settings()
    configure_logging(settings.log_level)
    engine = create_engine(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        create_sessionmaker(engine),
        HANDLERS,
        poll_s=settings.worker_poll_s,
        lock_timeout_s=settings.job_lock_timeout_s,
    )
    try:
        await worker.run_forever(stop)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
