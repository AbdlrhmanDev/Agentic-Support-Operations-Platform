"""Sends simple tickets to a lighter model. The choice is made by code, not by a model."""

import re
from typing import Any

from app.agent.llm.base import LLMClient, LLMResponse, Message

_CUSTOMER_TEXT = re.compile(r"<customer_message>\n(.*)\n</customer_message>", re.DOTALL)
# Words that suggest money, goods or the account may change hands. A ticket
# using any of them goes to the primary model.
_ACTION_WORDS = re.compile(
    r"\b(refund|return|charg|bill|paid|pay|money|credit|compensat|replac|exchang|damag|broke"
    r"|defect|faulty|wrong|lost|missing|never (arrived|received)|cancel|email|account|address"
    r"|password|manager|supervisor|human|complain|legal|lawyer|fraud|chargeback)",
    re.IGNORECASE,
)


def is_simple(ticket: str, max_chars: int) -> bool:
    """True when a ticket is short and asks for nothing that could need an action."""
    match = _CUSTOMER_TEXT.search(ticket)
    text = match.group(1) if match else ticket
    return len(text) <= max_chars and _ACTION_WORDS.search(text) is None


class RoutingLLM:
    """Routes a run by its opening ticket, so every turn of a run uses one model."""

    def __init__(self, primary: LLMClient, light: LLMClient, *, max_chars: int) -> None:
        self._primary = primary
        self._light = light
        self._max_chars = max_chars
        # The run record names the primary; each trace event names the model that answered.
        self.model = primary.model

    def choose(self, messages: list[Message]) -> LLMClient:
        opening = messages[0]["content"] if messages else ""
        simple = isinstance(opening, str) and is_simple(opening, self._max_chars)
        return self._light if simple else self._primary

    async def complete(
        self, *, system: str, messages: list[Message], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        return await self.choose(messages).complete(system=system, messages=messages, tools=tools)
