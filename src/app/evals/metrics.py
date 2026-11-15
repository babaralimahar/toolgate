"""Metric registry. A metric maps (expected, output) to a score in [0, 1]."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MetricFn = Callable[[Any, Any], float]
REGISTRY: dict[str, MetricFn] = {}


def metric(name: str) -> Callable[[MetricFn], MetricFn]:
    def register(fn: MetricFn) -> MetricFn:
        if name in REGISTRY:
            raise ValueError(f"metric {name!r} already registered")
        REGISTRY[name] = fn
        return fn

    return register


def _norm(x: Any) -> str:
    return " ".join(str(x).split()).casefold()


@metric("exact_match")
def exact_match(expected: Any, output: Any) -> float:
    return float(output is not None and str(output).strip() == str(expected).strip())


@metric("normalized_match")
def normalized_match(expected: Any, output: Any) -> float:
    """Case- and whitespace-insensitive."""
    return float(output is not None and _norm(output) == _norm(expected))


@metric("contains")
def contains(expected: Any, output: Any) -> float:
    return float(output is not None and _norm(expected) in _norm(output))
