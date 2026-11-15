"""Prometheus metrics shared by the whole service."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

LLM_CALLS = Counter("llm_calls_total", "LLM calls by outcome", ["model", "status"])
LLM_TOKENS = Counter("llm_tokens_total", "LLM tokens", ["model", "direction"])
LLM_COST = Counter("llm_cost_usd_total", "LLM spend in USD (from the pricing table)", ["model"])
LLM_LATENCY = Histogram(
    "llm_call_duration_seconds",
    "LLM call latency including retries",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64),
)

JOBS = Counter("jobs_total", "Background jobs by outcome", ["kind", "status"])
