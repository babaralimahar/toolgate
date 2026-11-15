"""LLMClient for any provider exposing an OpenAI-compatible /chat/completions endpoint.

Many providers do (OpenAI, Groq, DeepSeek, OpenRouter, Together, Gemini's compatibility
endpoint, a local Ollama). Pick yours on Day 2 and set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY.
If your provider needs its own SDK, write another class with the same ``complete`` signature.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.adapters.llm import (
    LLMBadRequest,
    LLMError,
    LLMResult,
    LLMTimeout,
    LLMTransient,
    Message,
    Prices,
    Usage,
    cost,
)


class OpenAICompatClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float,
        prices: Prices,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        cost(model, 0, 0, prices)  # fail fast if the model has no price entry
        self.model = model
        self._prices = prices
        self._timeout = httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))
        self._http = http or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResult:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema},
            }
        if tools:
            body["tools"] = tools

        started = time.perf_counter()
        try:
            resp = await self._http.post(
                "/chat/completions", json=body, headers=self._headers, timeout=self._timeout
            )
        except httpx.TimeoutException as e:
            raise LLMTimeout(f"timeout after {self._timeout.read}s") from e
        except httpx.TransportError as e:
            raise LLMTransient(f"network error: {type(e).__name__}") from e
        latency_ms = int((time.perf_counter() - started) * 1000)

        if resp.status_code == 429 or resp.status_code >= 500:
            raise LLMTransient(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMBadRequest(f"HTTP {resp.status_code}: {resp.text[:500]}")

        try:
            data = resp.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as e:
            raise LLMError("unexpected response shape from provider") from e

        text: str | None = message.get("content")
        parsed: dict[str, Any] | None = None
        if schema is not None and text:
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                candidate = None  # the caller gets one repair attempt with the raw text
            parsed = candidate if isinstance(candidate, dict) else None

        usage = data.get("usage") or {}
        tin = int(usage.get("prompt_tokens", 0))
        tout = int(usage.get("completion_tokens", 0))
        return LLMResult(
            text=text,
            parsed=parsed,
            tool_calls=list(message.get("tool_calls") or []),
            usage=Usage(tin, tout, cost(self.model, tin, tout, self._prices)),
            model=str(data.get("model") or self.model),
            latency_ms=latency_ms,
        )
