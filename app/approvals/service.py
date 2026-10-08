"""The approval queue: requests raised by a run, and a reviewer's decision on each."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clock import utcnow
from app.db.models import (
    AgentRun,
    Approval,
    ApprovalStatus,
    Customer,
    Order,
    RunStatus,
    Ticket,
    TicketStatus,
    new_id,
)
from app.db.session import SessionFactory
from app.observability.recorder import EventKind, TraceRecorder
from app.services.errors import ApprovalNotFound, ApprovalStateError
from app.tools.base import RunScope


class ApprovalService:
    def __init__(self, session_factory: SessionFactory, recorder: TraceRecorder) -> None:
        self._session_factory = session_factory
        self._recorder = recorder

    async def request(
        self,
        scope: RunScope,
        *,
        tool_call_id: str,
        action: str,
        payload: dict[str, Any],
        reason: str,
        context: dict[str, Any],
        policy_citation: dict[str, Any] | None,
    ) -> Approval:
        """Create the approval for one proposed action, or return the one that exists.

        A paused run re-enters its approval step when it resumes, so this must
        be safe to call again for the same tool call.
        """
        existing = await self._find(scope.run_id, tool_call_id)
        if existing is not None:
            return existing

        try:
            async with self._session_factory.begin() as session:
                customer = await session.get(Customer, scope.customer_id)
                order_id = payload.get("order_id")
                order = (
                    await session.scalar(
                        select(Order).where(
                            Order.id == order_id, Order.customer_id == scope.customer_id
                        )
                    )
                    if order_id
                    else None
                )
                approval = Approval(
                    id=new_id("apr"),
                    run_id=scope.run_id,
                    tool_call_id=tool_call_id,
                    action=action,
                    payload=payload,
                    reason=reason,
                    context={
                        **context,
                        "customer": _customer_view(customer),
                        "order": _order_view(order),
                    },
                    policy_citation=policy_citation,
                )
                session.add(approval)
                run = await session.get(AgentRun, scope.run_id)
                if run is not None:
                    run.status = RunStatus.AWAITING_APPROVAL
                ticket = await session.get(Ticket, scope.ticket_id)
                if ticket is not None:
                    ticket.status = TicketStatus.PENDING_APPROVAL
        except IntegrityError:
            # Lost a race with another attempt at the same step.
            raced = await self._find(scope.run_id, tool_call_id)
            if raced is None:
                raise
            return raced

        await self._recorder.record(
            scope.run_id,
            EventKind.APPROVAL,
            "approval_requested",
            input={"action": action, "payload": payload},
            output={"approval_id": approval.id, "reason": reason},
        )
        return approval

    async def decide(
        self, approval_id: str, *, approve: bool, reviewer: str, note: str | None
    ) -> Approval:
        """Record a reviewer's decision. A request can be decided exactly once."""
        async with self._session_factory.begin() as session:
            approval = await session.get(Approval, approval_id, with_for_update=True)
            if approval is None:
                raise ApprovalNotFound(f"No approval {approval_id}.")
            if approval.status != ApprovalStatus.PENDING:
                raise ApprovalStateError(f"Approval {approval_id} was already {approval.status}.")
            approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            approval.reviewer = reviewer
            approval.decision_note = note
            approval.decided_at = utcnow()

        await self._recorder.record(
            approval.run_id,
            EventKind.APPROVAL,
            f"approval_{approval.status}",
            input={"approval_id": approval.id, "reviewer": reviewer},
            output={"note": note},
        )
        return approval

    async def get(self, approval_id: str) -> Approval:
        async with self._session_factory() as session:
            approval = await session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalNotFound(f"No approval {approval_id}.")
        return approval

    async def list_approvals(
        self, status: ApprovalStatus | None = None, limit: int = 50
    ) -> Sequence[Approval]:
        query = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(Approval.status == status)
        async with self._session_factory() as session:
            return (await session.scalars(query)).all()

    async def _find(self, run_id: str, tool_call_id: str) -> Approval | None:
        async with self._session_factory() as session:
            found: Approval | None = await session.scalar(
                select(Approval).where(
                    Approval.run_id == run_id, Approval.tool_call_id == tool_call_id
                )
            )
            return found


def _customer_view(customer: Customer | None) -> dict[str, Any] | None:
    if customer is None:
        return None
    return {"customer_id": customer.id, "name": customer.name, "email": customer.email}


def _order_view(order: Order | None) -> dict[str, Any] | None:
    if order is None:
        return None
    return {
        "order_id": order.id,
        "item": order.item_summary,
        "total": str(order.total),
        "charged_amount": str(order.charged_amount),
        "currency": order.currency,
        "status": order.status,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
    }
