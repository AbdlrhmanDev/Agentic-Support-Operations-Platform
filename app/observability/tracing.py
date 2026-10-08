"""OpenTelemetry spans: one per graph node, model call and tool call."""

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import Span, Status, StatusCode

from app.config import Settings

_tracer = trace.get_tracer("agentic-support-ops")


def configure_tracing(settings: Settings) -> None:
    """Install the tracer provider. Without an exporter, spans are created and dropped."""
    provider = TracerProvider(resource=Resource.create({"service.name": "agentic-support-ops"}))
    if settings.otel_exporter == "console":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


@contextmanager
def span(name: str, **attributes: str | int | float | bool) -> Iterator[Span]:
    with _tracer.start_as_current_span(name, attributes=attributes) as current:
        try:
            yield current
        except Exception as exc:
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
