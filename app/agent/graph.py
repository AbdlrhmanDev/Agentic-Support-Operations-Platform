"""The support workflow as an explicit state graph.

    agent ──> gate ──> execute ──┐
      ▲         │  └─> approval ─┤   (pauses for a reviewer)
      └─────────┴────────────────┘
    agent ──> finalize            (no tool calls: the reply is ready)
    agent / execute ──> escalate ──> finalize   (limits hit: hand to a human)

The model proposes tool calls. `gate` validates each one and asks the business
rules whether it may run. Only `execute` runs tools, and only after `gate`.
"""

import json
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from app.agent.llm.base import LLMClient, LLMError, LLMRefusal, Message
from app.agent.prompts import Prompt
from app.agent.state import RunState, scope_of
from app.approvals.service import ApprovalService
from app.config import Settings
from app.db.models import ApprovalStatus, RunStatus, TicketStatus
from app.db.session import SessionFactory
from app.observability.cost import cost_usd
from app.observability.recorder import EventKind, TraceRecorder
from app.observability.tracing import span
from app.services import tickets
from app.services.decisions import Verdict
from app.services.errors import ApprovalStateError
from app.tools.base import ToolKind
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry

ESCALATE_TOOL = "escalate_to_human"
FORCED_ESCALATION_REPLY = (
    "Thank you for your patience. I was not able to complete this request automatically, "
    "so I have passed your ticket to a member of our support team, who will follow up with you."
)
EMPTY_REPLY = (
    "Thank you for contacting us. Your request has been recorded and our support team "
    "will follow up with you."
)

Update = dict[str, Any]


class Node(Protocol):
    def __call__(self, state: RunState) -> Awaitable[Update]: ...


@dataclass(frozen=True)
class AgentDeps:
    settings: Settings
    llm: LLMClient
    prompt: Prompt
    registry: ToolRegistry
    executor: ToolExecutor
    approvals: ApprovalService
    recorder: TraceRecorder
    session_factory: SessionFactory


def _traced(name: str, node: Node) -> Node:
    """Wrap a node in a span and note it on the workflow path."""

    async def wrapper(state: RunState) -> Update:
        with span(f"node.{name}", **{"run.id": state["run_id"]}) as current:
            try:
                update = await node(state)
            except GraphBubbleUp:
                # A pause for approval, not a failure.
                current.set_attribute("run.paused", True)
                raise
        return {**update, "workflow_path": [name]}

    return wrapper


def _tool_message(call: dict[str, Any], payload: dict[str, Any], *, is_error: bool) -> Message:
    return {
        "role": "tool",
        "tool_call_id": call["id"],
        "name": call["name"],
        # Sorted keys keep the prompt prefix byte-stable for caching.
        "content": json.dumps(payload, sort_keys=True),
        "is_error": is_error,
    }


def _settle(state: RunState, payload: dict[str, Any], *, ok: bool) -> Update:
    """Record the result of the call at the head of the queue and move past it."""
    call, *rest = state["pending_calls"]
    return {
        "messages": [_tool_message(call, payload, is_error=not ok)],
        "tool_results": [
            {"tool": call["name"], "arguments": call["arguments"], "ok": ok, "result": payload}
        ],
        "pending_calls": rest,
        "gate": None,
    }


def _force_escalation(reason: str) -> dict[str, Any]:
    return {"action": "force_escalate", "reason": reason}


def derive_outcome(state: RunState) -> tuple[RunStatus, str]:
    """Name what the run achieved, from the writes that actually happened."""
    done = {action["tool"] for action in state.get("actions", [])}
    if ESCALATE_TOOL in done:
        return RunStatus.ESCALATED, "escalated"
    for tool, outcome in (
        ("issue_refund", "refund_issued"),
        ("create_shipping_replacement", "replacement_created"),
        ("update_customer_email", "account_updated"),
    ):
        if tool in done:
            return RunStatus.COMPLETED, outcome
    if any(a["status"] == ApprovalStatus.REJECTED for a in state.get("approvals", [])):
        return RunStatus.COMPLETED, "approval_rejected"
    return RunStatus.COMPLETED, "no_action"


