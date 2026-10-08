"""Model routing, quality scoring, queue priority and the OpenAI embedder. No database."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest

from app.agent.llm.factory import build_embedder, build_llm
from app.agent.llm.openai_client import OpenAILLM
from app.agent.llm.openai_embeddings import OpenAIEmbedder
from app.agent.llm.routing import RoutingLLM, is_simple
from app.agent.llm.scripted import ScriptedLLM, turn
from app.agent.state import initial_state
from app.approvals.priority import Priority, prioritise, priority_of
from app.config import Settings
from app.db.models import EMBEDDING_DIM, Approval
from app.observability.quality import score_run
from app.policies.embeddings import EmbeddingError, HashingEmbedder
from app.services.errors import TransientError
from app.tools.base import RunScope, ToolContext
from app.tools.read import SearchPolicyArgs
from app.tools.registry import default_registry

SETTINGS = Settings(_env_file=None)
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def ticket(text: str) -> str:
    """The opening message exactly as a run sends it to the model."""
    content: str = initial_state(RunScope("run", "cus", "tkt"), text)["messages"][0]["content"]
    return content


class TestRouting:
    @pytest.mark.parametrize(
        "text",
        [
            "Where is my order ORD-1001?",
            "What are your opening hours?",
            "How long does standard shipping take?",
        ],
    )
    def test_a_short_question_is_simple(self, text: str) -> None:
        assert is_simple(ticket(text), 400)

    @pytest.mark.parametrize(
        "text",
        [
            "My order arrived damaged. Can you refund it?",
            "I was charged twice.",
            "Please change the email on my account.",
            "My parcel never arrived.",
            "I want to speak to a manager.",
            "Where is my order? " * 40,
        ],
    )
    def test_anything_that_may_need_an_action_is_not(self, text: str) -> None:
        assert not is_simple(ticket(text), 400)

    async def test_each_ticket_goes_to_the_right_model(self) -> None:
        primary = ScriptedLLM(steps=[turn(text="primary")])
        light = ScriptedLLM(steps=[turn(text="light")])
        router = RoutingLLM(primary, light, max_chars=400)

        async def ask(text: str) -> str:
            history = [{"role": "user", "content": ticket(text)}]
            return (await router.complete(system="", messages=history, tools=[])).text

        assert await ask("Where is my order?") == "light"
        assert await ask("Refund my order please.") == "primary"

    def test_routing_is_on_only_when_a_light_model_is_set(self) -> None:
        assert isinstance(build_llm(SETTINGS), OpenAILLM)

        routed = build_llm(Settings(_env_file=None, llm_light_model="gpt-6-luna"))

        assert isinstance(routed, RoutingLLM)
        assert routed.model == "gpt-6.1-sol"
        simple = routed.choose([{"role": "user", "content": ticket("Where is my order?")}])
        assert simple.model == "gpt-6-luna"


def result(tool: str, ok: bool = True, error: str | None = None) -> dict[str, Any]:
    return {"tool": tool, "ok": ok, "arguments": {}, "result": {"error": error} if error else {}}


def finished(*results: dict[str, Any], forced: bool = False, reply: str = "Done.") -> Any:
    actions = [{"tool": "escalate_to_human", "forced": True}] if forced else []
    return {
        "tool_results": list(results),
        "actions": actions,
        "messages": [{"role": "assistant", "content": reply}],
    }


class TestQuality:
    def test_a_clean_run_scores_full_marks(self) -> None:
        scored = score_run(finished(result("calculate_refund"), result("issue_refund")))

        assert scored["score"] == 1.0
        assert all(scored["checks"].values())

    def test_a_write_before_any_policy_lookup_is_not_grounded(self) -> None:
        scored = score_run(finished(result("issue_refund"), result("search_policy")))

        assert scored["checks"]["grounded_in_policy"] is False
        assert scored["score"] == 0.8

    def test_refused_calls_and_outages_are_marked(self) -> None:
        scored = score_run(
            finished(
                result("issue_refund", ok=False, error="action_denied"),
                result("get_order", ok=False, error="tool_unavailable"),
            )
        )

        assert scored["checks"]["no_refused_calls"] is False
        assert scored["checks"]["no_tool_outage"] is False
        assert scored["checks"]["grounded_in_policy"] is True

    def test_a_forced_escalation_did_not_finish_unaided(self) -> None:
        scored = score_run(finished(forced=True, reply=""))

        assert scored["checks"]["finished_unaided"] is False
        assert scored["checks"]["replied"] is False


def approval(action: str, minutes_ago: int, **payload: Any) -> Approval:
    return Approval(
        id=f"apr_{action}_{minutes_ago}",
        action=action,
        payload=payload,
        context={},
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


class TestQueuePriority:
    def test_levels(self) -> None:
        assert priority_of(approval("issue_refund", 5, amount="120.00"), NOW, SETTINGS) == "normal"
        assert priority_of(approval("issue_refund", 5, amount="500.00"), NOW, SETTINGS) == "high"
        assert priority_of(approval("update_customer_email", 5), NOW, SETTINGS) == "high"
        assert priority_of(approval("issue_refund", 60, amount="120.00"), NOW, SETTINGS) == "urgent"

    def test_a_replacement_is_ranked_by_the_order_total(self) -> None:
        costly = approval("create_shipping_replacement", 5, order_id="ORD-1")
        costly.context = {"order": {"total": "899.00"}}

        assert priority_of(costly, NOW, SETTINGS) is Priority.HIGH

    def test_the_queue_is_most_pressing_first_then_longest_wait(self) -> None:
        newest = approval("issue_refund", 1, amount="120.00")
        older = approval("issue_refund", 30, amount="120.00")
        big = approval("issue_refund", 2, amount="900.00")
        overdue = approval("issue_refund", 90, amount="101.00")

        ordered = prioritise([newest, older, big, overdue], NOW, SETTINGS)

        assert [item.id for item, _ in ordered] == [overdue.id, big.id, older.id, newest.id]
        assert [level for _, level in ordered] == ["urgent", "high", "normal", "normal"]


def embedder_answering(handler: Any, sent: list[dict[str, Any]] | None = None) -> OpenAIEmbedder:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return handler(request)

    config = Settings(_env_file=None, openai_api_key="test-key", embedding_provider="openai")
    return OpenAIEmbedder(config, httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))


def embedding_body(vector: list[float]) -> dict[str, Any]:
    return {
        "object": "list",
        "model": "text-embedding-3-small",
        "data": [{"object": "embedding", "index": 0, "embedding": vector}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


class TestOpenAIEmbedder:
    async def test_it_asks_for_a_vector_that_fits_the_column(self) -> None:
        sent: list[dict[str, Any]] = []
        vector = [0.5] * EMBEDDING_DIM
        embedder = embedder_answering(
            lambda _: httpx2.Response(200, json=embedding_body(vector)), sent
        )

        assert await embedder.embed("damaged item refund") == vector
        assert sent[0]["model"] == "text-embedding-3-small"
        assert sent[0]["input"] == "damaged item refund"
        assert sent[0]["dimensions"] == EMBEDDING_DIM

    async def test_a_vector_of_the_wrong_size_is_rejected(self) -> None:
        embedder = embedder_answering(
            lambda _: httpx2.Response(200, json=embedding_body([0.1, 0.2]))
        )

        with pytest.raises(EmbeddingError, match="dimensions"):
            await embedder.embed("text")

    async def test_an_api_error_is_an_embedding_error(self) -> None:
        embedder = embedder_answering(
            lambda _: httpx2.Response(401, json={"error": {"message": "bad key"}})
        )

        with pytest.raises(EmbeddingError):
            await embedder.embed("text")

    def test_the_provider_is_chosen_by_settings(self) -> None:
        assert isinstance(build_embedder(SETTINGS), HashingEmbedder)
        chosen = build_embedder(Settings(_env_file=None, embedding_provider="openai"))
        assert isinstance(chosen, OpenAIEmbedder)

    async def test_policy_search_reports_an_embedding_outage_as_retryable(self) -> None:
        class Down:
            async def embed(self, text: str) -> list[float]:
                raise EmbeddingError("embedding service is down")

        spec = default_registry().get("search_policy")
        assert spec is not None
        # The embedder fails before the session is used, so none is needed.
        ctx = ToolContext(RunScope("run", "cus", "tkt"), None, SETTINGS, Down())  # type: ignore[arg-type]

        with pytest.raises(TransientError):
            await spec.handler(ctx, SearchPolicyArgs(query="refund window"))


def test_an_empty_setting_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("API_KEY", "REVIEWER_KEYS", "LLM_LIGHT_MODEL", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "")

    config = Settings(_env_file=None)

    # An empty API key would otherwise lock every caller out.
    assert config.api_key is None
    assert config.reviewer_keys == {}
    assert config.llm_light_model is None
    assert config.openai_api_key is None
