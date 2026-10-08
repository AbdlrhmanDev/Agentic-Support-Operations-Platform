"""OpenAI models via the Responses API."""

import json
from functools import cached_property
from typing import Any, cast

import httpx2
import openai
from openai.types.responses import ResponseInputParam, ToolParam

from app.agent.llm.base import LLMError, LLMRefusal, LLMResponse, Message, ToolCall
from app.config import Settings
from app.observability.cost import Usage

# Output item types this client stores and can send back as input.
_REPLAYABLE = frozenset({"message", "reasoning", "function_call"})


def to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the registry's tool schemas to Responses API function tools."""
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
            # Strict mode rejects schemas with optional fields. Arguments are
            # validated by the gate instead, which is the authority anyway.
            "strict": False,
        }
        for tool in tools
    ]


def to_openai_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages to Responses API input items.

    Assistant turns are replayed from the stored output items, so reasoning
    items go back unchanged next to the function calls they led to.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "user":
            items.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            items.extend(_assistant_items(message))
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message["content"],
                }
            )
        else:
            raise ValueError(f"Unknown message role: {role}")
    return items


def _assistant_items(message: Message) -> list[dict[str, Any]]:
    blocks = message.get("provider_blocks")
    # Blocks written by another provider cannot be replayed here; rebuild instead.
    if blocks and all(block.get("type") in _REPLAYABLE for block in blocks):
        return list(blocks)
    items: list[dict[str, Any]] = []
    if message.get("content"):
        items.append({"role": "assistant", "content": message["content"]})
    for call in message.get("tool_calls", []):
        items.append(
            {
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": json.dumps(call["arguments"]),
            }
        )
    return items


def _usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    details = usage.input_tokens_details
    cache_read = (details.cached_tokens if details else 0) or 0
    cache_write = (getattr(details, "cache_write_tokens", 0) if details else 0) or 0
    return Usage(
        # The API's input count includes cached tokens; ours does not.
        input_tokens=max(usage.input_tokens - cache_read - cache_write, 0),
        output_tokens=usage.output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


class OpenAILLM:
    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self.model = settings.llm_model

    @cached_property
    def _client(self) -> openai.AsyncOpenAI:
        # Built on first use so the API can start, and serve reads, without credentials.
        key = self._settings.openai_api_key
        return openai.AsyncOpenAI(
            api_key=key.get_secret_value() if key else None,
            timeout=self._settings.llm_timeout_seconds,
            http_client=self._http_client,
        )

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        try:
            response = await self._client.responses.create(
                model=self.model,
                instructions=system,
                # Both are built as plain dicts in the API's shape.
                input=cast(ResponseInputParam, to_openai_input(messages)),
                tools=cast(list[ToolParam], to_openai_tools(tools)),
                reasoning={"effort": self._settings.llm_effort},
                max_output_tokens=self._settings.llm_max_tokens,
                # Nothing is kept server-side: the checkpoint holds the history,
                # so reasoning comes back encrypted and is replayed from there.
                store=False,
                include=["reasoning.encrypted_content"],
            )
        except openai.APIConnectionError as exc:
            raise LLMError(f"Could not reach the model API: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"Model API error {exc.status_code}: {exc.message}") from exc
        except openai.OpenAIError as exc:
            # Includes a missing API key, which the SDK reports when the client is built.
            raise LLMError(f"Model client is not configured: {exc}") from exc

        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else None
            if reason == "content_filter":
                raise LLMRefusal("The model declined this request.")
            raise LLMError(f"The model response was incomplete: {reason or 'unknown reason'}.")
        if response.status == "failed" or response.error is not None:
            detail = response.error.message if response.error else "no detail given"
            raise LLMError(f"The model failed to respond: {detail}")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in response.output:
            if item.type == "message":
                for part in item.content:
                    if part.type == "refusal":
                        raise LLMRefusal("The model declined this request.")
                    if part.type == "output_text":
                        text_parts.append(part.text)
            elif item.type == "function_call":
                tool_calls.append(ToolCall(item.call_id, item.name, _arguments(item.arguments)))

        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=_usage(response.usage),
            model=response.model,
            provider_blocks=[
                item.to_dict(mode="json") for item in response.output if item.type in _REPLAYABLE
            ],
        )


def _arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise LLMError(f"The model sent tool arguments that are not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("The model sent tool arguments that are not a JSON object.")
    return parsed
