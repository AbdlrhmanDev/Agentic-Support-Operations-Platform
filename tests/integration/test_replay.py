"""Replaying a recorded run in a sandbox copy of its customer."""

from sqlalchemy import select

from app.agent.llm.scripted import call, turn
from app.db.models import Order, Refund
from evals.replay import replay_run
from evals.scenarios.model import OrderSeed


async def test_a_recorded_run_replays_to_the_same_outcome_without_touching_the_original(
    harness,  # type: ignore[no-untyped-def]
) -> None:
    h = await harness([], OrderSeed("o", "349.00"))
    h.llm.extend(
        [
            turn(call("calculate_refund", order_id=h.order_id, reason="damaged_item")),
            turn(call("issue_refund", order_id=h.order_id, amount="349.00", reason="damaged_item")),
            turn(text="Your refund has been issued."),
        ]
    )
    original = await h.start(f"Order {h.order_id} arrived damaged. Refund it please.")
    original = await h.decide(original, approve=True)
    assert original.outcome == "refund_issued"

    report = await replay_run(h.container, original.id, h.llm)

    assert report.mode == "recorded"
    assert report.same_outcome and report.same_tools
    assert report.replay["outcome"] == "refund_issued"
    assert report.replay["tools"] == ["calculate_refund", "issue_refund"]

    copy_id = report.replay["calls"][0]["arguments"]["order_id"]
    assert copy_id != h.order_id
    assert len(await h.refunds()) == 1, "the original order must not be refunded again"
    assert [refund.amount for refund in await h.refunds(copy_id)] == [349]

    async with h.container.session_factory() as session:
        copy = await session.get(Order, copy_id)
        replayed = await h.container.runs.get(report.replay_run_id)
        approved = await session.scalars(
            select(Refund.approval_id).where(Refund.order_id == copy_id)
        )
    assert copy is not None and copy.customer_id == replayed.customer_id
    assert copy.customer_id != original.customer_id
    assert all(approved.all()), "the replayed refund still went through an approval"
    assert f"Order {copy_id} arrived damaged" in replayed.state_json["messages"][0]["content"]


async def test_a_replay_follows_the_recorded_rejection(harness) -> None:  # type: ignore[no-untyped-def]
    h = await harness([], OrderSeed("o", "349.00"))
    h.llm.extend(
        [
            turn(call("issue_refund", order_id=h.order_id, amount="349.00", reason="damaged_item")),
            turn(text="The refund was declined."),
        ]
    )
    original = await h.decide(await h.start(), approve=False)

    report = await replay_run(h.container, original.id, h.llm)

    assert report.same_outcome
    assert report.replay["outcome"] == "approval_rejected"
    copy_id = report.replay["calls"][0]["arguments"]["order_id"]
    assert await h.refunds(copy_id) == []
