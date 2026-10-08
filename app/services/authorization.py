"""Checks that a write which needs approval really has one."""

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, ApprovalStatus
from app.services.errors import ApprovalRequired


async def require_approved(
    session: AsyncSession,
    approval_id: str | None,
    *,
    run_id: str,
    action: str,
    matches: Callable[[dict[str, Any]], bool],
) -> Approval:
    """Return the approval backing this write, or raise.

    The approval must be approved, belong to the same run, and cover exactly
    this action and payload. An approval for one refund cannot pay for another.
    """
    if approval_id is None:
        raise ApprovalRequired(f"{action} needs human approval before it can run.")
    approval = await session.get(Approval, approval_id)
    if (
        approval is None
        or approval.status != ApprovalStatus.APPROVED
        or approval.run_id != run_id
        or approval.action != action
        or not matches(approval.payload)
    ):
        raise ApprovalRequired(f"{action} is not covered by an approved request.")
    return approval
