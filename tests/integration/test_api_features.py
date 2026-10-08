"""Reviewer identity, the prioritised queue, email intake and run quality, over HTTP."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from app.agent.llm.scripted import ScriptedLLM, call, turn
from app.config import Settings
from app.db.models import Refund, Ticket
from app.main import create_app
from app.policies.retrieval import ingest_policies
from evals.scenarios.model import Expected, OrderSeed, Scenario
from evals.seeding import SeededScenario, seed_scenario


class Served:
    def __init__(
        self, client: httpx.AsyncClient, llm: ScriptedLLM, seeded: SeededScenario, app: Any
    ) -> None:
        self.client = client
        self.llm = llm
        self.seeded = seeded
        self.container = app.state.container
        self.order_id = seeded.own_order_ids[0]

    async def start_large_refund(self, **request: Any) -> dict[str, Any]:
        self.llm.extend(
            [
                turn(
                    call(
                        "issue_refund",
                        order_id=self.order_id,
                        amount="349.00",
                        reason="damaged_item",
                    )
                ),
                turn(text="Your refund has been issued."),
            ]
        )
        body = {"customer_email": self.seeded.customer_email, "message": "Refund please."}
        response = await self.client.post("/agent/runs", json=body, **request)
        assert response.status_code == 201
        run: dict[str, Any] = response.json()
        return run

    async def pending_for(self, *run_ids: str, **request: Any) -> list[dict[str, Any]]:
        listed = await self.client.get("/approvals", params={"status": "pending"}, **request)
        return [item for item in listed.json() if item["run_id"] in run_ids]

    async def finished(self, run_id: str) -> dict[str, Any]:
        for _ in range(100):
            run: dict[str, Any] = (await self.client.get(f"/agent/runs/{run_id}")).json()
            if run["status"] != "running":
                return run
            await asyncio.sleep(0.05)
        raise AssertionError(f"Run {run_id} did not finish.")


@asynccontextmanager
async def serving(settings: Settings) -> AsyncIterator[Served]:
    llm = ScriptedLLM(steps=[])
    app = create_app(settings, llm)
    async with app.router.lifespan_context(app):
        container = app.state.container
        async with container.session_factory.begin() as session:
            await ingest_policies(session, container.embedder)
            seeded = await seed_scenario(
                session,
                Scenario("api", "api", "", (OrderSeed("o", "349.00"),), Expected(calls=())),
            )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield Served(client, llm, seeded, app)


async def test_a_reviewer_key_decides_who_the_reviewer_is(settings: Settings) -> None:
    protected = settings.model_copy(
        update={
            "api_key": SecretStr("shared"),
            "reviewer_keys": {"dana": SecretStr("key-dana"), "eli": SecretStr("key-eli")},
        }
    )
    shared, dana = {"X-API-Key": "shared"}, {"X-API-Key": "key-dana"}
    async with serving(protected) as api:
        run = await api.start_large_refund(headers=shared)
        (approval,) = await api.pending_for(run["run_id"], headers=dana)
        path = f"/approvals/{approval['approval_id']}/approve"

        no_key = await api.client.post(path, json={})
        assert no_key.status_code == 401
        with_shared_key = await api.client.post(path, json={"reviewer": "dana"}, headers=shared)
        assert with_shared_key.status_code == 403
        as_someone_else = await api.client.post(path, json={"reviewer": "eli"}, headers=dana)
        assert as_someone_else.status_code == 403

        async with api.container.session_factory() as session:
            paid = await session.scalars(select(Refund).where(Refund.order_id == api.order_id))
            assert paid.all() == [], "a refused decision must not release the refund"

        decided = await api.client.post(path, json={}, headers=dana)
        assert decided.status_code == 200
        assert decided.json()["approval"]["reviewer"] == "dana"
        assert decided.json()["run"]["outcome"] == "refund_issued"


async def test_without_reviewer_keys_a_reviewer_name_is_required(settings: Settings) -> None:
    async with serving(settings) as api:
        run = await api.start_large_refund()
        (approval,) = await api.pending_for(run["run_id"])
        path = f"/approvals/{approval['approval_id']}/reject"

        assert (await api.client.post(path, json={})).status_code == 422
        decided = await api.client.post(path, json={"reviewer": "lead"})
        assert decided.json()["approval"]["reviewer"] == "lead"


async def test_the_pending_queue_puts_account_changes_before_small_refunds(
    settings: Settings,
) -> None:
    async with serving(settings) as api:
        new_email = f"new-{uuid4().hex[:8]}@example.com"
        api.llm.extend(
            [
                turn(
                    call(
                        "issue_refund",
                        order_id=api.order_id,
                        amount="349.00",
                        reason="damaged_item",
                    )
                ),
                turn(call("update_customer_email", new_email=new_email)),
            ]
        )

        async def start(message: str) -> dict[str, Any]:
            body = {"customer_email": api.seeded.customer_email, "message": message}
            run: dict[str, Any] = (await api.client.post("/agent/runs", json=body)).json()
            assert run["status"] == "awaiting_approval"
            return run

        refund = await start("Refund please.")
        change = await start("Change my email.")

        queue = await api.pending_for(refund["run_id"], change["run_id"])

        assert [item["run_id"] for item in queue] == [change["run_id"], refund["run_id"]]
        assert [item["priority"] for item in queue] == ["high", "normal"]
        decided = (await api.client.get("/approvals")).json()
        assert all(item["priority"] is None for item in decided)


async def test_an_email_is_taken_in_once_however_often_it_is_delivered(
    settings: Settings,
) -> None:
    async with serving(settings) as api:
        api.llm.extend([turn(text="Your order is on its way.")])
        email = {
            "from_email": api.seeded.customer_email,
            "subject": "Where is my order?",
            "body": "I ordered last week and have heard nothing.",
            "message_id": f"<{uuid4().hex}@mail.example.com>",
        }

        first = await api.client.post("/intake/email", json=email)
        assert first.status_code == 202
        run = await api.finished(first.json()["run_id"])
        assert run["status"] == "completed"
        assert run["final_response"] == "Your order is on its way."
        assert run["quality"]["score"] == 1.0

        again = await api.client.post("/intake/email", json=email)
        assert again.status_code == 200
        assert again.json()["run_id"] == run["run_id"]
        assert len(api.llm.calls) == 1, "a redelivered email must not start a second run"

        async with api.container.session_factory() as session:
            ticket = await session.get(Ticket, run["ticket_id"])
        assert ticket is not None and ticket.channel == "email"
        assert ticket.messages[0]["text"].startswith("Subject: Where is my order?\n\n")
        sent_to_model = api.llm.calls[0][0]["content"]
        assert "<customer_message>\nSubject: Where is my order?" in sent_to_model


async def test_email_from_an_unknown_sender_is_not_taken_in(settings: Settings) -> None:
    async with serving(settings) as api:
        response = await api.client.post(
            "/intake/email",
            json={
                "from_email": f"stranger-{uuid4().hex[:8]}@example.com",
                "body": "Refund me.",
                "message_id": f"<{uuid4().hex}@mail.example.com>",
            },
        )
        assert response.status_code == 404
        assert response.json()["error"] == "customer_not_found"
