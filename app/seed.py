"""Loads demo customers, orders and the policy documents. Safe to run again.

uv run python -m app.seed
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm.factory import build_embedder
from app.clock import utcnow
from app.config import get_settings
from app.db.models import Customer, Order
from app.db.session import create_engine, create_session_factory
from app.eventloop import run
from app.policies.embeddings import Embedder
from app.policies.retrieval import ingest_policies

SEED_FILE = Path(__file__).parent.parent / "fixtures" / "seed.json"


def _order(data: dict[str, Any]) -> Order:
    now = utcnow()
    delivered_days = data.get("delivered_days_ago")
    delivered_at = now - timedelta(days=delivered_days) if delivered_days is not None else None
    shipped_days = data.get("shipped_days_ago")
    shipped_at: datetime | None
    if shipped_days is not None:
        shipped_at = now - timedelta(days=shipped_days)
    else:
        shipped_at = delivered_at - timedelta(days=3) if delivered_at else None
    return Order(
        id=data["id"],
        customer_id=data["customer_id"],
        item_summary=data["item"],
        total=Decimal(data["total"]),
        charged_amount=Decimal(data.get("charged", data["total"])),
        currency="USD",
        status=data["status"],
        placed_at=(shipped_at or now) - timedelta(days=2),
        shipped_at=shipped_at,
        delivered_at=delivered_at,
    )


async def seed(session: AsyncSession, embedder: Embedder) -> dict[str, int]:
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    added = {"customers": 0, "orders": 0}
    for customer in data["customers"]:
        if await session.get(Customer, customer["id"]) is None:
            session.add(Customer(**customer))
            added["customers"] += 1
    await session.flush()
    for order in data["orders"]:
        if await session.get(Order, order["id"]) is None:
            session.add(_order(order))
            added["orders"] += 1
    added["policy_chunks"] = await ingest_policies(session, embedder)
    return added


async def main() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with create_session_factory(engine).begin() as session:
            print(f"seeded: {await seed(session, build_embedder(settings))}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    run(main())
