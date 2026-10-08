"""Refund rules and the only code path that creates a refund.

The model may propose a refund. Whether it happens, and for how much, is
decided here.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.config import Settings
from app.db.models import Order, OrderStatus, Refund, new_id
from app.services.authorization import require_approved
from app.services.decisions import Decision, Verdict
from app.services.errors import ActionDenied
from app.services.orders import get_owned_order

ISSUE_REFUND = "issue_refund"
ZERO = Decimal("0.00")


class RefundReason(StrEnum):
    DAMAGED_ITEM = "damaged_item"
    LOST_SHIPMENT = "lost_shipment"
    DUPLICATE_CHARGE = "duplicate_charge"
    CHANGED_MIND = "changed_mind"
    OTHER = "other"


@dataclass(frozen=True)
class RefundQuote:
    order_id: str
    reason: RefundReason
    eligible: bool
    code: str
    amount: Decimal
    currency: str
    explanation: str
    policy_id: str
    refundable_balance: Decimal


def quote_refund(
    order: Order,
    refunded_total: Decimal,
    reason: RefundReason,
    *,
    now: datetime,
    settings: Settings,
) -> RefundQuote:
    """Work out what the policy allows for this order and reason. Pure."""
    balance = max(order.charged_amount - refunded_total, ZERO)

    def quote(
        eligible: bool, code: str, amount: Decimal, explanation: str, policy: str
    ) -> RefundQuote:
        return RefundQuote(
            order_id=order.id,
            reason=reason,
            eligible=eligible,
            code=code,
            amount=amount if eligible else ZERO,
            currency=order.currency,
            explanation=explanation,
            policy_id=policy,
            refundable_balance=balance,
        )

    def no(code: str, explanation: str, policy: str = "refunds") -> RefundQuote:
        return quote(False, code, ZERO, explanation, policy)

    if balance <= ZERO:
        return no("already_refunded", "This order has already been refunded in full.")

    if reason is RefundReason.DUPLICATE_CHARGE:
        overcharge = order.charged_amount - order.total - refunded_total
        if overcharge <= ZERO:
            return no("no_duplicate_charge", "Payment records show a single charge.", "billing")
        return quote(
            True, "duplicate_charge", overcharge, "The duplicate charge is refundable.", "billing"
        )

    if order.status == OrderStatus.CANCELLED:
        return no("order_cancelled", "The order was cancelled.")

    item_refund = min(order.total, balance)

    if reason is RefundReason.LOST_SHIPMENT:
        if order.status == OrderStatus.DELIVERED:
            return no("order_delivered", "Tracking shows the order as delivered.", "shipping")
        if order.status != OrderStatus.SHIPPED or order.shipped_at is None:
            return no("not_shipped", "The order has not shipped yet.", "shipping")
        if now - order.shipped_at < timedelta(days=settings.lost_shipment_days):
            return no(
                "shipment_in_transit",
                f"A shipment counts as lost after {settings.lost_shipment_days} days in transit.",
                "shipping",
            )
        return quote(True, "lost_shipment", item_refund, "The shipment is lost.", "shipping")

    if reason in (RefundReason.DAMAGED_ITEM, RefundReason.CHANGED_MIND):
        if order.status != OrderStatus.DELIVERED or order.delivered_at is None:
            return no("not_delivered", "The order has not been delivered.")
        if now - order.delivered_at > timedelta(days=settings.refund_window_days):
            return no(
                "outside_refund_window",
                f"The {settings.refund_window_days}-day refund window has passed.",
            )
        return quote(True, reason.value, item_refund, "Within the refund window.", "refunds")

    return no("requires_review", "No refund rule covers this reason. A person must decide.")


def decide_refund(
    quote: RefundQuote, amount: Decimal, *, settings: Settings, review_requested: bool
) -> Decision:
    """Decide whether a refund of `amount` may run, needs approval, or is refused. Pure.

    `review_requested` is true when the agent asked for a human decision or an
    approval already exists. It lets a person grant a policy exception, but
    never lifts the hard limits.
    """
    if amount <= ZERO:
        return Decision.deny("The refund amount must be positive.")
    if quote.code == "already_refunded":
        return Decision.deny(quote.explanation)
    if amount > quote.refundable_balance:
        return Decision.deny(
            f"The amount exceeds the refundable balance of {quote.refundable_balance} "
            f"{quote.currency}."
        )
    if amount > settings.max_refund_amount:
        return Decision.deny(
            f"Refunds above {settings.max_refund_amount} {quote.currency} cannot be issued "
            "by the agent. Escalate to a human."
        )

    context = {
        "eligibility": quote.code,
        "eligible_amount": str(quote.amount),
        "refundable_balance": str(quote.refundable_balance),
        "policy_id": quote.policy_id,
    }

    if not quote.eligible:
        if review_requested or quote.code == "requires_review":
            return Decision.needs_approval(f"Policy exception: {quote.explanation}", **context)
        return Decision.deny(f"Not eligible: {quote.explanation}")

    if amount > quote.amount:
        return Decision.deny(
            f"The policy allows at most {quote.amount} {quote.currency} for this reason."
        )
    if amount > settings.auto_refund_threshold:
        return Decision.needs_approval(
            f"The amount is above the automatic limit of {settings.auto_refund_threshold} "
            f"{quote.currency}.",
            **context,
        )
    if review_requested:
        return Decision.needs_approval("The agent asked for a human decision.", **context)
    return Decision.allow()


async def refunded_total(session: AsyncSession, order_id: str) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(Refund.amount), ZERO)).where(Refund.order_id == order_id)
    )
    return Decimal(total or ZERO)


async def calculate_refund(
    session: AsyncSession,
    *,
    order_id: str,
    customer_id: str,
    reason: RefundReason,
    settings: Settings,
    now: datetime | None = None,
) -> RefundQuote:
    order = await get_owned_order(session, order_id, customer_id)
    return quote_refund(
        order,
        await refunded_total(session, order.id),
        reason,
        now=now or utcnow(),
        settings=settings,
    )


async def decide_refund_request(
    session: AsyncSession,
    *,
    order_id: str,
    customer_id: str,
    amount: Decimal,
    reason: RefundReason,
    settings: Settings,
    review_requested: bool = False,
) -> Decision:
    quote = await calculate_refund(
        session, order_id=order_id, customer_id=customer_id, reason=reason, settings=settings
    )
    return decide_refund(quote, amount, settings=settings, review_requested=review_requested)


async def issue_refund(
    session: AsyncSession,
    *,
    order_id: str,
    customer_id: str,
    amount: Decimal,
    reason: RefundReason,
    idempotency_key: str,
    run_id: str,
    approval_id: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[Refund, bool]:
    """Create a refund. Returns the refund and whether this call created it.

    The order row is locked first, so concurrent calls for one order run one
    after another and the balance check cannot be raced.
    """
    order = await get_owned_order(session, order_id, customer_id, for_update=True)

    existing = await session.scalar(select(Refund).where(Refund.idempotency_key == idempotency_key))
    if existing is not None:
        if existing.order_id != order.id or existing.amount != amount:
            raise ActionDenied("This idempotency key was already used for a different refund.")
        return existing, False

    quote = quote_refund(
        order,
        await refunded_total(session, order.id),
        reason,
        now=now or utcnow(),
        settings=settings,
    )
    decision = decide_refund(
        quote, amount, settings=settings, review_requested=approval_id is not None
    )
    if decision.verdict is Verdict.DENY:
        raise ActionDenied(decision.reason)
    if decision.verdict is Verdict.NEEDS_APPROVAL:
        await require_approved(
            session,
            approval_id,
            run_id=run_id,
            action=ISSUE_REFUND,
            matches=lambda payload: (
                payload.get("order_id") == order_id
                and Decimal(str(payload.get("amount", "0"))) == amount
            ),
        )

    refund = Refund(
        id=new_id("ref"),
        order_id=order.id,
        amount=amount,
        currency=order.currency,
        reason=reason.value,
        idempotency_key=idempotency_key,
        run_id=run_id,
        approval_id=approval_id if decision.verdict is Verdict.NEEDS_APPROVAL else None,
    )
    session.add(refund)
    await session.flush()
    return refund, True
