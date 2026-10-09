"""The HTTP API, driven through the real app with a scripted model."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from app.agent.llm.scripted import ScriptedLLM, call, turn
from app.config import Settings
from app.main import create_app
from app.policies.retrieval import ingest_policies
from evals.scenarios.model import Expected, OrderSeed, Scenario
from evals.seeding import SeededScenario, seed_scenario


class Api:
    def __init__(self, client: httpx.AsyncClient, llm: ScriptedLLM, seeded: SeededScenario) -> None:
        self.client = client
        self.llm = llm
        self.seeded = seeded
        self.order_id = seeded.own_order_ids[0]

    def script(self, *steps) -> None:  # type: ignore[no-untyped-def]
        self.llm.extend(steps)

    async def start(self, **extra) -> httpx.Response:  # type: ignore[no-untyped-def]
        body = {"customer_email": self.seeded.customer_email, "message": "Refund please."}
        return await self.client.post("/agent/runs", json={**body, **extra})


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[Api]:
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
            yield Api(client, llm, seeded)


async def test_health_and_console(api: Api) -> None:
    assert (await api.client.get("/health")).json() == {"status": "ok"}
    console = await api.client.get("/")
    assert console.status_code == 200 and "Support Ops Console" in console.text
    assert 'src="/static/console.js"' in console.text
    assert 'href="/static/console.css"' in console.text
    for path, content_type in (
        ("/static/console.js", "javascript"),
        ("/static/console.css", "text/css"),
    ):
        asset = await api.client.get(path)
        assert asset.status_code == 200
        assert content_type in asset.headers["content-type"]
    assert (await api.client.get("/static/missing.js")).status_code == 404
    assert (await api.client.get("/static/%2e%2e/config.py")).status_code == 404


async def test_run_pauses_for_approval_and_resumes_over_http(api: Api) -> None:
    api.script(
        turn(call("search_policy", query="damaged item refund")),
        turn(call("issue_refund", order_id=api.order_id, amount="349.00", reason="damaged_item")),
        turn(text="Your refund has been issued."),
    )

    started = await api.start()
    assert started.status_code == 201
    run = started.json()
    assert run["status"] == "awaiting_approval"
    assert run["trace"]["llm_calls"] == 2

    pending = (await api.client.get("/approvals", params={"status": "pending"})).json()
    approval = next(item for item in pending if item["run_id"] == run["run_id"])
    assert approval["payload"]["amount"] == "349.00"
    assert approval["context"]["order"]["order_id"] == api.order_id
    assert approval["policy_citation"]["title"]

    decided = await api.client.post(
        f"/approvals/{approval['approval_id']}/approve", json={"reviewer": "lead"}
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["approval"]["status"] == "approved"
    assert body["run"]["status"] == "completed"
    assert body["run"]["outcome"] == "refund_issued"

    again = await api.client.post(
        f"/approvals/{approval['approval_id']}/reject", json={"reviewer": "lead"}
    )
    assert again.status_code == 409
    assert again.json()["error"] == "approval_state_conflict"

    fetched = (await api.client.get(f"/agent/runs/{run['run_id']}")).json()
    assert fetched["final_response"] == "Your refund has been issued."
    assert fetched["workflow_path"][-1] == "finalize"

    trace = (await api.client.get(f"/traces/{run['run_id']}")).json()
    kinds = [event["kind"] for event in trace["events"]]
    assert {"llm_call", "tool_call", "guard", "approval"} <= set(kinds)
    assert trace["prompt_version"] == "support_agent_v2"
    names = [event["name"] for event in trace["events"] if event["kind"] == "approval"]
    assert names == ["approval_requested", "approval_approved"]
    assert api.seeded.customer_email not in str(trace), "emails must be masked in traces"


async def test_rejecting_over_http_leaves_no_refund(api: Api) -> None:
    api.script(
        turn(call("issue_refund", order_id=api.order_id, amount="349.00", reason="damaged_item")),
        turn(text="The refund was declined."),
    )
    run = (await api.start()).json()
    pending = (await api.client.get("/approvals", params={"status": "pending"})).json()
    approval = next(item for item in pending if item["run_id"] == run["run_id"])

    decided = await api.client.post(
        f"/approvals/{approval['approval_id']}/reject",
        json={"reviewer": "lead", "note": "No photo of the damage."},
    )

    assert decided.json()["run"]["outcome"] == "approval_rejected"
    assert decided.json()["run"]["actions"] == []


async def test_run_started_without_waiting_finishes_in_the_background(api: Api) -> None:
    api.script(turn(text="Happy to help."))

    started = await api.client.post(
        "/agent/runs",
        params={"wait": "false"},
        json={"customer_email": api.seeded.customer_email, "message": "Hello."},
    )

    assert started.status_code == 202
    run = started.json()
    assert run["status"] == "running"
    assert run["final_response"] is None

    for _ in range(100):
        fetched = (await api.client.get(f"/agent/runs/{run['run_id']}")).json()
        if fetched["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert fetched["status"] == "completed"
    assert fetched["final_response"] == "Happy to help."
    assert fetched["trace"]["llm_calls"] == 1


async def test_errors_are_mapped_to_status_codes(api: Api) -> None:
    unknown_customer = await api.client.post(
        "/agent/runs", json={"customer_email": "nobody@example.com", "message": "hi"}
    )
    assert unknown_customer.status_code == 404
    assert unknown_customer.json()["error"] == "customer_not_found"

    assert (await api.client.get("/agent/runs/run_missing")).status_code == 404
    assert (await api.client.get("/traces/run_missing")).status_code == 404
    assert (await api.client.get("/approvals/apr_missing")).status_code == 404
    missing = await api.client.post("/approvals/apr_missing/approve", json={"reviewer": "x"})
    assert missing.status_code == 404

    invalid = await api.client.post("/agent/runs", json={"customer_email": "x", "message": ""})
    assert invalid.status_code == 422
    extra = await api.start(is_admin=True)
    assert extra.status_code == 422


async def test_fault_injection_is_off_by_default(api: Api) -> None:
    response = await api.start(faults={"issue_refund": 3})
    assert response.status_code == 403


async def test_eval_endpoint_validates_its_selection(api: Api) -> None:
    response = await api.client.post("/eval/run", json={"categories": ["no_such_category"]})
    assert response.status_code == 422
    assert (await api.client.get("/eval/runs/evl_missing")).status_code == 404


async def test_api_key_is_required_when_configured(settings: Settings) -> None:
    protected = settings.model_copy(update={"api_key": SecretStr("s3cret")})
    app = create_app(protected, ScriptedLLM(steps=[]))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/static/console.js")).status_code == 200
            assert (await client.get("/static/console.css")).status_code == 200
            assert (await client.get("/agent/runs")).status_code == 401
            assert (await client.get("/approvals", headers={"X-API-Key": "no"})).status_code == 401
            assert (await client.post("/eval/run", json={})).status_code == 401
            allowed = await client.get("/agent/runs?limit=1", headers={"X-API-Key": "s3cret"})
            assert allowed.status_code == 200
