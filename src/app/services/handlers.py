"""Job handlers, keyed by job kind. Projects register their own (e.g. P1 'extract_invoice')."""

from __future__ import annotations

import structlog

from app.adapters.db.jobs import ClaimedJob
from app.worker import Handler

log = structlog.get_logger()


async def echo(job: ClaimedJob) -> None:
    """Template demo handler. Side-effect free, so at-least-once delivery is harmless."""
    if job.payload.get("fail"):
        raise RuntimeError("requested failure")
    log.info("echo_job", payload=job.payload)


HANDLERS: dict[str, Handler] = {"echo": echo}
