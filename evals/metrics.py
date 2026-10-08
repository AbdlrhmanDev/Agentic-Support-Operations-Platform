"""Turns per-scenario results into the suite metrics."""

import math
from decimal import Decimal
from typing import Any

from evals.grading import ScenarioResult


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def _percentile(values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(fraction * len(ordered)), 1) - 1]


def summarize(results: list[ScenarioResult]) -> dict[str, Any]:
    total = len(results)
    flagged = [r for r in results if r.actual_needs_human]
    should_flag = [r for r in results if r.expected_needs_human]
    correct_flags = sum(1 for r in flagged if r.expected_needs_human)
    costs = [r.cost_usd for r in results]
    known_costs = [cost for cost in costs if cost is not None]

    by_category: dict[str, dict[str, Any]] = {}
    for category in dict.fromkeys(r.category for r in results):
        group = [r for r in results if r.category == category]
        by_category[category] = {
            "scenarios": len(group),
            "task_success_rate": _rate(sum(r.task_success for r in group), len(group)),
            "unsafe_actions": sum(r.unsafe_action for r in group),
        }

    return {
        "scenarios": total,
        "task_success_rate": _rate(sum(r.task_success for r in results), total),
        "tool_selection_accuracy": _rate(sum(r.tool_selection_correct for r in results), total),
        "tool_argument_accuracy": _rate(sum(r.tool_arguments_correct for r in results), total),
        "policy_compliance_rate": _rate(sum(r.policy_compliant for r in results), total),
        "unsafe_action_rate": _rate(sum(r.unsafe_action for r in results), total),
        "escalation_precision": _rate(correct_flags, len(flagged)),
        "escalation_recall": _rate(correct_flags, len(should_flag)),
        "avg_tool_calls": round(sum(r.tool_calls for r in results) / total, 2) if total else None,
        "latency_ms_p50": _percentile([r.latency_ms for r in results], 0.50),
        "latency_ms_p95": _percentile([r.latency_ms for r in results], 0.95),
        # Reported only when every run had a known price.
        "avg_cost_usd": (
            str((sum(known_costs, Decimal(0)) / total).quantize(Decimal("0.000001")))
            if total and len(known_costs) == total
            else None
        ),
        "by_category": by_category,
        "failed_scenarios": [
            {"id": r.scenario_id, "failures": r.failures} for r in results if r.failures
        ],
    }
