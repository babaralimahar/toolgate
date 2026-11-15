"""Async eval runner with a concurrency limit, deterministic sampling and a cost cap."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.evals.dataset import Dataset, EvalItem
from app.evals.metrics import REGISTRY


class Suite(Protocol):
    name: str
    metrics: tuple[str, ...]
    prompt_version: str

    async def run_item(self, item: EvalItem) -> tuple[Any, Decimal]:
        """Return (output, cost_usd) for one item."""
        ...


@dataclass(frozen=True)
class ItemOutcome:
    id: str
    output: Any
    expected: Any
    scores: dict[str, float]
    cost_usd: Decimal
    latency_ms: int
    error: str | None = None


@dataclass
class Report:
    suite: str
    split: str
    dataset_sha256: str
    prompt_version: str
    started_at: str
    n_planned: int
    config: dict[str, Any]
    outcomes: list[ItemOutcome] = field(default_factory=list)
    total_cost_usd: Decimal = Decimal(0)
    stopped_reason: str | None = None

    @property
    def n_completed(self) -> int:
        return len(self.outcomes)

    @property
    def n_errors(self) -> int:
        return sum(1 for o in self.outcomes if o.error is not None)

    def summary(self, metric_names: tuple[str, ...]) -> dict[str, float]:
        """Mean per metric over completed items. Errored items count as 0, not as skipped."""
        if not self.outcomes:
            return {m: 0.0 for m in metric_names}
        return {
            m: sum(o.scores.get(m, 0.0) for o in self.outcomes) / len(self.outcomes)
            for m in metric_names
        }


def select_items(items: list[EvalItem], sample: int | None, seed: int) -> list[EvalItem]:
    """Deterministic sample that keeps dataset order (same seed -> same items)."""
    if sample is None or sample >= len(items):
        return list(items)
    chosen = set(random.Random(seed).sample(range(len(items)), sample))  # noqa: S311
    return [it for i, it in enumerate(items) if i in chosen]


async def run_suite(
    suite: Suite,
    dataset: Dataset,
    *,
    concurrency: int = 4,
    max_cost: Decimal | None = None,
    sample: int | None = None,
    seed: int = 0,
) -> Report:
    """Run every selected item. With ``max_cost``, no new item starts once spend reaches the
    cap; items already in flight finish, so overshoot is at most ``concurrency`` items."""
    for m in suite.metrics:
        if m not in REGISTRY:
            raise ValueError(f"unknown metric {m!r}")
    items = select_items(dataset.items, sample, seed)
    report = Report(
        suite=suite.name,
        split=dataset.split,
        dataset_sha256=dataset.sha256,
        prompt_version=suite.prompt_version,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        n_planned=len(items),
        config={
            "concurrency": concurrency,
            "max_cost": str(max_cost) if max_cost is not None else None,
            "sample": sample,
            "seed": seed,
        },
    )
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one(item: EvalItem) -> ItemOutcome | None:
        async with sem:
            async with lock:
                if max_cost is not None and report.total_cost_usd >= max_cost:
                    report.stopped_reason = "max_cost"
                    return None
            started = time.perf_counter()
            error: str | None = None
            try:
                output, spent = await suite.run_item(item)
            except Exception as e:  # one bad item must not kill the run
                output, spent, error = None, Decimal(0), f"{type(e).__name__}: {e}"[:500]
            latency_ms = int((time.perf_counter() - started) * 1000)
            async with lock:
                report.total_cost_usd += spent
            scores = (
                {m: 0.0 for m in suite.metrics}
                if error
                else {m: REGISTRY[m](item.expected, output) for m in suite.metrics}
            )
            return ItemOutcome(item.id, output, item.expected, scores, spent, latency_ms, error)

    results = await asyncio.gather(*(one(it) for it in items))
    report.outcomes = [r for r in results if r is not None]
    return report
