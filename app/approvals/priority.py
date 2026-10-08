"""Orders the approval queue so the requests that matter most are seen first."""

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from app.config import Settings
from app.db.models import Approval

_ACCOUNT_ACTIONS = frozenset({"update_customer_email"})


class Priority(StrEnum):
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"


_RANK = {Priority.URGENT: 0, Priority.HIGH: 1, Priority.NORMAL: 2}


def _amount(approval: Approval) -> Decimal | None:
    """The money at stake: the proposed amount, or the order total for a replacement."""
    order = approval.context.get("order") or {}
    raw = approval.payload.get("amount") or order.get("total")
    try:
        return Decimal(str(raw)) if raw is not None else None
    except InvalidOperation:
        return None


def priority_of(approval: Approval, now: datetime, settings: Settings) -> Priority:
    if now - approval.created_at >= timedelta(minutes=settings.approval_sla_minutes):
        return Priority.URGENT
    amount = _amount(approval)
    if approval.action in _ACCOUNT_ACTIONS or (
        amount is not None and amount >= settings.high_value_approval_amount
    ):
        return Priority.HIGH
    return Priority.NORMAL


def prioritise(
    approvals: Iterable[Approval], now: datetime, settings: Settings
) -> list[tuple[Approval, Priority]]:
    """Most pressing first; within a level, the longest wait first."""
    ranked = [(approval, priority_of(approval, now, settings)) for approval in approvals]
    return sorted(ranked, key=lambda pair: (_RANK[pair[1]], pair[0].created_at))
