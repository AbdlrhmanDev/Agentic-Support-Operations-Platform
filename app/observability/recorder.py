"""Persists the structured trace of a run: model calls, tool calls and guard decisions."""

import re
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.db.models import TraceEvent
from app.db.session import SessionFactory
from app.observability.cost import Usage

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


class EventKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    GUARD = "guard"
    APPROVAL = "approval"
    ESCALATION = "escalation"


def redact(value: Any) -> Any:
    """Mask email addresses anywhere in a JSON-like value before it is stored."""
    if isinstance(value, str):
        return _EMAIL.sub(r"\1***@\2", value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class TraceRecorder:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        run_id: str,
        kind: EventKind,
        name: str,
        *,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        attempts: int = 1,
        latency_ms: int | None = None,
        usage: Usage | None = None,
        cost: Decimal | None = None,
    ) -> None:
        # Its own transaction: a trace event must survive the failure it describes.
        async with self._session_factory.begin() as session:
            session.add(
                TraceEvent(
                    run_id=run_id,
                    kind=kind,
                    name=name,
                    input=redact(input),
                    output=redact(output),
                    error=error,
                    attempts=attempts,
                    latency_ms=latency_ms,
                    input_tokens=usage.total_input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    cost_usd=cost,
                )
            )
