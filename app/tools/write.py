"""Write tools. Each one passes a decision rule before it may run."""

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import EmailStr, Field, model_validator

from app.db.models import TicketStatus
from app.services import customers, refunds, replacements, tickets
from app.services.decisions import Decision
from app.services.refunds import RefundReason
from app.services.replacements import ReplacementReason
from app.tools.base import ToolArgs, ToolContext, ToolKind, ToolSpec, idempotency_key

OrderId = Annotated[str, Field(min_length=1, max_length=40)]


class IssueRefundArgs(ToolArgs):
    order_id: OrderId
    amount: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    reason: RefundReason


class CreateReplacementArgs(ToolArgs):
    order_id: OrderId
    reason: ReplacementReason


class UpdateTicketArgs(ToolArgs):
    # `escalated` is deliberately absent: only escalate_to_human sets it.
    status: Literal["open", "pending_approval", "resolved"]
    note: str = Field(min_length=1, max_length=2000, description="Internal note on what was done.")
    order_id: str | None = Field(default=None, max_length=40)


class UpdateCustomerEmailArgs(ToolArgs):
    new_email: EmailStr


class EscalateArgs(ToolArgs):
    reason: str = Field(min_length=1, max_length=1000)


class ApprovalRequestArgs(ToolArgs):
    action: Literal["issue_refund", "create_shipping_replacement", "update_customer_email"]
    justification: str = Field(
        min_length=1, max_length=1000, description="Why a person should approve this."
    )
    policy_citation: str | None = Field(
        default=None, max_length=200, description="Id of the policy section this relies on."
    )
    order_id: str | None = Field(default=None, max_length=40)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=2)
    reason: str | None = Field(
        default=None, max_length=40, description="The refund or replacement reason."
    )
    new_email: EmailStr | None = None

    @model_validator(mode="after")
    def _has_fields_for_action(self) -> "ApprovalRequestArgs":
        required = {
            "issue_refund": ("order_id", "amount", "reason"),
            "create_shipping_replacement": ("order_id", "reason"),
            "update_customer_email": ("new_email",),
        }[self.action]
        missing = [name for name in required if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.action} needs: {', '.join(missing)}")
        return self


# --- issue_refund -----------------------------------------------------------


async def _decide_refund(ctx: ToolContext, args: IssueRefundArgs, review: bool) -> Decision:
    return await refunds.decide_refund_request(
        ctx.session,
        order_id=args.order_id,
        customer_id=ctx.scope.customer_id,
        amount=args.amount,
        reason=args.reason,
        settings=ctx.settings,
        review_requested=review,
    )


async def _issue_refund(ctx: ToolContext, args: IssueRefundArgs) -> dict[str, Any]:
    refund, created = await refunds.issue_refund(
        ctx.session,
        order_id=args.order_id,
        customer_id=ctx.scope.customer_id,
        amount=args.amount,
        reason=args.reason,
        idempotency_key=idempotency_key(
            ctx.scope.run_id, "issue_refund", args.model_dump(mode="json")
        ),
        run_id=ctx.scope.run_id,
        approval_id=ctx.approval_id,
        settings=ctx.settings,
    )
    return {
        "refund_id": refund.id,
        "order_id": refund.order_id,
        "amount": str(refund.amount),
        "currency": refund.currency,
        "status": refund.status,
        "already_issued": not created,
    }


# --- create_shipping_replacement --------------------------------------------


async def _decide_replacement(
    ctx: ToolContext, args: CreateReplacementArgs, review: bool
) -> Decision:
    return await replacements.decide_replacement_request(
        ctx.session,
        order_id=args.order_id,
        customer_id=ctx.scope.customer_id,
        reason=args.reason,
        settings=ctx.settings,
        review_requested=review,
    )


async def _create_replacement(ctx: ToolContext, args: CreateReplacementArgs) -> dict[str, Any]:
    replacement, created = await replacements.create_replacement(
        ctx.session,
        order_id=args.order_id,
        customer_id=ctx.scope.customer_id,
        reason=args.reason,
        idempotency_key=idempotency_key(
            ctx.scope.run_id, "create_shipping_replacement", args.model_dump(mode="json")
        ),
        run_id=ctx.scope.run_id,
        approval_id=ctx.approval_id,
        settings=ctx.settings,
    )
    return {
        "replacement_id": replacement.id,
        "order_id": replacement.order_id,
        "status": replacement.status,
        "already_created": not created,
    }


# --- update_customer_email --------------------------------------------------


