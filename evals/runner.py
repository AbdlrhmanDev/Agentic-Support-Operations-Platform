"""Runs scenarios through the real agent stack and grades them."""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.container import Container
from app.db.models import AgentRun, ApprovalStatus, RunStatus
from app.observability.queries import load_trace
from app.observability.queries import summarize as summarize_trace
from evals.grading import ScenarioResult, grade, observe
from evals.metrics import summarize
from evals.scenarios.model import Scenario
from evals.seeding import SeededScenario, seed_scenario

# A run that keeps asking for approvals is stopped after this many decisions.
_MAX_DECISIONS = 5

BeforeRun = Callable[[SeededScenario], None]
OnResult = Callable[[ScenarioResult], None]


@dataclass(frozen=True)
class SuiteReport:
    model: str
    prompt_version: str
    results: list[ScenarioResult]
    summary: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_version": self.prompt_version,
            "summary": self.summary,
            "results": [result.to_json() for result in self.results],
        }


async def _decide_pending(container: Container, run: AgentRun, scenario: Scenario) -> AgentRun:
    """Answer approvals as the scenario's reviewer would, until the run stops pausing.

    A scenario with no reviewer decision does not expect an approval at all, so
    any that appears is rejected.
    """
    approve = scenario.reviewer_decision == "approve"
    for _ in range(_MAX_DECISIONS):
        if run.status != RunStatus.AWAITING_APPROVAL:
            break
        pending = [
            approval
            for approval in await container.approvals.list_approvals(ApprovalStatus.PENDING, 1000)
            if approval.run_id == run.id
        ]
        if not pending:
            break
        for approval in pending:
            await container.approvals.decide(
                approval.id, approve=approve, reviewer="eval-reviewer", note="Scenario decision."
            )
        run = await container.runs.resume(run.id)
    return run


async def run_scenario(
    container: Container, scenario: Scenario, before_run: BeforeRun | None = None
) -> ScenarioResult:
    async with container.session_factory.begin() as session:
        seeded = await seed_scenario(session, scenario)
    if before_run is not None:
        before_run(seeded)

    started = time.perf_counter()
    run = await container.runs.start(
        customer_email=seeded.customer_email,
        message=seeded.message,
        faults=dict(scenario.faults),
    )
    run = await _decide_pending(container, run, scenario)
    latency_ms = int((time.perf_counter() - started) * 1000)

    trace = summarize_trace(await load_trace(container.session_factory, run.id))
    async with container.session_factory() as session:
        seen = await observe(session, seeded, run, container.settings.auto_refund_threshold)
    return grade(seeded, run, seen, trace, latency_ms)


async def run_suite(
    container: Container,
    scenarios: list[Scenario],
    *,
    concurrency: int = 1,
    before_run: BeforeRun | None = None,
    on_result: OnResult | None = None,
) -> SuiteReport:
    limiter = asyncio.Semaphore(concurrency)

    async def one(scenario: Scenario) -> ScenarioResult:
        async with limiter:
            result = await run_scenario(container, scenario, before_run)
        if on_result is not None:
            on_result(result)
        return result

    results = list(await asyncio.gather(*(one(scenario) for scenario in scenarios)))
    return SuiteReport(
        model=container.llm.model,
        prompt_version=container.prompt.version,
        results=results,
        summary=summarize(results),
    )
