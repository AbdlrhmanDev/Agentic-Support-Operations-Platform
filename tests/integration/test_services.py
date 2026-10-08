"""Business services against a real database: idempotency, limits and ownership."""

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.agent.llm.scripted import ScriptedLLM
from app.container import Container
from app.db.models import AgentRun, Refund, Replacement, new_id
from app.policies.retrieval import search_policies
from app.services import refunds, replacements, tickets
from app.services.decisions import Verdict
from app.services.errors import ActionDenied, ApprovalRequired, OrderNotFound
from app.services.refunds import RefundReason
from app.services.replacements import ReplacementReason
from evals.scenarios.model import OrderSeed
from evals.seeding import SeededScenario
from tests.conftest import ContainerFactory, Seeder


@pytest.fixture
async def container(make_container: ContainerFactory) -> Container:
    return await make_container(ScriptedLLM(steps=[]))


@pytest.fixture
def key() -> str:
    """An idempotency key no earlier test run has used. The database outlives a run."""
    return uuid4().hex


async def _run_id(container: Container, seeded: SeededScenario) -> str:
    """A run row, so refunds can reference it."""
    async with container.session_factory.begin() as session:
        ticket = await tickets.create_ticket(session, seeded.customer_id)
        run = AgentRun(
            id=new_id("run"),
            ticket_id=ticket.id,
            customer_id=seeded.customer_id,
            model="test",
            prompt_version="test",
        )
        session.add(run)
    return run.id


async def _issue(
    container: Container,
    seeded: SeededScenario,
    run_id: str,
    amount: str,
    key: str,
    *,
    customer_id: str | None = None,
    reason: RefundReason = RefundReason.DAMAGED_ITEM,
) -> tuple[Refund, bool]:
    async with container.session_factory.begin() as session:
        return await refunds.issue_refund(
            session,
            order_id=seeded.own_order_ids[0],
            customer_id=customer_id or seeded.customer_id,
            amount=Decimal(amount),
            reason=reason,
            idempotency_key=key,
            run_id=run_id,
            approval_id=None,
            settings=container.settings,
        )


async def _refund_count(container: Container, order_id: str) -> int:
    async with container.session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(Refund).where(Refund.order_id == order_id)
        )
    return int(count or 0)


async def test_same_idempotency_key_creates_one_refund(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00"))
    run_id = await _run_id(container, seeded)

    first, created_first = await _issue(container, seeded, run_id, "60.00", key)
    second, created_second = await _issue(container, seeded, run_id, "60.00", key)

    assert created_first and not created_second
    assert first.id == second.id
    assert await _refund_count(container, seeded.own_order_ids[0]) == 1


async def test_idempotency_key_cannot_be_reused_for_a_different_refund(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00"))
    run_id = await _run_id(container, seeded)
    await _issue(container, seeded, run_id, "20.00", key)

    with pytest.raises(ActionDenied, match="idempotency key"):
        await _issue(container, seeded, run_id, "30.00", key)
    assert await _refund_count(container, seeded.own_order_ids[0]) == 1


async def test_concurrent_refunds_cannot_exceed_the_balance(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00"))
    run_id = await _run_id(container, seeded)

    results = await asyncio.gather(
        *(_issue(container, seeded, run_id, "60.00", f"{key}-{i}") for i in range(5)),
        return_exceptions=True,
    )

    assert sum(1 for r in results if not isinstance(r, Exception)) == 1
    assert all(isinstance(r, ActionDenied) for r in results if isinstance(r, Exception))
    assert await _refund_count(container, seeded.own_order_ids[0]) == 1


async def test_refund_above_the_automatic_limit_needs_an_approval(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "400.00"))
    run_id = await _run_id(container, seeded)

    with pytest.raises(ApprovalRequired):
        await _issue(container, seeded, run_id, "400.00", key)
    assert await _refund_count(container, seeded.own_order_ids[0]) == 0


async def test_refund_outside_policy_is_denied(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00", delivered_days_ago=90))
    run_id = await _run_id(container, seeded)

    with pytest.raises(ActionDenied, match="refund window"):
        await _issue(container, seeded, run_id, "60.00", key)


async def test_another_customers_order_looks_like_a_missing_order(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00"))
    run_id = await _run_id(container, seeded)

    with pytest.raises(OrderNotFound):
        await _issue(container, seeded, run_id, "60.00", key, customer_id=seeded.other_customer_id)
    assert await _refund_count(container, seeded.own_order_ids[0]) == 0


async def test_replacement_is_created_once_and_blocks_a_second(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00"))
    run_id = await _run_id(container, seeded)

    async def create(idempotency_key: str) -> tuple[Replacement, bool]:
        async with container.session_factory.begin() as session:
            return await replacements.create_replacement(
                session,
                order_id=seeded.own_order_ids[0],
                customer_id=seeded.customer_id,
                reason=ReplacementReason.DAMAGED_ITEM,
                idempotency_key=idempotency_key,
                run_id=run_id,
                approval_id=None,
                settings=container.settings,
            )

    first, created = await create(key)
    again, created_again = await create(key)
    assert created and not created_again and first.id == again.id
    with pytest.raises(ActionDenied, match="already been created"):
        await create(f"{key}-2")


async def test_refunded_order_cannot_also_be_replaced(
    container: Container, seed: Seeder, key: str
) -> None:
    seeded = await seed(container, OrderSeed("o", "60.00"))
    run_id = await _run_id(container, seeded)
    await _issue(container, seeded, run_id, "60.00", key)

    async with container.session_factory() as session:
        decision = await replacements.decide_replacement_request(
            session,
            order_id=seeded.own_order_ids[0],
            customer_id=seeded.customer_id,
            reason=ReplacementReason.DAMAGED_ITEM,
            settings=container.settings,
        )
    assert decision.verdict is Verdict.DENY


@pytest.mark.parametrize(
    ("query", "policy_id", "section"),
    [
        ("my item arrived damaged and broken", "refunds", "Damaged or defective items"),
        ("customer was charged twice for the same order", "billing", "Duplicate charges"),
        ("shipment lost after many days in transit", "shipping", "Lost shipments"),
        ("change the email address on the account", "account", "Account changes"),
        ("how many days to request a refund", "refunds", "Refund window"),
    ],
)
async def test_policy_search_finds_the_relevant_section(
    container: Container, query: str, policy_id: str, section: str
) -> None:
    async with container.session_factory() as session:
        hits = await search_policies(session, container.embedder, query, top_k=3)

    assert hits[0].policy_id == policy_id
    assert hits[0].section == section
    assert hits[0].score >= hits[-1].score
