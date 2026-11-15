"""Provider-agnostic LLM client protocol with bounded retries, cost accounting and tracing.

Real providers implement ``LLMClient`` and map their SDK/HTTP errors onto the error types
below. Only ``LLMTransient`` (429, 5xx, network, timeout) is retried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import yaml
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.observability.metrics import LLM_CALLS, LLM_COST, LLM_LATENCY, LLM_TOKENS

Message = dict[str, Any]
Prices = dict[str, tuple[Decimal, Decimal]]  # model -> (USD per 1M input, USD per 1M output)


class LLMError(Exception): ...


class LLMTransient(LLMError): ...  # 429, 5xx, network


class LLMTimeout(LLMTransient): ...


class LLMBadRequest(LLMError): ...  # 4xx other than 429 -> never retried


class PricingMissing(LLMError): ...


@dataclass(frozen=True)
class Usage:
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


@dataclass(frozen=True)
class LLMResult:
    text: str | None
    parsed: dict[str, Any] | None  # structured output, if a schema was given
    tool_calls: list[dict[str, Any]]
    usage: Usage
    model: str
    latency_ms: int


class LLMClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult: ...


# ------------------------------------------------------------------ pricing


def load_pricing(path: Path) -> Prices:
    """Read config/pricing.yaml. Prices are copied by hand from the provider's page, with a date."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models: dict[str, dict[str, Any]] = data.get("models") or {}
    return {
        name: (Decimal(str(p["input"])), Decimal(str(p["output"]))) for name, p in models.items()
    }


def cost(model: str, tin: int, tout: int, prices: Prices) -> Decimal:
    try:
        p_in, p_out = prices[model]
    except KeyError as e:
        raise PricingMissing(f"no price for model {model!r} in the pricing table") from e
    return (Decimal(tin) * p_in + Decimal(tout) * p_out) / Decimal(1_000_000)


# ------------------------------------------------------------------ retries + tracing

# Module-level so tests can monkeypatch it to tenacity.wait_none().
_WAIT = wait_exponential_jitter(multiplier=1, max=20)
_tracer = trace.get_tracer(__name__)


async def complete_with_retries(
    client: LLMClient,
    max_attempts: int,
    *,
    prompt_version: str = "unversioned",
    **kw: Any,
) -> LLMResult:
    """Call ``client.complete(**kw)``, retrying transient errors with exponential jitter.

    Emits one ``llm.call`` span covering all attempts, plus Prometheus metrics.
    """
    model_label = str(getattr(client, "model", "unknown"))
    attempts = 0
    started = time.perf_counter()
    with _tracer.start_as_current_span("llm.call") as span:
        span.set_attribute("llm.prompt_version", prompt_version)
        span.set_attribute("llm.request.model", model_label)
        result: LLMResult | None = None
        try:
            async for attempt in AsyncRetrying(
                retry=retry_if_exception_type(LLMTransient),
                stop=stop_after_attempt(max_attempts),
                wait=_WAIT,
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    result = await client.complete(**kw)
        except LLMError as e:
            span.set_attribute("llm.attempts", attempts)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, type(e).__name__))
            LLM_CALLS.labels(model=model_label, status=type(e).__name__).inc()
            raise
        if result is None:  # unreachable: tenacity either returns or raises
            raise LLMError("retry loop ended without a result")

        elapsed = time.perf_counter() - started
        span.set_attribute("llm.model", result.model)
        span.set_attribute("llm.attempts", attempts)
        span.set_attribute("llm.tokens_in", result.usage.tokens_in)
        span.set_attribute("llm.tokens_out", result.usage.tokens_out)
        span.set_attribute("llm.cost_usd", float(result.usage.cost_usd))
        span.set_attribute("llm.latency_ms", int(elapsed * 1000))
        LLM_CALLS.labels(model=result.model, status="ok").inc()
        LLM_TOKENS.labels(model=result.model, direction="in").inc(result.usage.tokens_in)
        LLM_TOKENS.labels(model=result.model, direction="out").inc(result.usage.tokens_out)
        LLM_COST.labels(model=result.model).inc(float(result.usage.cost_usd))
        LLM_LATENCY.labels(model=result.model).observe(elapsed)
        return result


# ------------------------------------------------------------------ fake client (tests, dev)

Responder = Callable[[list[Message]], "LLMResult"]


@dataclass
class FakeLLMClient:
    """Replays scripted outputs: each item is an LLMResult or an Exception to raise.

    When the script is empty, ``responder`` (if given) produces the result.
    """

    script: list[LLMResult | Exception] = field(default_factory=list)
    responder: Responder | None = None
    model: str = "fake"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        self.calls.append({"messages": messages, "schema": schema, "tools": tools})
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if self.responder is not None:
            return self.responder(messages)
        raise LLMError("FakeLLMClient script exhausted")


def fake_result(
    parsed: dict[str, Any] | None = None,
    text: str | None = None,
    *,
    cost_usd: Decimal = Decimal("0.0001"),
    tool_calls: list[dict[str, Any]] | None = None,
) -> LLMResult:
    return LLMResult(
        text=text,
        parsed=parsed,
        tool_calls=tool_calls or [],
        usage=Usage(100, 50, cost_usd),
        model="fake",
        latency_ms=5,
    )


def echo_responder(messages: list[Message]) -> LLMResult:
    """Dev-mode fake: echoes the last user message. Lets the app run with zero spend."""
    last = next((m for m in reversed(messages) if m.get("role") == "user"), {"content": ""})
    return fake_result(text=str(last.get("content", "")), cost_usd=Decimal(0))
