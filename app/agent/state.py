"""The state carried through a run. Plain JSON-compatible values only, so it checkpoints cleanly."""

import operator
from typing import Annotated, Any, TypedDict

from app.agent.llm.base import Message
from app.db.models import RunStatus
from app.tools.base import RunScope


def _add_counts(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {key: left.get(key, 0) + right.get(key, 0) for key in left.keys() | right.keys()}


class RunState(TypedDict, total=False):
    run_id: str
    ticket_id: str
    customer_id: str

    messages: Annotated[list[Message], operator.add]
    # Tool calls from the latest model turn that still need handling, in order.
    pending_calls: list[dict[str, Any]]
    # What the gate decided for the call at the head of `pending_calls`.
    gate: dict[str, Any] | None

    policy_evidence: Annotated[list[dict[str, Any]], operator.add]
    tool_results: Annotated[list[dict[str, Any]], operator.add]
    # Writes that actually happened.
    actions: Annotated[list[dict[str, Any]], operator.add]
    approvals: Annotated[list[dict[str, Any]], operator.add]
    workflow_path: Annotated[list[str], operator.add]
    usage: Annotated[dict[str, int], _add_counts]

    step: int
    consecutive_failures: int
    # Remaining injected failures per tool. Empty outside tests and evals.
    faults: dict[str, int]

    final_response: str | None
    status: str
    outcome: str | None


def initial_state(
    scope: RunScope, customer_message: str, faults: dict[str, int] | None = None
) -> RunState:
    # The message is data. Stop it closing its own wrapper to pose as something else.
    safe = customer_message.replace("</customer_message>", "").replace("</ticket>", "")
    content = (
        f'<ticket id="{scope.ticket_id}">\n'
        f"<customer_message>\n{safe}\n</customer_message>\n"
        "</ticket>"
    )
    return RunState(
        run_id=scope.run_id,
        ticket_id=scope.ticket_id,
        customer_id=scope.customer_id,
        messages=[{"role": "user", "content": content}],
        pending_calls=[],
        gate=None,
        policy_evidence=[],
        tool_results=[],
        actions=[],
        approvals=[],
        workflow_path=[],
        usage={},
        step=0,
        consecutive_failures=0,
        faults=dict(faults or {}),
        final_response=None,
        status=RunStatus.RUNNING.value,
        outcome=None,
    )


def scope_of(state: RunState) -> RunScope:
    return RunScope(state["run_id"], state["customer_id"], state["ticket_id"])
