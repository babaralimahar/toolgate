"""OpenTelemetry setup: OTLP/HTTP export (e.g. to Jaeger) plus auto-instrumentation."""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.config import Settings

_provider: TracerProvider | None = None


def setup_tracing(app: FastAPI, settings: Settings, engine: AsyncEngine) -> TracerProvider | None:
    """Export traces only when OTEL_ENDPOINT is set; otherwise spans are no-ops."""
    global _provider
    if not settings.otel_endpoint:
        return None
    if _provider is None:  # the global provider can only be set once per process
        _provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.app_name,
                    "service.version": __version__,
                    "deployment.environment": settings.env,
                }
            )
        )
        _provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
        )
        trace.set_tracer_provider(_provider)
        HTTPXClientInstrumentor().instrument(tracer_provider=_provider)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_provider, excluded_urls="healthz,readyz,metrics"
    )
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=_provider)
    return _provider


def shutdown_tracing() -> None:
    if _provider is not None:
        _provider.force_flush(timeout_millis=5000)
