from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx

from app.adapters.llm import (
    LLMBadRequest,
    LLMTimeout,
    LLMTransient,
    PricingMissing,
    complete_with_retries,
)
from app.adapters.llm_openai_compat import OpenAICompatClient

BASE = "https://llm.test/v1"
PRICES = {"m-1": (Decimal("1"), Decimal("2"))}


def ok_body(
    content: str | None, *, tool_calls: list[dict[str, object]] | None = None
) -> dict[str, object]:
    msg: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "model": "m-1",
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
    }


def client() -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url=BASE, api_key="sk-test", model="m-1", timeout_s=5, prices=PRICES
    )


@respx.mock
async def test_success_parses_text_usage_and_cost() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=ok_body("hello"))
    )
    res = await client().complete([{"role": "user", "content": "hi"}])
    assert res.text == "hello"
    assert (res.usage.tokens_in, res.usage.tokens_out) == (1000, 500)
    assert res.usage.cost_usd == Decimal("0.002")  # 1000*1/1M + 500*2/1M
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer sk-test"
    assert json.loads(sent.content)["model"] == "m-1"


@respx.mock
async def test_structured_output_is_parsed() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=ok_body('{"total": 12.5}'))
    )
    res = await client().complete([], schema={"type": "object"})
    assert res.parsed == {"total": 12.5}


@respx.mock
async def test_invalid_json_leaves_parsed_none_for_repair() -> None:
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=ok_body("not json"))
    )
    res = await client().complete([], schema={"type": "object"})
    assert res.parsed is None and res.text == "not json"


@respx.mock
async def test_tool_calls_returned() -> None:
    call: dict[str, object] = {
        "id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"},
    }  # fmt: skip
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=ok_body(None, tool_calls=[call]))
    )
    res = await client().complete([], tools=[{"type": "function", "function": {"name": "f"}}])
    assert res.tool_calls == [call]


@respx.mock
async def test_429_then_200_is_retried() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=ok_body("ok"))]
    )
    res = await complete_with_retries(client(), 3, messages=[])
    assert res.text == "ok" and route.call_count == 2


@respx.mock
async def test_5xx_maps_to_transient() -> None:
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(503))
    with pytest.raises(LLMTransient):
        await client().complete([])


@respx.mock
async def test_400_is_not_retried() -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": "bad"})
    )
    with pytest.raises(LLMBadRequest):
        await complete_with_retries(client(), 3, messages=[])
    assert route.call_count == 1


@respx.mock
async def test_timeout_maps_to_llm_timeout() -> None:
    respx.post(f"{BASE}/chat/completions").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(LLMTimeout):
        await client().complete([])


def test_unpriced_model_fails_fast() -> None:
    with pytest.raises(PricingMissing):
        OpenAICompatClient(base_url=BASE, api_key="k", model="other", timeout_s=5, prices=PRICES)
