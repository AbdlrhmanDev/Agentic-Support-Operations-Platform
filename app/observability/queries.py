"""Reads a run's trace back out, in full or as totals."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.db.models import TraceEvent
from app.db.session import SessionFactory
from app.observability.recorder import EventKind


@dataclass(frozen=True)
class TraceSummary:
    llm_calls: int
    tool_calls: int
    tool_retries: int
    errors: int
    input_tokens: int
    output_tokens: int
    # None when any model call had no known price.
    cost_usd: Decimal | None
    latency_ms: int


async def load_trace(session_factory: SessionFactory, run_id: str) -> list[TraceEvent]:
    async with session_factory() as session:
        result = await session.scalars(
            select(TraceEvent).where(TraceEvent.run_id == run_id).order_by(TraceEvent.id)
        )
        return list(result.all())


def summarize(events: list[TraceEvent]) -> TraceSummary:
    llm = [event for event in events if event.kind == EventKind.LLM_CALL]
    tools = [event for event in events if event.kind == EventKind.TOOL_CALL]
    priced = [event.cost_usd for event in llm if event.error is None]
    return TraceSummary(
        llm_calls=len(llm),
        tool_calls=len(tools),
        tool_retries=sum(event.attempts - 1 for event in tools),
        errors=sum(1 for event in events if event.error is not None),
        input_tokens=sum(event.input_tokens or 0 for event in llm),
        output_tokens=sum(event.output_tokens or 0 for event in llm),
        cost_usd=(
            None
            if any(cost is None for cost in priced)
            else sum((cost for cost in priced if cost is not None), Decimal(0))
        ),
        latency_ms=sum(event.latency_ms or 0 for event in llm + tools),
    )
