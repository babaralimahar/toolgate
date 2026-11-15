"""Build the configured LLM client."""

from __future__ import annotations

from app.adapters.llm import FakeLLMClient, LLMClient, Prices, echo_responder
from app.adapters.llm_openai_compat import OpenAICompatClient
from app.config import Settings


def build_llm_client(settings: Settings, prices: Prices) -> LLMClient:
    if settings.llm_provider == "fake":
        return FakeLLMClient(responder=echo_responder)
    if settings.llm_api_key is None:  # also enforced by Settings validation
        raise ValueError("LLM_API_KEY is required")
    return OpenAICompatClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout_s=settings.llm_timeout_s,
        prices=prices,
    )
