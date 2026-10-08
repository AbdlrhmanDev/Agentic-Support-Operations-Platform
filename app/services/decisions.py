"""The verdict a business rule gives on a proposed write."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    # Shown to the reviewer when the verdict is NEEDS_APPROVAL.
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "within automatic limits") -> "Decision":
        return cls(Verdict.ALLOW, reason)

    @classmethod
    def needs_approval(cls, reason: str, **context: Any) -> "Decision":
        return cls(Verdict.NEEDS_APPROVAL, reason, context)

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        return cls(Verdict.DENY, reason)
