"""Scores a finished run against fixed quality checks. Rules only: no model is asked."""

from collections.abc import Mapping
from typing import Any

RUBRIC_VERSION = 1

# Writes that move money, goods or account details.
_GUARDED_WRITES = frozenset(
    {"issue_refund", "create_shipping_replacement", "update_customer_email"}
)
# Reads that put the policy, or the rule that applies it, in front of the model.
_GROUNDING_READS = frozenset({"search_policy", "calculate_refund"})
# The gate refused the call: the model asked for something invalid or not allowed.
_REFUSALS = frozenset({"unknown_tool", "invalid_arguments", "action_denied"})


def score_run(state: Mapping[str, Any]) -> dict[str, Any]:
    """Check a finished run's state and return each check with the share that passed."""
    results: list[dict[str, Any]] = state.get("tool_results", [])
    errors = [
        result["result"].get("error")
        for result in results
        if not result["ok"] and isinstance(result.get("result"), dict)
    ]
    forced = any(action.get("forced") for action in state.get("actions", []))
    replies = [m for m in state.get("messages", []) if m.get("role") == "assistant"]

    grounded = True
    seen_policy = False
    for result in results:
        if not result["ok"]:
            continue
        if result["tool"] in _GROUNDING_READS:
            seen_policy = True
        elif result["tool"] in _GUARDED_WRITES and not seen_policy:
            grounded = False

    checks = {
        # The agent wrote the reply itself, rather than a fallback being sent.
        "replied": bool(replies and replies[-1].get("content")) and not forced,
        # The run was not taken away from the agent by a limit.
        "finished_unaided": not forced,
        # Every money, goods or account write came after a policy or eligibility lookup.
        "grounded_in_policy": grounded,
        # The gate never had to refuse a call.
        "no_refused_calls": not any(error in _REFUSALS for error in errors),
        # No tool stayed down through its retries.
        "no_tool_outage": "tool_unavailable" not in errors,
    }
    return {
        "rubric_version": RUBRIC_VERSION,
        "score": round(sum(checks.values()) / len(checks), 2),
        "checks": checks,
    }
