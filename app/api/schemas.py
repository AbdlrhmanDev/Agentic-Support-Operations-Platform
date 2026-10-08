"""Request and response models for the HTTP API."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.approvals.priority import Priority
from app.db.models import AgentRun, Approval, TraceEvent
from app.observability.queries import TraceSummary


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_email: EmailStr
    message: str = Field(min_length=1, max_length=5000)
    ticket_id: str | None = Field(default=None, max_length=40)
    faults: dict[str, int] | None = Field(
        default=None,
        description="Testing only. Makes the named tools fail this many times.",
    )


class EmailIntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_email: EmailStr
    subject: str = Field(default="", max_length=300)
    body: str = Field(min_length=1, max_length=4500)
    message_id: str = Field(min_length=1, max_length=200, description="The email's Message-ID.")


class TraceSummaryView(BaseModel):
    llm_calls: int
    tool_calls: int
    tool_retries: int
    errors: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    latency_ms: int

    @classmethod
    def of(cls, summary: TraceSummary) -> "TraceSummaryView":
        return cls(**summary.__dict__)


class RunView(BaseModel):
    run_id: str
    ticket_id: str
    customer_id: str
    status: str
    outcome: str | None
    final_response: str | None
    error: str | None
    model: str
    prompt_version: str
    workflow_path: list[str]
    actions: list[dict[str, Any]]
    approvals: list[dict[str, Any]]
    policy_evidence: list[dict[str, Any]]
    quality: dict[str, Any] | None
    started_at: datetime
    completed_at: datetime | None
    trace: TraceSummaryView | None = None

    @classmethod
    def of(cls, run: AgentRun, summary: TraceSummary | None = None) -> "RunView":
        state = run.state_json or {}
        return cls(
            run_id=run.id,
            ticket_id=run.ticket_id,
            customer_id=run.customer_id,
            status=run.status,
            outcome=run.outcome,
            final_response=run.final_response,
            error=run.error,
            model=run.model,
            prompt_version=run.prompt_version,
            workflow_path=state.get("workflow_path", []),
            actions=state.get("actions", []),
            approvals=state.get("approvals", []),
            policy_evidence=[
                {key: hit[key] for key in ("chunk_id", "title", "section", "score")}
                for hit in state.get("policy_evidence", [])
            ],
            quality=run.quality,
            started_at=run.started_at,
            completed_at=run.completed_at,
            trace=TraceSummaryView.of(summary) if summary else None,
        )


class ApprovalView(BaseModel):
    approval_id: str
    run_id: str
    action: str
    payload: dict[str, Any]
    reason: str
    context: dict[str, Any]
    policy_citation: dict[str, Any] | None
    status: str
    # Set on the pending queue only, where it decides the order.
    priority: Priority | None
    reviewer: str | None
    decision_note: str | None
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, approval: Approval, priority: Priority | None = None) -> "ApprovalView":
        return cls(
            approval_id=approval.id,
            run_id=approval.run_id,
            action=approval.action,
            payload=approval.payload,
            reason=approval.reason,
            context=approval.context,
            policy_citation=approval.policy_citation,
            status=approval.status,
            priority=priority,
            reviewer=approval.reviewer,
            decision_note=approval.decision_note,
            created_at=approval.created_at,
            decided_at=approval.decided_at,
        )


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required unless reviewer keys are configured, when the key identifies the reviewer.
    reviewer: str | None = Field(default=None, min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)


class DecisionResponse(BaseModel):
    approval: ApprovalView
    run: RunView


class TraceEventView(BaseModel):
    seq: int
    kind: str
    name: str
    input: dict[str, Any] | None
    output: dict[str, Any] | None
    error: str | None
    attempts: int
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    at: datetime

    @classmethod
    def of(cls, event: TraceEvent) -> "TraceEventView":
        return cls(
            seq=event.id,
            kind=event.kind,
            name=event.name,
            input=event.input,
            output=event.output,
            error=event.error,
            attempts=event.attempts,
            latency_ms=event.latency_ms,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            cost_usd=event.cost_usd,
            at=event.created_at,
        )


class TraceView(BaseModel):
    run_id: str
    status: str
    model: str
    prompt_version: str
    workflow_path: list[str]
    summary: TraceSummaryView
    events: list[TraceEventView]


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[str] | None = None
    limit: int | None = Field(default=None, ge=1)


class EvalRunView(BaseModel):
    eval_run_id: str
    status: str
    model: str
    prompt_version: str
    summary: dict[str, Any] | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None
