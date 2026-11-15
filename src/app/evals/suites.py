"""Suite registry. Each project adds its own suites (e.g. P1 field-level extraction)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.adapters.llm import LLMClient, LLMResult, Message, complete_with_retries, fake_result
from app.evals.dataset import EvalItem


class ToyUppercaseSuite:
    """Toy task that exercises the harness end to end: uppercase the input text."""

    name = "toy"
    metrics = ("exact_match", "normalized_match")
    prompt_version = "toy-uppercase-v1"
    SYSTEM = "Rewrite the user's text in uppercase. Reply with the rewritten text only."

    def __init__(self, llm: LLMClient, max_attempts: int = 2) -> None:
        self._llm = llm
        self._max_attempts = max_attempts

    async def run_item(self, item: EvalItem) -> tuple[Any, Decimal]:
        res = await complete_with_retries(
            self._llm,
            self._max_attempts,
            prompt_version=self.prompt_version,
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": str(item.input)},
            ],
        )
        return (res.text or "").strip(), res.usage.cost_usd


def fake_uppercase_responder(cost_per_call: Decimal) -> Callable[[list[Message]], LLMResult]:
    """Deterministic stand-in for a model on the toy task (zero-spend runs and tests)."""

    def respond(messages: list[Message]) -> LLMResult:
        return fake_result(text=str(messages[-1]["content"]).upper(), cost_usd=cost_per_call)

    return respond


SuiteFactory = Callable[[LLMClient], Any]
SUITES: dict[str, SuiteFactory] = {"toy": ToyUppercaseSuite}
