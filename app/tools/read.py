"""Read tools. They never change state, so they run without a decision step."""

from dataclasses import asdict
from typing import Any

from pydantic import Field

from app.db.models import Order
from app.policies.embeddings import EmbeddingError
from app.policies.retrieval import search_policies
from app.services import customers, orders, refunds
from app.services.errors import TransientError
from app.services.refunds import RefundReason
from app.tools.base import ToolArgs, ToolContext, ToolKind, ToolSpec


class NoArgs(ToolArgs):
    pass


class GetOrderArgs(ToolArgs):
    order_id: str = Field(min_length=1, max_length=40, description="The order id.")


class SearchPolicyArgs(ToolArgs):
    query: str = Field(min_length=3, max_length=300, description="What you need the policy to say.")


class CalculateRefundArgs(ToolArgs):
    order_id: str = Field(min_length=1, max_length=40)
    reason: RefundReason = Field(description="Why the customer wants a refund.")


def _order_view(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.id,
        "item": order.item_summary,
        "total": str(order.total),
        "charged_amount": str(order.charged_amount),
        "currency": order.currency,
        "status": order.status,
        "placed_at": order.placed_at.isoformat(),
        "shipped_at": order.shipped_at.isoformat() if order.shipped_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
    }


async def _get_customer(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    customer = await customers.get_customer(ctx.session, ctx.scope.customer_id)
    return {"customer_id": customer.id, "name": customer.name, "email": customer.email}


async def _list_orders(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    found = await orders.list_orders(ctx.session, ctx.scope.customer_id)
    return {"orders": [_order_view(order) for order in found]}


async def _get_order(ctx: ToolContext, args: GetOrderArgs) -> dict[str, Any]:
    order = await orders.get_owned_order(ctx.session, args.order_id, ctx.scope.customer_id)
    view = _order_view(order)
    view["refunded_total"] = str(await refunds.refunded_total(ctx.session, order.id))
    return view


async def _search_policy(ctx: ToolContext, args: SearchPolicyArgs) -> dict[str, Any]:
    try:
        hits = await search_policies(
            ctx.session, ctx.embedder, args.query, top_k=ctx.settings.policy_top_k
        )
    except EmbeddingError as exc:
        # Retried by the executor, then reported to the model as unavailable.
        raise TransientError(str(exc)) from exc
    return {"results": [asdict(hit) for hit in hits]}


async def _calculate_refund(ctx: ToolContext, args: CalculateRefundArgs) -> dict[str, Any]:
    quote = await refunds.calculate_refund(
        ctx.session,
        order_id=args.order_id,
        customer_id=ctx.scope.customer_id,
        reason=args.reason,
        settings=ctx.settings,
    )
    return {
        "order_id": quote.order_id,
        "reason": quote.reason.value,
        "eligible": quote.eligible,
        "eligibility_code": quote.code,
        "refund_amount": str(quote.amount),
        "currency": quote.currency,
        "explanation": quote.explanation,
        "policy_id": quote.policy_id,
        "automatic_limit": str(ctx.settings.auto_refund_threshold),
    }


READ_TOOLS = [
    ToolSpec(
        name="get_customer",
        description="Get the profile of the customer who opened this ticket.",
        kind=ToolKind.READ,
        args_model=NoArgs,
        handler=_get_customer,
    ),
    ToolSpec(
        name="list_customer_orders",
        description=(
            "List this customer's recent orders, newest first. Use it when the customer "
            "has not given an order id."
        ),
        kind=ToolKind.READ,
        args_model=NoArgs,
        handler=_list_orders,
    ),
    ToolSpec(
        name="get_order",
        description=(
            "Get one of this customer's orders: status, dates, amount charged and amount "
            "already refunded."
        ),
        kind=ToolKind.READ,
        args_model=GetOrderArgs,
        handler=_get_order,
    ),
    ToolSpec(
        name="search_policy",
        description=(
            "Search the company support policies. Returns the most relevant policy "
            "sections with their ids, to cite when you act."
        ),
        kind=ToolKind.READ,
        args_model=SearchPolicyArgs,
        handler=_search_policy,
    ),
    ToolSpec(
        name="calculate_refund",
        description=(
            "Check whether an order is eligible for a refund for a given reason and how "
            "much the policy allows. Always call this before issuing a refund."
        ),
        kind=ToolKind.READ,
        args_model=CalculateRefundArgs,
        handler=_calculate_refund,
    ),
]
