"""Structured JSON logging with request context, trace ids and secret scrubbing."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog
from opentelemetry import trace

REDACTED = "[REDACTED]"

# Keys whose values are never logged, whatever they contain.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "x-api-key",
        "x_api_key",
        "llm_api_key",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
        "set-cookie",
    }
)

# Value patterns scrubbed inside any string.
_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),  # bearer tokens
    re.compile(r"\bsk[_-][A-Za-z0-9_-]{16,}"),  # API keys (ours and most providers')
    re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),  # IBAN-like account numbers
]

_MAX_DEPTH = 6


def scrub(value: Any, depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive keys and patterns redacted."""
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        for pattern in _PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, dict):
        return {
            k: REDACTED if str(k).lower() in SENSITIVE_KEYS else scrub(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(scrub(v, depth + 1) for v in value)
    return value


def scrub_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        else:
            event_dict[key] = scrub(event_dict[key])
    return event_dict


def add_trace_ids(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    """Configure structlog for JSON output. Safe to call more than once (tests do)."""
    numeric = logging.getLevelNamesMapping()[level.upper()]
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            add_trace_ids,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            scrub_processor,  # last before rendering, so it also sees formatted tracebacks
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=numeric, stream=stream or sys.stdout, format="%(message)s")