async def _decide_email(ctx: ToolContext, args: UpdateCustomerEmailArgs, _: bool) -> Decision:
    return await customers.decide_email_update(
        ctx.session, ctx.scope.customer_id, str(args.new_email)
    )


async def _update_email(ctx: ToolContext, args: UpdateCustomerEmailArgs) -> dict[str, Any]:
    customer = await customers.update_email(
        ctx.session,
        customer_id=ctx.scope.customer_id,
        new_email=str(args.new_email),
        run_id=ctx.scope.run_id,
        approval_id=ctx.approval_id,
    )
    return {"customer_id": customer.id, "email": customer.email}


# --- ticket tools -----------------------------------------------------------


async def _always_allow(_ctx: ToolContext, _args: ToolArgs, _review: bool) -> Decision:
    return Decision.allow("ticket updates carry no financial or account risk")


async def _update_ticket(ctx: ToolContext, args: UpdateTicketArgs) -> dict[str, Any]:
    ticket = await tickets.update_ticket(
        ctx.session,
        ticket_id=ctx.scope.ticket_id,
        customer_id=ctx.scope.customer_id,
        status=TicketStatus(args.status),
        note=args.note,
        order_id=args.order_id,
    )
    return {"ticket_id": ticket.id, "status": str(ticket.status)}


async def _escalate(ctx: ToolContext, args: EscalateArgs) -> dict[str, Any]:
    ticket = await tickets.update_ticket(
        ctx.session,
        ticket_id=ctx.scope.ticket_id,
        customer_id=ctx.scope.customer_id,
        status=TicketStatus.ESCALATED,
        note=f"Escalated to a human: {args.reason}",
    )
    return {"ticket_id": ticket.id, "status": str(ticket.status), "escalated": True}


# --- create_approval_request ------------------------------------------------


def _resolve_approval_request(args: ApprovalRequestArgs) -> tuple[str, dict[str, Any]]:
    """Turn a review request into the name and raw arguments of the action under review."""
    fields = {
        "issue_refund": ("order_id", "amount", "reason"),
        "create_shipping_replacement": ("order_id", "reason"),
        "update_customer_email": ("new_email",),
    }[args.action]
    dumped = args.model_dump(mode="json")
    return args.action, {name: dumped[name] for name in fields}


async def _never_runs(_ctx: ToolContext, _args: ApprovalRequestArgs) -> dict[str, Any]:
    raise AssertionError("create_approval_request is resolved to its target action first.")


WRITE_TOOLS = [
    ToolSpec(
        name="issue_refund",
        description=(
            "Refund money to the customer for one of their orders. The amount must not "
            "exceed what calculate_refund returned. Large refunds pause for human approval."
        ),
        kind=ToolKind.WRITE,
        args_model=IssueRefundArgs,
        handler=_issue_refund,
        decide=_decide_refund,
    ),
    ToolSpec(
        name="create_shipping_replacement",
        description=(
            "Send a free replacement for a damaged item or a lost shipment. Not available "
            "for an order that was refunded."
        ),
        kind=ToolKind.WRITE,
        args_model=CreateReplacementArgs,
        handler=_create_replacement,
        decide=_decide_replacement,
    ),
    ToolSpec(
        name="update_customer_email",
        description=(
            "Change the contact email of the customer who opened this ticket. Always "
            "pauses for human approval."
        ),
        kind=ToolKind.WRITE,
        args_model=UpdateCustomerEmailArgs,
        handler=_update_email,
        decide=_decide_email,
    ),
    ToolSpec(
        name="update_ticket",
        description=(
            "Set the status of this ticket and record an internal note describing what "
            "was done and why."
        ),
        kind=ToolKind.WRITE,
        args_model=UpdateTicketArgs,
        handler=_update_ticket,
        decide=_always_allow,
    ),
    ToolSpec(
        name="escalate_to_human",
        description=(
            "Hand this ticket to a human agent. Use it when no policy covers the case, "
            "the customer asks for a person, or you cannot resolve the request safely."
        ),
        kind=ToolKind.WRITE,
        args_model=EscalateArgs,
        handler=_escalate,
        decide=_always_allow,
    ),
    ToolSpec(
        name="create_approval_request",
        description=(
            "Ask a human reviewer to approve a refund, replacement or account change that "
            "the policy does not clearly allow. The run pauses until they decide; if they "
            "approve, the action is carried out for you."
        ),
        kind=ToolKind.WRITE,
        args_model=ApprovalRequestArgs,
        handler=_never_runs,
        resolve=_resolve_approval_request,
    ),
]
