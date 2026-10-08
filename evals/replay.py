"""Replays a recorded run against a sandbox copy of its customer, to compare then with now.

uv run python -m evals.replay RUN_ID              # ask the configured model again (costs money)
uv run python -m evals.replay RUN_ID --recorded   # replay the recorded model turns, no model calls

The original customer, orders and refunds are never touched. The replay runs
for a new customer holding copies of the orders, with dates moved forward so
each order is as old as it was when the original run started. Orders are
copied as they are now: a status that changed since the run is not undone.

With --recorded the model's recorded turns go through today's gate, business
rules and tools, which shows whether a code change alters the outcome.
Without it, today's model and prompt answer the same ticket.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.base import LLMResponse, ToolCall
from app.agent.llm.scripted import ScriptedLLM
from app.clock import utcnow
from app.config import get_settings
from app.container import Container, build_container
from app.db.models import (
    AgentRun,
    Approval,
    ApprovalStatus,
    Customer,
    Order,
    Refund,
    RunStatus,
    new_id,
)
from app.eventloop import run as run_async
from app.services.errors import InvalidInput
from evals.grading import proposed_calls

_CUSTOMER_TEXT = re.compile(r"<customer_message>\n(.*)\n</customer_message>", re.DOTALL)
# A replay that keeps asking for approvals is stopped after this many decisions.
_MAX_DECISIONS = 10


@dataclass(frozen=True)
class Sandbox:
    customer_email: str
    # Original order ids and email, mapped to their copies.
    renamed: dict[str, str]

    def remap(self, value: Any) -> Any:
        """Point every mention of an original id, anywhere in a JSON value, at its copy."""
        text = json.dumps(value)
        for original, copy in self.renamed.items():
            text = text.replace(original, copy)
        return json.loads(text)


@dataclass(frozen=True)
class ReplayReport:
    run_id: str
    replay_run_id: str
    mode: str
    original: dict[str, Any]
    replay: dict[str, Any]

    @property
    def same_outcome(self) -> bool:
        return all(self.original[key] == self.replay[key] for key in ("status", "outcome"))

    @property
    def same_tools(self) -> bool:
        return bool(self.original["tools"] == self.replay["tools"])

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "replay_run_id": self.replay_run_id,
            "mode": self.mode,
            "same_outcome": self.same_outcome,
            "same_tools": self.same_tools,
            "original": self.original,
            "replay": self.replay,
        }


def _side(run: AgentRun, sandbox: Sandbox | None = None) -> dict[str, Any]:
    calls = proposed_calls(run.state_json or {})
    if sandbox is not None:
        calls = sandbox.remap(calls)
    return {
        "status": str(run.status),
        "outcome": run.outcome,
        "tools": [call["name"] for call in calls],
        "calls": calls,
    }


async def _clone_customer(session: AsyncSession, run: AgentRun) -> Sandbox:
    original = await session.get(Customer, run.customer_id)
    assert original is not None
    tag = uuid4().hex[:10]
    copy = Customer(id=new_id("cus"), email=f"replay-{tag}@example.com", name=original.name)
    session.add(copy)
    await session.flush()

    # If the run changed the email, the recorded call names the address the
    # original customer now holds. Give the replay a free one to ask for.
    renamed = {original.email: f"replay-new-{tag}@example.com"}
    shift = utcnow() - run.started_at
    orders = await session.scalars(
        select(Order).where(Order.customer_id == run.customer_id, Order.placed_at <= run.started_at)
    )
    for order in orders.all():
        order_id = f"ORD-{uuid4().int % 10**8:08d}"
        renamed[order.id] = order_id
        session.add(
            Order(
                id=order_id,
                customer_id=copy.id,
                item_summary=order.item_summary,
                total=order.total,
                charged_amount=order.charged_amount,
                currency=order.currency,
                status=order.status,
                placed_at=order.placed_at + shift,
                shipped_at=order.shipped_at + shift if order.shipped_at else None,
                delivered_at=order.delivered_at + shift if order.delivered_at else None,
            )
        )
        await session.flush()
        earlier = await session.scalars(
            select(Refund).where(Refund.order_id == order.id, Refund.created_at < run.started_at)
        )
        for index, refund in enumerate(earlier.all()):
            session.add(
                Refund(
                    id=new_id("ref"),
                    order_id=order_id,
                    amount=refund.amount,
                    currency=refund.currency,
                    reason=refund.reason,
                    idempotency_key=f"replay-{order_id}-{index}",
                )
            )
    return Sandbox(copy.email, renamed)


def _recorded_turns(state: dict[str, Any], sandbox: Sandbox) -> list[LLMResponse]:
    return [
        LLMResponse(
            text=message.get("content") or "",
            tool_calls=[
                ToolCall(call["id"], call["name"], sandbox.remap(call["arguments"]))
                for call in message.get("tool_calls", [])
            ],
        )
        for message in state.get("messages", [])
        if message.get("role") == "assistant"
    ]


async def _decide_as_recorded(container: Container, run: AgentRun, recorded: list[str]) -> AgentRun:
    """Answer each approval as the original reviewer did. Anything beyond that is rejected."""
    answered = 0
    while run.status == RunStatus.AWAITING_APPROVAL and answered < _MAX_DECISIONS:
        pending = [
            approval
            for approval in await container.approvals.list_approvals(ApprovalStatus.PENDING, 1000)
            if approval.run_id == run.id
        ]
        if not pending:
            break
        for approval in sorted(pending, key=lambda item: item.created_at):
            decision = recorded[answered] if answered < len(recorded) else None
            await container.approvals.decide(
                approval.id,
                approve=decision == ApprovalStatus.APPROVED,
                reviewer="replay",
                note=f"Replay. Recorded decision: {decision or 'none'}.",
            )
            answered += 1
        run = await container.runs.resume(run.id)
    return run


async def replay_run(
    container: Container, run_id: str, script: ScriptedLLM | None = None
) -> ReplayReport:
    """Replay a run in a sandbox. Give the container's scripted model to replay recorded turns."""
    original = await container.runs.get(run_id)
    state = original.state_json or {}
    opening = next((m for m in state.get("messages", []) if m.get("role") == "user"), None)
    match = _CUSTOMER_TEXT.search(opening["content"]) if opening else None
    if match is None:
        raise InvalidInput(f"Run {run_id} has no recorded customer message to replay.")

    async with container.session_factory.begin() as session:
        sandbox = await _clone_customer(session, original)
        approvals = await session.scalars(
            select(Approval).where(Approval.run_id == run_id).order_by(Approval.created_at)
        )
        decisions = [str(approval.status) for approval in approvals.all()]

    if script is not None:
        script.extend(_recorded_turns(state, sandbox))
    run = await container.runs.start(
        customer_email=sandbox.customer_email, message=sandbox.remap(match.group(1))
    )
    run = await _decide_as_recorded(container, run, decisions)
    return ReplayReport(
        run_id=run_id,
        replay_run_id=run.id,
        mode="recorded" if script is not None else "live",
        original=_side(original, sandbox),
        replay=_side(run),
    )


async def main(args: argparse.Namespace) -> int:
    script = ScriptedLLM(steps=[]) if args.recorded else None
    container = await build_container(get_settings(), llm=script)
    try:
        report = await replay_run(container, args.run_id, script)
    finally:
        await container.aclose()
    print(json.dumps(report.to_json(), indent=2))
    return 0 if report.same_outcome else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay a recorded run in a sandbox.")
    parser.add_argument("run_id")
    parser.add_argument(
        "--recorded",
        action="store_true",
        help="Replay the recorded model turns instead of calling a model.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(run_async(main(parse_args())))
