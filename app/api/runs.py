from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import ContainerDep, run_in_background
from app.api.schemas import RunView, StartRunRequest, TraceEventView, TraceSummaryView, TraceView
from app.observability.queries import load_trace, summarize

router = APIRouter(tags=["agent"])


@router.post("/agent/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
async def start_run(
    body: StartRunRequest,
    request: Request,
    response: Response,
    container: ContainerDep,
    wait: bool = True,
) -> RunView:
    """Start a workflow for a customer message.

    Returns when it finishes or pauses. With `wait=false` it returns 202 at
    once and the run continues in the background; poll `GET /agent/runs/{id}`.
    """
    if body.faults and not container.settings.allow_fault_injection:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Fault injection is disabled.")
    run, state = await container.runs.open(
        customer_email=body.customer_email,
        message=body.message,
        ticket_id=body.ticket_id,
        faults=body.faults,
    )
    if not wait:
        run_in_background(request, container.runs.drive(run, state))
        response.status_code = status.HTTP_202_ACCEPTED
        return RunView.of(run)
    run = await container.runs.drive(run, state)
    events = await load_trace(container.session_factory, run.id)
    return RunView.of(run, summarize(events))


@router.get("/agent/runs", response_model=list[RunView])
async def list_runs(container: ContainerDep, limit: int = 50) -> list[RunView]:
    return [RunView.of(run) for run in await container.runs.list_runs(min(limit, 200))]


@router.get("/agent/runs/{run_id}", response_model=RunView)
async def get_run(run_id: str, container: ContainerDep) -> RunView:
    """Current state of a run with its trace totals."""
    run = await container.runs.get(run_id)
    events = await load_trace(container.session_factory, run.id)
    return RunView.of(run, summarize(events))


@router.get("/traces/{run_id}", response_model=TraceView)
async def get_trace(run_id: str, container: ContainerDep) -> TraceView:
    """Every model call, tool call and guard decision of a run, in order."""
    run = await container.runs.get(run_id)
    events = await load_trace(container.session_factory, run.id)
    return TraceView(
        run_id=run.id,
        status=run.status,
        model=run.model,
        prompt_version=run.prompt_version,
        workflow_path=(run.state_json or {}).get("workflow_path", []),
        summary=TraceSummaryView.of(summarize(events)),
        events=[TraceEventView.of(event) for event in events],
    )
