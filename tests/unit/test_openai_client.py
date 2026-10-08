"""The OpenAI client, driven through the real SDK against a fake HTTP transport."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from app.agent.llm.base import LLMError, LLMRefusal, Message
from app.agent.llm.factory import build_llm
from app.agent.llm.openai_client import OpenAILLM, to_openai_input
from app.config import Settings
from app.observability.cost import Usage, cost_usd
from app.tools.registry import default_registry

TOOLS = default_registry().llm_schemas()
REASONING = {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "opaque"}
FUNCTION_CALL = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "get_order",
    "arguments": '{"order_id": "ORD-1"}',
    "status": "completed",
}

Handler = Callable[[httpx2.Request], httpx2.Response]


def text_item(text: str) -> dict[str, Any]:
    part = {"type": "output_text", "text": text, "annotations": []}
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [part],
    }


def body(output: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6.1-sol-2026-08-01",
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
            "output_tokens": 50,
            "output_tokens_details": {"reasoning_tokens": 20},
            "total_tokens": 1050,
        },
        **overrides,
    }


def make_llm(handler: Handler, **settings: Any) -> OpenAILLM:
    config = Settings(_env_file=None, openai_api_key="test-key", **settings)
    return OpenAILLM(config, httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


def replying(payload: dict[str, Any], sent: list[dict[str, Any]] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return httpx2.Response(200, json=payload)

    return handler


async def complete(llm: OpenAILLM, messages: list[Message] | None = None) -> Any:
    history = messages or [{"role": "user", "content": "Where is my order?"}]
    return await llm.complete(system="Be helpful.", messages=history, tools=TOOLS)


async def test_tool_call_is_parsed_and_the_request_is_well_formed() -> None:
    sent: list[dict[str, Any]] = []
    llm = make_llm(replying(body([REASONING, FUNCTION_CALL]), sent), llm_effort="low")

    response = await complete(llm)

    assert [(c.id, c.name, c.arguments) for c in response.tool_calls] == [
        ("call_1", "get_order", {"order_id": "ORD-1"})
    ]
    assert response.text == ""
    assert response.model == "gpt-6.1-sol-2026-08-01"

    request = sent[0]
    assert request["model"] == "gpt-6.1-sol"
    assert request["instructions"] == "Be helpful."
    assert request["input"] == [{"role": "user", "content": "Where is my order?"}]
    assert request["reasoning"] == {"effort": "low"}
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert {tool["name"] for tool in request["tools"]} == set(default_registry().names())
    refund = next(tool for tool in request["tools"] if tool["name"] == "issue_refund")
    assert refund["type"] == "function"
    assert refund["strict"] is False
    assert refund["parameters"]["additionalProperties"] is False


async def test_usage_separates_cached_tokens_and_snapshot_ids_are_priced() -> None:
    response = await complete(make_llm(replying(body([text_item("Done.")]))))

    assert response.text == "Done."
    assert response.usage == Usage(
        input_tokens=300, output_tokens=50, cache_read_tokens=600, cache_write_tokens=100
    )
    assert response.usage.total_input_tokens == 1000
    # 300 * 2.00 + 50 * 10.00 + 600 * 0.10 + 100 * 2.50, per million tokens
    assert cost_usd(response.model, response.usage) == Decimal("0.001410")


async def test_the_next_request_replays_the_turn_and_answers_the_call() -> None:
    sent: list[dict[str, Any]] = []
    llm = make_llm(replying(body([REASONING, FUNCTION_CALL]), sent))
    history: list[Message] = [{"role": "user", "content": "Where is my order?"}]

    first = await complete(llm, history)
    history.append(first.to_message())
    history.append(
        {"role": "tool", "tool_call_id": "call_1", "name": "get_order", "content": '{"ok": true}'}
    )
    await complete(llm, history)

    replayed = sent[1]["input"]
    assert replayed[1]["type"] == "reasoning"
    assert replayed[1]["encrypted_content"] == "opaque"
    assert replayed[2]["type"] == "function_call"
    assert replayed[2]["call_id"] == "call_1"
    assert replayed[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok": true}',
    }


def test_a_turn_stored_by_another_provider_is_rebuilt() -> None:
    foreign: Message = {
        "role": "assistant",
        "content": "Checking.",
        "tool_calls": [{"id": "toolu_1", "name": "get_order", "arguments": {"order_id": "ORD-1"}}],
        "provider_blocks": [{"type": "tool_use", "id": "toolu_1", "name": "get_order"}],
    }

    assert to_openai_input([foreign]) == [
        {"role": "assistant", "content": "Checking."},
        {
            "type": "function_call",
            "call_id": "toolu_1",
            "name": "get_order",
            "arguments": '{"order_id": "ORD-1"}',
        },
    ]


async def test_a_refusal_is_reported_as_a_refusal() -> None:
    item = text_item("")
    item["content"] = [{"type": "refusal", "refusal": "I can't help with that."}]

    with pytest.raises(LLMRefusal):
        await complete(make_llm(replying(body([item]))))


@pytest.mark.parametrize(
    ("reason", "error"), [("max_output_tokens", LLMError), ("content_filter", LLMRefusal)]
)
async def test_an_incomplete_response_is_an_error(reason: str, error: type[Exception]) -> None:
    payload = body([], status="incomplete", incomplete_details={"reason": reason})

    with pytest.raises(error):
        await complete(make_llm(replying(payload)))


async def test_malformed_tool_arguments_are_an_error() -> None:
    broken = {**FUNCTION_CALL, "arguments": '{"order_id": '}

    with pytest.raises(LLMError, match="not valid JSON"):
        await complete(make_llm(replying(body([broken]))))


async def test_an_api_error_becomes_a_model_error() -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"message": "Incorrect API key provided."}})

    with pytest.raises(LLMError, match="401"):
        await complete(make_llm(handler))


async def test_a_missing_key_becomes_a_model_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm = OpenAILLM(Settings(_env_file=None, openai_api_key=None))

    with pytest.raises(LLMError, match="not configured"):
        await complete(llm)


def test_openai_is_the_default_provider() -> None:
    llm = build_llm(Settings(_env_file=None))

    assert isinstance(llm, OpenAILLM)
    assert llm.model == "gpt-6.1-sol"
