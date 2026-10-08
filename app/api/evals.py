import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import ContainerDep
from app.api.schemas import EvalRunRequest, EvalRunView
from app.clock import utcnow
from app.container import Container
from app.db.models import EvalRun, new_id
from evals.runner import run_suite
from evals.scenarios.catalog import select
from evals.scenarios.model import Scenario

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/eval", tags=["evaluation"])


def _view(row: EvalRun) -> EvalRunView:
    return EvalRunView(
        eval_run_id=row.id,
        status=row.status,
        model=row.model,
        prompt_version=row.prompt_version,
        summary=row.summary,
        error=row.error,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


async def _execute(container: Container, eval_run_id: str, scenarios: list[Scenario]) -> None:
    summary, error = None, None
    try:
        summary = (await run_suite(container, scenarios, concurrency=4)).summary
    except Exception as exc:
        # Background task boundary: store the failure where the caller can read it.
        logger.exception("Eval run %s failed", eval_run_id)
        error = f"{type(exc).__name__}: {exc}"
    async with container.session_factory.begin() as session:
        row = await session.get(EvalRun, eval_run_id)
        if row is not None:
            row.status = "failed" if error else "completed"
            row.summary, row.error, row.completed_at = summary, error, utcnow()


@router.post("/run", response_model=EvalRunView, status_code=status.HTTP_202_ACCEPTED)
async def start_eval(
    body: EvalRunRequest, request: Request, container: ContainerDep
) -> EvalRunView:
    """Start the scenario regression suite in the background. Poll the returned id."""
    scenarios = select(body.categories, body.limit)
    if not scenarios:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "No scenarios selected.")
    row = EvalRun(
        id=new_id("evl"), model=container.llm.model, prompt_version=container.prompt.version
    )
    async with container.session_factory.begin() as session:
        session.add(row)

    task = asyncio.create_task(_execute(container, row.id, scenarios))
    # Keep a reference so the task is not garbage collected mid-run.
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)
    return _view(row)


@router.get("/runs/{eval_run_id}", response_model=EvalRunView)
async def get_eval(eval_run_id: str, container: ContainerDep) -> EvalRunView:
    async with container.session_factory() as session:
        row = await session.get(EvalRun, eval_run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No eval run {eval_run_id}.")
    return _view(row)
