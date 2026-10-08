"""Database schema. Mirrors PRD section 11 plus the tables needed for traces and policy search."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.clock import utcnow

EMBEDDING_DIM = 256


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class OrderStatus(StrEnum):
    PLACED = "placed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class TicketStatus(StrEnum):
    OPEN = "open"
    PENDING_APPROVAL = "pending_approval"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[dict[str, Any]]: JSONB,
    }


Money = Numeric(12, 2)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    item_summary: Mapped[str] = mapped_column(String(300))
    total: Mapped[Decimal] = mapped_column(Money)
    # What the customer actually paid. Above `total` when they were charged twice.
    charged_amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[str] = mapped_column(String(20))
    placed_at: Mapped[datetime]
    shipped_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]


class Ticket(Base):
    __tablename__ = "tickets"
    # One ticket per message from an outside channel, however often it is delivered.
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_tickets_channel_external_id"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    channel: Mapped[str] = mapped_column(String(20), default="api", server_default="api")
    # The channel's own id for the message that opened the ticket, such as an email Message-ID.
    external_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=TicketStatus.OPEN)
    messages: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING, index=True)
    outcome: Mapped[str | None] = mapped_column(String(40))
    state_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    final_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # Rule-based quality checks, scored when the run finishes.
    quality: Mapped[dict[str, Any] | None]
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None]


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("run_id", "tool_call_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]]
    reason: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    policy_citation: Mapped[dict[str, Any] | None]
    status: Mapped[str] = mapped_column(String(20), default=ApprovalStatus.PENDING, index=True)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None]


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="issued")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Replacement(Base):
    __tablename__ = "replacements"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    reason: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="created")
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TraceEvent(Base):
    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80))
    input: Mapped[dict[str, Any] | None]
    output: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PolicyChunk(Base):
    __tablename__ = "policy_chunks"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(60), index=True)
    version: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(200))
    section: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    model: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(40))
    summary: Mapped[dict[str, Any] | None]
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None]
