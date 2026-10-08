"""A model stand-in that replays prepared responses. For tests and harness checks only."""

from collections.abc import Callable, Iterable
from typing import Any

from app.agent.llm.base import LLMError, LLMResponse, Message, ToolCall
from app.observability.cost import Usage

Step = LLMResponse | Exception
Policy = Callable[[list[Message]], Step]


def call(name: str, call_id: str | None = None, **arguments: Any) -> ToolCall:
    return ToolCall(call_id or f"call_{name}_{abs(hash(repr(arguments))) % 10**8}", name, arguments)


def turn(*tool_calls: ToolCall, text: str = "") -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(tool_calls), usage=Usage(100, 20))


class ScriptedLLM:
    """Returns each scripted step in order, or asks a policy function what to say next."""

    model = "scripted"

    def __init__(self, steps: list[Step] | None = None, policy: Policy | None = None) -> None:
        if (steps is None) == (policy is None):
            raise ValueError("Give either steps or a policy.")
        self._steps = list(steps or [])
        self._policy = policy
        self.calls: list[list[Message]] = []

    def extend(self, steps: Iterable[Step]) -> None:
        """Add steps once the test knows the ids they need to mention."""
        self._steps.extend(steps)

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.calls.append(list(messages))
        if self._policy is not None:
            step = self._policy(messages)
        elif self._steps:
            step = self._steps.pop(0)
        else:
            raise LLMError("The scripted model ran out of steps.")
        if isinstance(step, Exception):
            raise step
        return step
