"""Replacement shipment rules and the only code path that creates one."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import Settings
from app.db.models import Order, Replacement, new_id
from app.services.authorization import require_approved
from app.services.decisions import Decision, Verdict
from app.services.errors import ActionDenied
from app.services.orders import get_owned_order
from app.services.refunds import RefundReason, quote_refund, refunded_total

CREATE_REPLACEMENT = "create_shipping_replacement"


class ReplacementReason(StrEnum):
    DAMAGED_ITEM = "damaged_item"
    LOST_SHIPMENT = "lost_shipment"


async def _decide(
    session: AsyncSession,
    order: Order,
    reason: ReplacementReason,
    *,
    settings: Settings,
    review_requested: bool,
    now: datetime,
) -> Decision:
    already = await session.scalar(select(Replacement.id).where(Replacement.order_id == order.id))
    if already is not None:
        return Decision.deny("A replacement has already been created for this order.")

    refunded = await refunded_total(session, order.id)
    if refunded > order.charged_amount - order.total:
        return Decision.deny("The order has been refunded, so it cannot also be replaced.")

    # A replacement is owed in exactly the cases where the item refund would be.
    quote = quote_refund(order, refunded, RefundReason(reason.value), now=now, settings=settings)
    context = {"eligibility": quote.code, "order_total": str(order.total), "policy_id": "shipping"}

    if not quote.eligible:
        if review_requested:
            return Decision.needs_approval(f"Policy exception: {quote.explanation}", **context)
        return Decision.deny(f"Not eligible: {quote.explanation}")
    if order.total > settings.auto_replacement_threshold:
        return Decision.needs_approval(
            f"The order value is above the automatic replacement limit of "
            f"{settings.auto_replacement_threshold} {order.currency}.",
            **context,
        )
    if review_requested:
        return Decision.needs_approval("The agent asked for a human decision.", **context)
    return Decision.allow()


async def decide_replacement_request(
    session: AsyncSession,
    *,
    order_id: str,
    customer_id: str,
    reason: ReplacementReason,
    settings: Settings,
    review_requested: bool = False,
) -> Decision:
    order = await get_owned_order(session, order_id, customer_id)
    return await _decide(
        session, order, reason, settings=settings, review_requested=review_requested, now=utcnow()
    )


async def create_replacement(
    session: AsyncSession,
    *,
    order_id: str,
    customer_id: str,
    reason: ReplacementReason,
    idempotency_key: str,
    run_id: str,
    approval_id: str | None,
    settings: Settings,
) -> tuple[Replacement, bool]:
    """Create a replacement shipment. Returns it and whether this call created it."""
    order = await get_owned_order(session, order_id, customer_id, for_update=True)

    existing = await session.scalar(
        select(Replacement).where(Replacement.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.order_id != order.id:
            raise ActionDenied("This idempotency key was already used for a different order.")
        return existing, False

    decision = await _decide(
        session,
        order,
        reason,
        settings=settings,
        review_requested=approval_id is not None,
        now=utcnow(),
    )
    if decision.verdict is Verdict.DENY:
        raise ActionDenied(decision.reason)
    if decision.verdict is Verdict.NEEDS_APPROVAL:
        await require_approved(
            session,
            approval_id,
            run_id=run_id,
            action=CREATE_REPLACEMENT,
            matches=lambda payload: payload.get("order_id") == order_id,
        )

    replacement = Replacement(
        id=new_id("rpl"),
        order_id=order.id,
        reason=reason.value,
        idempotency_key=idempotency_key,
        run_id=run_id,
        approval_id=approval_id if decision.verdict is Verdict.NEEDS_APPROVAL else None,
    )
    session.add(replacement)
    await session.flush()
    return replacement, True
