from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order
from app.services.errors import OrderNotFound


async def get_owned_order(
    session: AsyncSession, order_id: str, customer_id: str, *, for_update: bool = False
) -> Order:
    """Load an order only if it belongs to this customer.

    Someone else's order and a missing order raise the same error, so the
    agent cannot be used to probe which order ids exist.
    """
    query = select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
    if for_update:
        query = query.with_for_update()
    order = await session.scalar(query)
    if order is None:
        raise OrderNotFound(f"No order {order_id} on this customer's account.")
    return order


async def list_orders(session: AsyncSession, customer_id: str, limit: int = 10) -> Sequence[Order]:
    result = await session.scalars(
        select(Order)
        .where(Order.customer_id == customer_id)
        .order_by(Order.placed_at.desc())
        .limit(limit)
    )
    return result.all()
