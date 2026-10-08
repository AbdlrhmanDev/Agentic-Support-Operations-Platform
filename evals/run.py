"""Command line entry point for the scenario suite.

uv run python -m evals.run                    # full suite against the configured model
uv run python -m evals.run --category damaged_item --limit 5
uv run python -m evals.run --oracle           # harness self-check, no model calls
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.container import build_container
from app.eventloop import run as run_async
from app.policies.retrieval import ingest_policies
from evals.grading import ScenarioResult
from evals.oracle import OracleLLM
from evals.runner import SuiteReport, run_suite
from evals.scenarios.catalog import categories, select

REPORTS_DIR = Path(__file__).parent / "reports"


def _print_result(result: ScenarioResult) -> None:
    mark = "PASS" if result.task_success else "FAIL"
    detail = "" if result.task_success else f"  {'; '.join(result.failures)}"
    print(f"{mark}  {result.scenario_id:<24}{detail}", flush=True)


def _print_summary(report: SuiteReport, oracle: bool) -> None:
    print()
    if oracle:
        print("ORACLE RUN: these numbers check the harness. They are not agent results.")
    print(f"model: {report.model}   prompt: {report.prompt_version}")
    for key, value in report.summary.items():
        if key not in ("by_category", "failed_scenarios"):
            print(f"  {key:<28}{value}")
    print("  by category:")
    for category, stats in report.summary["by_category"].items():
        print(f"    {category:<22}{stats['task_success_rate']}  ({stats['scenarios']} scenarios)")


async def main(args: argparse.Namespace) -> int:
    scenarios = select(args.category, args.limit)
    if not scenarios:
        print("No scenarios selected.", file=sys.stderr)
        return 2

    oracle = OracleLLM() if args.oracle else None
    container = await build_container(get_settings(), llm=oracle)
    try:
        async with container.session_factory.begin() as session:
            await ingest_policies(session, container.embedder)
        report = await run_suite(
            container,
            scenarios,
            concurrency=1 if oracle else args.concurrency,
            before_run=oracle.use if oracle else None,
            on_result=_print_result,
        )
    finally:
        await container.aclose()

    _print_summary(report, bool(oracle))
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{'oracle' if oracle else report.model}-{stamp}.json"
    path.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    print(f"\nreport: {path}")
    return 0 if report.summary["unsafe_action_rate"] == 0 else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the support-agent scenario suite.")
    parser.add_argument("--category", action="append", choices=categories())
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--oracle",
        action="store_true",
        help="Replay each scenario's reference path instead of calling a model.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(run_async(main(parse_args())))
