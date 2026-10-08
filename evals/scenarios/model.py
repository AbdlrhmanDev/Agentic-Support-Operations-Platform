"""What a scenario is: a seeded situation, a customer message, and the correct outcome."""

from dataclasses import dataclass, field
from typing import Any, Literal

# Placeholders, resolved once the scenario's data is seeded:
#   {order:KEY}   id of the seeded order with that key
#   {missing}     an order id that does not exist
#   {new_email}   a fresh email address


@dataclass(frozen=True)
class OrderSeed:
    key: str
    total: str
    status: Literal["placed", "shipped", "delivered", "cancelled"] = "delivered"
    delivered_days_ago: int | None = 5
    shipped_days_ago: int | None = None
    # What was actually charged. Defaults to `total`; higher means a duplicate charge.
    charged: str | None = None
    # An amount already refunded before the scenario starts.
    refunded: str | None = None
    item: str = "Wireless headphones"
    owner: Literal["customer", "other"] = "customer"


@dataclass(frozen=True)
class ExpectedCall:
    name: str
    # Arguments the agent's call must match.
    args: dict[str, Any] = field(default_factory=dict)
    # Further arguments the oracle needs to make a valid call. Not graded.
    oracle_args: dict[str, Any] = field(default_factory=dict)
    # Optional calls are part of the reference path but not required of the agent.
    required: bool = True


@dataclass(frozen=True)
class Expected:
    # The reference path, in order. Doubles as the oracle's script.
    calls: tuple[ExpectedCall, ...]
    forbidden_tools: tuple[str, ...] = ()
    # End state on the customer's own orders, after any reviewer decision.
    refund_total: str = "0.00"
    replacements: int = 0
    email_changed: bool = False
    # An approval was requested or the ticket was escalated.
    needs_human: bool = False
    escalated: bool = False
    status: Literal["completed", "escalated"] = "completed"
    reply_must_not_contain: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    message: str
    orders: tuple[OrderSeed, ...]
    expected: Expected
    faults: dict[str, int] = field(default_factory=dict)
    # How the reviewer answers if the run pauses for approval.
    reviewer_decision: Literal["approve", "reject"] | None = None
