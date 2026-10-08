"""Claude via the Anthropic SDK."""

from functools import cached_property
from typing import Any

import anthropic

from app.agent.llm.base import LLMError, LLMRefusal, LLMResponse, Message, ToolCall
from app.config import Settings
from app.observability.cost import Usage

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages to the Messages API shape.

    Assistant turns are replayed from the stored provider blocks so thinking
    blocks go back unchanged. All results for one assistant turn are sent in a
    single user message.
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "user":
            converted.append({"role": "user", "content": message["content"]})
        elif role == "assistant":
            blocks = message.get("provider_blocks") or _assistant_blocks(message)
            converted.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            result = {
                "type": "tool_result",
                "tool_use_id": message["tool_call_id"],
                "content": message["content"],
                "is_error": bool(message.get("is_error")),
            }
            previous = converted[-1] if converted else None
            if previous and previous["role"] == "user" and isinstance(previous["content"], list):
                previous["content"].append(result)
            else:
                converted.append({"role": "user", "content": [result]})
        else:
            raise ValueError(f"Unknown message role: {role}")
    return converted


def _assistant_blocks(message: Message) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls", []):
        blocks.append(
            {"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["arguments"]}
        )
    return blocks


class AnthropicLLM:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.model = settings.llm_model

    @cached_property
    def _client(self) -> anthropic.AsyncAnthropic:
        # Built on first use so the API can start, and serve reads, without credentials.
        return anthropic.AsyncAnthropic(timeout=self._settings.llm_timeout_seconds)

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._settings.llm_max_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages),
            "tools": tools,
            "output_config": {"effort": self._settings.llm_effort},
            # Caches the stable prefix: tools, system prompt and earlier turns.
            "cache_control": {"type": "ephemeral"},
        }
        try:
            if self._settings.llm_refusal_fallback:
                # A declined request is retried server-side on a fallback model.
                response: Any = await self._client.beta.messages.create(
                    betas=[_FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await self._client.messages.create(**request)
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the model API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Model API error {exc.status_code}: {exc.message}") from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(str(exc)) from exc
        except TypeError as exc:
            # How the SDK reports that it found no credentials to send.
            raise LLMError(f"Model client is not configured: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMRefusal("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("The model response was cut off at the token limit.")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(block.id, block.name, dict(block.input)))

        usage = response.usage
        return LLMResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
            ),
            model=response.model,
            provider_blocks=[block.to_dict() for block in response.content],
        )