def build_graph(
    deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any]
) -> CompiledStateGraph[RunState, None, RunState, RunState]:
    settings = deps.settings

    async def agent(state: RunState) -> Update:
        """Ask the model for its next move: tool calls, or the reply to the customer."""
        if state["step"] >= settings.max_agent_steps:
            return {"gate": _force_escalation("The agent reached its step limit.")}

        started = time.perf_counter()
        try:
            response = await deps.llm.complete(
                system=deps.prompt.text,
                messages=state["messages"],
                tools=deps.registry.llm_schemas(),
            )
        except LLMError as exc:
            reason = "refusal" if isinstance(exc, LLMRefusal) else "unavailable"
            await deps.recorder.record(
                state["run_id"],
                EventKind.LLM_CALL,
                deps.llm.model,
                error=str(exc),
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
            return {"gate": _force_escalation(f"The model was not usable ({reason}).")}

        await deps.recorder.record(
            state["run_id"],
            EventKind.LLM_CALL,
            response.model or deps.llm.model,
            output={
                "text": response.text,
                "tool_calls": [
                    {"name": call.name, "arguments": call.arguments} for call in response.tool_calls
                ],
            },
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=response.usage,
            cost=cost_usd(response.model or deps.llm.model, response.usage),
        )
        message = response.to_message()
        return {
            "messages": [message],
            "pending_calls": message["tool_calls"],
            "gate": None,
            "step": state["step"] + 1,
            "final_response": None if response.tool_calls else response.text,
            "usage": {
                "llm_calls": 1,
                "input_tokens": response.usage.total_input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }

    async def gate(state: RunState) -> Update:
        """Validate the next tool call and decide: run it, ask a reviewer, or refuse."""
        call = state["pending_calls"][0]
        scope = scope_of(state)

        async def refuse(code: str, message: str) -> Update:
            await deps.recorder.record(
                scope.run_id,
                EventKind.GUARD,
                call["name"],
                input=call["arguments"],
                output={"verdict": "refused", "code": code, "message": message},
            )
            return _settle(state, {"error": code, "message": message}, ok=False)

        spec = deps.registry.get(call["name"])
        if spec is None:
            return await refuse("unknown_tool", f"There is no tool named {call['name']}.")
        try:
            args = spec.args_model.model_validate(call["arguments"])
        except ValidationError as exc:
            return await refuse("invalid_arguments", _validation_summary(exc))

        review_requested = False
        requested: dict[str, Any] = {}
        if spec.resolve is not None:
            # A request for review: gate the action it is about, not the request.
            target_name, raw = spec.resolve(args)
            target = deps.registry.get(target_name)
            if target is None:
                return await refuse("unknown_tool", f"There is no tool named {target_name}.")
            try:
                requested = args.model_dump(mode="json")
                args = target.args_model.model_validate(raw)
            except ValidationError as exc:
                return await refuse("invalid_arguments", _validation_summary(exc))
            spec, review_requested = target, True

        plan: dict[str, Any] = {
            "call_id": call["id"],
            "tool": spec.name,
            "args": args.model_dump(mode="json"),
            "approval_id": None,
        }
        if spec.kind is ToolKind.READ:
            return {"gate": {**plan, "action": "execute"}}

        decision = await deps.executor.decide(spec, scope, args, review_requested=review_requested)
        if decision.verdict is Verdict.DENY:
            return await refuse("action_denied", decision.reason)

        await deps.recorder.record(
            scope.run_id,
            EventKind.GUARD,
            spec.name,
            input=plan["args"],
            output={"verdict": decision.verdict.value, "reason": decision.reason},
        )
        if decision.verdict is Verdict.ALLOW:
            return {"gate": {**plan, "action": "execute"}}
        return {
            "gate": {
                **plan,
                "action": "approval",
                "reason": decision.reason,
                "context": {
                    **decision.context,
                    "justification": requested.get("justification"),
                },
                "cited_policy": requested.get("policy_citation"),
            }
        }

    async def approval(state: RunState) -> Update:
        """Raise an approval request and pause until a reviewer decides."""
        plan = state["gate"]
        assert plan is not None
        scope = scope_of(state)
        record = await deps.approvals.request(
            scope,
            tool_call_id=plan["call_id"],
            action=plan["tool"],
            payload=plan["args"],
            reason=plan["reason"],
            context=plan["context"],
            policy_citation=_citation(state, plan.get("cited_policy")),
        )
        if record.status == ApprovalStatus.PENDING:
            # Stops the run here. On resume this node starts again from the top,
            # finds the same approval already decided, and carries on.
            interrupt({"approval_id": record.id})
            record = await deps.approvals.get(record.id)
            if record.status == ApprovalStatus.PENDING:
                raise ApprovalStateError(f"Run resumed while approval {record.id} is pending.")

        entry = {"approval_id": record.id, "action": record.action, "status": record.status}
        if record.status == ApprovalStatus.APPROVED:
            return {
                "gate": {**plan, "action": "execute", "approval_id": record.id},
                "approvals": [entry],
            }
        payload = {
            "error": "approval_rejected",
            "message": "A reviewer rejected this action. It was not carried out.",
            "reviewer_note": record.decision_note,
        }
        return {**_settle(state, payload, ok=False), "approvals": [entry]}

    async def execute(state: RunState) -> Update:
        """Run the tool call the gate cleared."""
        plan = state["gate"]
        assert plan is not None
        spec = deps.registry.get(plan["tool"])
        assert spec is not None
        args = spec.args_model.model_validate(plan["args"])

        faults = dict(state.get("faults", {}))
        budget = faults.get(spec.name, 0)
        outcome = await deps.executor.execute(
            spec,
            scope_of(state),
            args,
            approval_id=plan["approval_id"],
            injected_failures=budget,
        )
        if budget:
            faults[spec.name] = budget - min(budget, outcome.attempts)

        update = _settle(state, outcome.payload, ok=outcome.ok)
        update["faults"] = faults
        if outcome.ok:
            update["consecutive_failures"] = 0
            if spec.name == "search_policy":
                update["policy_evidence"] = outcome.payload["results"]
            if spec.kind is ToolKind.WRITE:
                update["actions"] = [
                    {
                        "tool": spec.name,
                        "arguments": plan["args"],
                        "result": outcome.payload,
                        "approval_id": plan["approval_id"],
                    }
                ]
        elif outcome.unavailable:
            failures = state["consecutive_failures"] + 1
            update["consecutive_failures"] = failures
            if failures >= settings.max_consecutive_tool_failures:
                update["gate"] = _force_escalation(f"{spec.name} kept failing.")
        return update

    async def escalate(state: RunState) -> Update:
        """Hand the ticket to a human without asking the model. The fail-closed path."""
        plan = state["gate"]
        assert plan is not None
        scope = scope_of(state)
        reason = plan["reason"]
        async with deps.session_factory.begin() as session:
            await tickets.update_ticket(
                session,
                ticket_id=scope.ticket_id,
                customer_id=scope.customer_id,
                status=TicketStatus.ESCALATED,
                note=f"Escalated automatically: {reason}",
            )
        await deps.recorder.record(
            scope.run_id, EventKind.ESCALATION, "forced_escalation", output={"reason": reason}
        )
        return {
            "actions": [{"tool": ESCALATE_TOOL, "forced": True, "arguments": {"reason": reason}}],
            "final_response": FORCED_ESCALATION_REPLY,
            "pending_calls": [],
            "gate": None,
        }

    async def finalize(state: RunState) -> Update:
        """Send the reply to the ticket and name the outcome."""
        scope = scope_of(state)
        reply = state.get("final_response") or EMPTY_REPLY
        async with deps.session_factory.begin() as session:
            await tickets.append_message(
                session, scope.ticket_id, scope.customer_id, "agent", reply
            )
        status, outcome = derive_outcome(state)
        # Plain strings: the state is checkpointed as JSON-compatible values.
        return {"final_response": reply, "status": status.value, "outcome": outcome}

    def after_agent(state: RunState) -> Literal["gate", "escalate", "finalize"]:
        if state.get("gate"):
            return "escalate"
        return "gate" if state["pending_calls"] else "finalize"

    def next_call(state: RunState) -> Literal["gate", "agent"]:
        return "gate" if state["pending_calls"] else "agent"

    def after_gate(state: RunState) -> Literal["execute", "approval", "gate", "agent"]:
        plan = state.get("gate")
        if plan is None:
            return next_call(state)
        return "approval" if plan["action"] == "approval" else "execute"

    def after_approval(state: RunState) -> Literal["execute", "gate", "agent"]:
        return "execute" if state.get("gate") else next_call(state)

    def after_execute(state: RunState) -> Literal["escalate", "gate", "agent"]:
        return "escalate" if state.get("gate") else next_call(state)

    graph = StateGraph(RunState)
    for name, node in (
        ("agent", agent),
        ("gate", gate),
        ("approval", approval),
        ("execute", execute),
        ("escalate", escalate),
        ("finalize", finalize),
    ):
        graph.add_node(name, _traced(name, node))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent)
    graph.add_conditional_edges("gate", after_gate)
    graph.add_conditional_edges("approval", after_approval)
    graph.add_conditional_edges("execute", after_execute)
    graph.add_edge("escalate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def _validation_summary(exc: ValidationError) -> str:
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    ]
    return "Invalid arguments. " + "; ".join(problems)


def _citation(state: RunState, cited: str | None) -> dict[str, Any] | None:
    """The policy section to show the reviewer: the one the agent named, else its best hit."""
    evidence = state.get("policy_evidence", [])
    if cited:
        for hit in evidence:
            if cited in (hit["chunk_id"], hit["policy_id"]):
                return _citation_view(hit)
        return {"cited": cited, "verified": False}
    if not evidence:
        return None
    return _citation_view(max(evidence, key=lambda hit: hit["score"]))


def _citation_view(hit: dict[str, Any]) -> dict[str, Any]:
    return {
        "chunk_id": hit["chunk_id"],
        "title": hit["title"],
        "section": hit["section"],
        "excerpt": hit["content"],
        "verified": True,
    }
