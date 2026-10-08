"""Provider-neutral model interface. The only place the graph meets a model."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.observability.cost import Usage

# Conversation messages are plain dicts so they survive checkpointing as JSON:
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [...], "provider_blocks": [...]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}
# `provider_blocks` is the provider's own representation of the assistant turn.
# It is replayed untouched, because some providers reject an edited history.
Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider_blocks: list[dict[str, Any]] | None = None

    def to_message(self) -> Message:
        return {
            "role": "assistant",
            "content": self.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in self.tool_calls
            ],
            "provider_blocks": self.provider_blocks,
        }


class LLMError(Exception):
    """The model could not produce a usable response."""


class LLMRefusal(LLMError):
    """The model declined the request."""


class LLMClient(Protocol):
    model: str

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...
