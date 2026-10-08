from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer
from app.services.authorization import require_approved
from app.services.decisions import Decision
from app.services.errors import ActionDenied, CustomerNotFound

UPDATE_EMAIL = "update_customer_email"


async def get_customer(session: AsyncSession, customer_id: str) -> Customer:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise CustomerNotFound(f"No customer with id {customer_id}.")
    return customer


async def get_customer_by_email(session: AsyncSession, email: str) -> Customer:
    customer = await session.scalar(select(Customer).where(Customer.email == email.lower()))
    if customer is None:
        raise CustomerNotFound(f"No customer with email {email}.")
    return customer


async def decide_email_update(session: AsyncSession, customer_id: str, new_email: str) -> Decision:
    customer = await get_customer(session, customer_id)
    new_email = new_email.lower()
    if new_email == customer.email:
        return Decision.deny("The new email is the same as the current one.")
    taken = await session.scalar(select(Customer.id).where(Customer.email == new_email))
    if taken is not None:
        return Decision.deny("That email address cannot be used.")
    return Decision.needs_approval(
        "Account changes always need human approval.",
        current_email=customer.email,
        new_email=new_email,
    )


async def update_email(
    session: AsyncSession,
    *,
    customer_id: str,
    new_email: str,
    run_id: str,
    approval_id: str | None,
) -> Customer:
    new_email = new_email.lower()
    customer = await session.get(Customer, customer_id, with_for_update=True)
    if customer is None:
        raise CustomerNotFound(f"No customer with id {customer_id}.")
    if customer.email == new_email:
        return customer  # already applied: a retry or a resumed run
    decision = await decide_email_update(session, customer_id, new_email)
    if decision.verdict == "deny":
        raise ActionDenied(decision.reason)
    await require_approved(
        session,
        approval_id,
        run_id=run_id,
        action=UPDATE_EMAIL,
        matches=lambda payload: str(payload.get("new_email", "")).lower() == new_email,
    )
    customer.email = new_email
    return customer
