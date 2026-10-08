"""The refund rules are pure functions, so they are tested without a database."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.db.models import Order
from app.services.decisions import Verdict
from app.services.refunds import RefundReason, decide_refund, quote_refund

NOW = datetime(2026, 6, 1, tzinfo=UTC)
SETTINGS = Settings(_env_file=None)


def order(
    total: str = "50.00",
    *,
    status: str = "delivered",
    delivered_days_ago: int | None = 5,
    shipped_days_ago: int | None = None,
    charged: str | None = None,
) -> Order:
    return Order(
        id="ORD-1",
        customer_id="cus_1",
        item_summary="Headphones",
        total=Decimal(total),
        charged_amount=Decimal(charged or total),
        currency="USD",
        status=status,
        placed_at=NOW - timedelta(days=40),
        shipped_at=NOW - timedelta(days=shipped_days_ago) if shipped_days_ago is not None else None,
        delivered_at=(
            NOW - timedelta(days=delivered_days_ago) if delivered_days_ago is not None else None
        ),
    )


def quote(target: Order, reason: RefundReason, refunded: str = "0"):  # type: ignore[no-untyped-def]
    return quote_refund(target, Decimal(refunded), reason, now=NOW, settings=SETTINGS)


class TestQuote:
    def test_damaged_item_inside_window_is_refunded_in_full(self) -> None:
        result = quote(order("50.00"), RefundReason.DAMAGED_ITEM)
        assert result.eligible
        assert result.amount == Decimal("50.00")

    @pytest.mark.parametrize(("days", "eligible"), [(29, True), (30, True), (31, False)])
    def test_refund_window_boundary(self, days: int, eligible: bool) -> None:
        result = quote(order(delivered_days_ago=days), RefundReason.CHANGED_MIND)
        assert result.eligible is eligible
        if not eligible:
            assert result.code == "outside_refund_window"
            assert result.amount == Decimal("0.00")

    def test_undelivered_order_cannot_be_returned(self) -> None:
        target = order(status="shipped", delivered_days_ago=None, shipped_days_ago=2)
        assert quote(target, RefundReason.CHANGED_MIND).code == "not_delivered"

    def test_cancelled_order_is_not_refundable(self) -> None:
        target = order(status="cancelled", delivered_days_ago=None)
        assert quote(target, RefundReason.DAMAGED_ITEM).code == "order_cancelled"

    def test_fully_refunded_order_is_not_refundable_again(self) -> None:
        result = quote(order("50.00"), RefundReason.DAMAGED_ITEM, refunded="50.00")
        assert result.code == "already_refunded"

    def test_partial_refund_leaves_only_the_balance(self) -> None:
        result = quote(order("50.00"), RefundReason.DAMAGED_ITEM, refunded="20.00")
        assert result.amount == Decimal("30.00")

    @pytest.mark.parametrize(("days", "code"), [(9, "shipment_in_transit"), (10, "lost_shipment")])
    def test_shipment_is_lost_after_ten_days(self, days: int, code: str) -> None:
        target = order(status="shipped", delivered_days_ago=None, shipped_days_ago=days)
        assert quote(target, RefundReason.LOST_SHIPMENT).code == code

    def test_delivered_order_is_not_a_lost_shipment(self) -> None:
        assert quote(order(), RefundReason.LOST_SHIPMENT).code == "order_delivered"

    def test_duplicate_charge_refunds_only_the_extra_charge(self) -> None:
        result = quote(order("40.00", charged="80.00"), RefundReason.DUPLICATE_CHARGE)
        assert result.eligible
        assert result.amount == Decimal("40.00")

    def test_duplicate_charge_is_refunded_once(self) -> None:
        target = order("40.00", charged="80.00")
        result = quote(target, RefundReason.DUPLICATE_CHARGE, refunded="40.00")
        assert result.code == "no_duplicate_charge"

    def test_single_charge_is_not_a_duplicate(self) -> None:
        assert quote(order(), RefundReason.DUPLICATE_CHARGE).code == "no_duplicate_charge"

    def test_unlisted_reason_needs_a_person(self) -> None:
        assert quote(order(), RefundReason.OTHER).code == "requires_review"


class TestDecide:
    def decide(
        self, total: str, amount: str, *, reason=RefundReason.DAMAGED_ITEM, review=False, **kw
    ):  # type: ignore[no-untyped-def]
        return decide_refund(
            quote(order(total, **kw), reason),
            Decimal(amount),
            settings=SETTINGS,
            review_requested=review,
        )

    def test_small_eligible_refund_runs_automatically(self) -> None:
        assert self.decide("50.00", "50.00").verdict is Verdict.ALLOW

    @pytest.mark.parametrize(
        ("amount", "verdict"),
        [("100.00", Verdict.ALLOW), ("100.01", Verdict.NEEDS_APPROVAL)],
    )
    def test_automatic_limit_boundary(self, amount: str, verdict: Verdict) -> None:
        assert self.decide("500.00", amount).verdict is verdict

    def test_amount_above_hard_cap_is_refused_even_with_review(self) -> None:
        decision = self.decide("1500.00", "1500.00", review=True)
        assert decision.verdict is Verdict.DENY
        assert "Escalate" in decision.reason

    def test_amount_above_policy_amount_is_refused(self) -> None:
        # 40.00 order charged twice: only 40.00 is owed, though 80.00 was paid.
        decision = self.decide(
            "40.00", "80.00", reason=RefundReason.DUPLICATE_CHARGE, charged="80.00"
        )
        assert decision.verdict is Verdict.DENY

    def test_amount_above_balance_is_refused(self) -> None:
        assert self.decide("50.00", "500.00").verdict is Verdict.DENY

    @pytest.mark.parametrize("amount", ["0", "-5.00"])
    def test_non_positive_amount_is_refused(self, amount: str) -> None:
        assert self.decide("50.00", amount).verdict is Verdict.DENY

    def test_ineligible_refund_is_refused_outright(self) -> None:
        assert self.decide("50.00", "50.00", delivered_days_ago=60).verdict is Verdict.DENY

    def test_ineligible_refund_can_go_to_a_reviewer_as_an_exception(self) -> None:
        decision = self.decide("50.00", "50.00", delivered_days_ago=60, review=True)
        assert decision.verdict is Verdict.NEEDS_APPROVAL
        assert decision.context["eligibility"] == "outside_refund_window"

    def test_uncovered_reason_always_goes_to_a_reviewer(self) -> None:
        decision = self.decide("50.00", "50.00", reason=RefundReason.OTHER)
        assert decision.verdict is Verdict.NEEDS_APPROVAL
