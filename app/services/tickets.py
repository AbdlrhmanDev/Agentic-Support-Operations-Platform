from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.db.models import Ticket, TicketStatus, new_id
from app.services import orders
from app.services.errors import TicketNotFound

MessageRole = Literal["customer", "agent", "note"]


async def create_ticket(
    session: AsyncSession, customer_id: str, *, channel: str = "api", external_id: str | None = None
) -> Ticket:
    ticket = Ticket(
        id=new_id("tkt"),
        customer_id=customer_id,
        messages=[],
        channel=channel,
        external_id=external_id,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def get_owned_ticket(
    session: AsyncSession, ticket_id: str, customer_id: str, *, for_update: bool = False
) -> Ticket:
    ticket = await session.get(Ticket, ticket_id, with_for_update=for_update)
    if ticket is None or ticket.customer_id != customer_id:
        raise TicketNotFound(f"No ticket {ticket_id} on this customer's account.")
    return ticket


def _message(role: MessageRole, text: str) -> dict[str, Any]:
    return {"role": role, "text": text, "at": utcnow().isoformat()}


async def append_message(
    session: AsyncSession, ticket_id: str, customer_id: str, role: MessageRole, text: str
) -> Ticket:
    ticket = await get_owned_ticket(session, ticket_id, customer_id, for_update=True)
    # Reassign rather than mutate: SQLAlchemy does not track in-place JSON edits.
    ticket.messages = [*ticket.messages, _message(role, text)]
    return ticket


async def update_ticket(
    session: AsyncSession,
    *,
    ticket_id: str,
    customer_id: str,
    status: TicketStatus,
    note: str,
    order_id: str | None = None,
) -> Ticket:
    """Set the ticket status and add a note. Repeating the same update changes nothing.

    An escalated ticket keeps that status: only a person takes it back. A
    linked order must be one of the customer's own.
    """
    ticket = await get_owned_ticket(session, ticket_id, customer_id, for_update=True)
    if order_id is not None:
        await orders.get_owned_order(session, order_id, customer_id)
    if ticket.status == TicketStatus.ESCALATED:
        status = TicketStatus.ESCALATED
    last = ticket.messages[-1] if ticket.messages else None
    repeated = (
        ticket.status == status
        and last is not None
        and last.get("role") == "note"
        and last.get("text") == note
    )
    if not repeated:
        ticket.status = status
        ticket.messages = [*ticket.messages, _message("note", note)]
    if order_id is not None:
        ticket.order_id = order_id
    return ticket
