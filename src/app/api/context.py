"""Request-scoped context shared by middleware, error handlers and logging."""

from __future__ import annotations

import contextvars

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
