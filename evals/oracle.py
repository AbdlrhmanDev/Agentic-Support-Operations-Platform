"""A scripted model that follows each scenario's reference path.

Running the suite with the oracle checks the harness, not the agent: if the
reference path does not reach the expected outcome, the scenario or a grader
is wrong. Oracle results say nothing about how a real model performs.
"""

from typing import Any

from app.agent.llm.base import LLMResponse, Message, ToolCall
from app.agent.llm.scripted import ScriptedLLM, turn
from evals.seeding import SeededScenario


def oracle_llm(seeded: SeededScenario) -> ScriptedLLM:
    calls = [
        ToolCall(
            id=f"oracle_{index}_{call.name}",
            name=call.name,
            arguments=seeded.resolve({**call.args, **call.oracle_args}),
        )
        for index, call in enumerate(seeded.scenario.expected.calls)
    ]

    def policy(messages: list[Message]) -> LLMResponse:
        turns_taken = sum(1 for message in messages if message["role"] == "assistant")
        if turns_taken < len(calls):
            return turn(calls[turns_taken])
        return turn(text="Your request has been handled. Thank you for contacting us.")

    return ScriptedLLM(policy=policy)


class OracleLLM:
    """Delegates to the oracle of whichever scenario is running. One scenario at a time."""

    model = "oracle"

    def __init__(self) -> None:
        self._current: ScriptedLLM | None = None

    def use(self, seeded: SeededScenario) -> None:
        self._current = oracle_llm(seeded)

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        if self._current is None:
            raise RuntimeError("No scenario is active.")
        return await self._current.complete(system=system, messages=messages, tools=tools)
