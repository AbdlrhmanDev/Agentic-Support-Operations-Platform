"""Token usage and what it costs."""

import re
from dataclasses import dataclass
from decimal import Decimal

_PER_MILLION = Decimal(1_000_000)
# Providers may answer with a dated snapshot id, such as `model-2026-08-01`.
_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}-?\d{2}-?\d{2}$")


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


# First-party standard-tier list prices. Update when they change.
MODEL_PRICES: dict[str, ModelPrice] = {
    # OpenAI, checked 2026-10-08.
    "gpt-6-astra": ModelPrice(Decimal("10"), Decimal("50"), Decimal("1"), Decimal("12.50")),
    "gpt-6.1-sol": ModelPrice(Decimal("2"), Decimal("10"), Decimal("0.10"), Decimal("2.50")),
    "gpt-6-luna": ModelPrice(Decimal("0.10"), Decimal("0.50"), Decimal("0.01"), Decimal("0.125")),
    # Anthropic, checked 2026-09-25.
    "claude-opus-5-5": ModelPrice(Decimal("4"), Decimal("20"), Decimal("0.20"), Decimal("5")),
    "claude-sonnet-5-5": ModelPrice(Decimal("2"), Decimal("10"), Decimal("0.20"), Decimal("2.50")),
    "claude-haiku-4-5": ModelPrice(Decimal("1"), Decimal("5"), Decimal("0.10"), Decimal("1.25")),
}


def cost_usd(model: str, usage: Usage) -> Decimal | None:
    """Cost of one model call, or None when the model has no known price.

    Unknown is reported as unknown rather than as zero, so a missing price
    cannot make a run look free.
    """
    price = MODEL_PRICES.get(model) or MODEL_PRICES.get(_SNAPSHOT_SUFFIX.sub("", model))
    if price is None:
        return None
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_tokens * price.cache_read
        + usage.cache_write_tokens * price.cache_write
    ) / _PER_MILLION
    return total.quantize(Decimal("0.000001"))
