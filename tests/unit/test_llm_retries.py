from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.llm import (
    FakeLLMClient,
    LLMBadRequest,
    LLMError,
    LLMTimeout,
    LLMTransient,
    PricingMissing,
    complete_with_retries,
    cost,
    echo_responder,
    fake_result,
    load_pricing,
)


async def test_retries_transient_then_succeeds() -> None:
    fake = FakeLLMClient([LLMTransient("429"), fake_result(text="ok")])
    res = await complete_with_retries(fake, 3, messages=[{"role": "user", "content": "hi"}])
    assert res.text == "ok" and len(fake.calls) == 2


async def test_does_not_retry_bad_request() -> None:
    fake = FakeLLMClient([LLMBadRequest("400"), fake_result(text="never")])
    with pytest.raises(LLMBadRequest):
        await complete_with_retries(fake, 3, messages=[])
    assert len(fake.calls) == 1


async def test_timeout_is_retried_then_raised_after_max_attempts() -> None:
    fake = FakeLLMClient([LLMTimeout("t1"), LLMTimeout("t2"), LLMTimeout("t3"), fake_result()])
    with pytest.raises(LLMTimeout):
        await complete_with_retries(fake, 3, messages=[])
    assert len(fake.calls) == 3  # bounded: never a 4th attempt


async def test_non_llm_errors_are_not_retried() -> None:
    fake = FakeLLMClient([ValueError("bug"), fake_result()])
    with pytest.raises(ValueError):
        await complete_with_retries(fake, 3, messages=[])
    assert len(fake.calls) == 1


async def test_single_attempt_means_no_retry() -> None:
    fake = FakeLLMClient([LLMTransient("503"), fake_result()])
    with pytest.raises(LLMTransient):
        await complete_with_retries(fake, 1, messages=[])
    assert len(fake.calls) == 1


async def test_kwargs_reach_the_client() -> None:
    fake = FakeLLMClient([fake_result(parsed={"a": 1})])
    schema = {"type": "object"}
    res = await complete_with_retries(fake, 2, prompt_version="v9", messages=[], schema=schema)
    assert res.parsed == {"a": 1}
    assert fake.calls[0]["schema"] == schema


async def test_exhausted_script_raises() -> None:
    with pytest.raises(LLMError):
        await FakeLLMClient().complete([])


async def test_echo_responder_is_free() -> None:
    fake = FakeLLMClient(responder=echo_responder)
    res = await fake.complete([{"role": "user", "content": "ping"}])
    assert res.text == "ping" and res.usage.cost_usd == 0


def test_cost_calculation() -> None:
    prices = {"m": (Decimal("0.15"), Decimal("0.60"))}  # USD per 1M tokens
    assert cost("m", 1_000_000, 0, prices) == Decimal("0.15")
    assert cost("m", 2000, 500, prices) == Decimal("0.0006")


def test_cost_unknown_model_raises() -> None:
    with pytest.raises(PricingMissing):
        cost("nope", 1, 1, {})


def test_load_pricing(tmp_path: Path) -> None:
    p = tmp_path / "pricing.yaml"
    p.write_text("models:\n  m:\n    input: 0.15\n    output: 0.6\n", encoding="utf-8")
    assert load_pricing(p) == {"m": (Decimal("0.15"), Decimal("0.6"))}


def test_repo_pricing_file_loads() -> None:
    assert "fake" in load_pricing(Path("config/pricing.yaml"))
