"""Creates the customer and orders a scenario needs, isolated from every other scenario."""

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.db.models import Customer, Order, Refund, new_id
from evals.scenarios.model import OrderSeed, Scenario

_PLACEHOLDER = re.compile(r"\{(order:[a-z0-9_]+|missing|new_email)\}")


@dataclass(frozen=True)
class SeededScenario:
    scenario: Scenario
    customer_id: str
    customer_email: str
    other_customer_id: str
    own_order_ids: tuple[str, ...]
    other_order_ids: tuple[str, ...]
    placeholders: dict[str, str]

    @property
    def message(self) -> str:
        return str(self.resolve(self.scenario.message))

    def resolve(self, value: Any) -> Any:
        """Substitute placeholders in a string, or in every string of a dict."""
        if isinstance(value, str):
            return _PLACEHOLDER.sub(lambda match: self.placeholders[match.group(1)], value)
        if isinstance(value, dict):
            return {key: self.resolve(item) for key, item in value.items()}
        return value


def _order_id() -> str:
    return f"ORD-{uuid4().int % 10**8:08d}"


def _build_order(seed: OrderSeed, order_id: str, customer_id: str) -> Order:
    now = utcnow()
    delivered_at = (
        now - timedelta(days=seed.delivered_days_ago)
        if seed.status == "delivered" and seed.delivered_days_ago is not None
        else None
    )
    if seed.shipped_days_ago is not None:
        shipped_at = now - timedelta(days=seed.shipped_days_ago)
    elif delivered_at is not None:
        shipped_at = delivered_at - timedelta(days=3)
    else:
        shipped_at = None
    placed_at = (shipped_at or now) - timedelta(days=2)
    return Order(
        id=order_id,
        customer_id=customer_id,
        item_summary=seed.item,
        total=Decimal(seed.total),
        charged_amount=Decimal(seed.charged or seed.total),
        currency="USD",
        status=seed.status,
        placed_at=placed_at,
        shipped_at=shipped_at,
        delivered_at=delivered_at,
    )


async def seed_scenario(session: AsyncSession, scenario: Scenario) -> SeededScenario:
    tag = uuid4().hex[:10]
    customer = Customer(
        id=new_id("cus"), email=f"eval-{tag}@example.com", name="Evaluation Customer"
    )
    other = Customer(id=new_id("cus"), email=f"other-{tag}@example.com", name="Someone Else")
    session.add_all([customer, other])
    await session.flush()

    placeholders = {"missing": _order_id(), "new_email": f"new-{tag}@example.com"}
    own: list[str] = []
    others: list[str] = []
    for seed in scenario.orders:
        order_id = _order_id()
        placeholders[f"order:{seed.key}"] = order_id
        owner = customer if seed.owner == "customer" else other
        (own if seed.owner == "customer" else others).append(order_id)
        session.add(_build_order(seed, order_id, owner.id))
        await session.flush()
        if seed.refunded:
            session.add(
                Refund(
                    id=new_id("ref"),
                    order_id=order_id,
                    amount=Decimal(seed.refunded),
                    currency="USD",
                    reason="changed_mind",
                    idempotency_key=f"seed-{order_id}",
                )
            )
    await session.flush()
    return SeededScenario(
        scenario=scenario,
        customer_id=customer.id,
        customer_email=customer.email,
        other_customer_id=other.id,
        own_order_ids=tuple(own),
        other_order_ids=tuple(others),
        placeholders=placeholders,
    )
