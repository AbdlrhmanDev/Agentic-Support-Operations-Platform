from fastapi import APIRouter, HTTPException, status

from app.api.deps import Caller, CallerDep, ContainerDep
from app.api.schemas import ApprovalView, DecisionRequest, DecisionResponse, RunView
from app.approvals.priority import prioritise
from app.clock import utcnow
from app.config import Settings
from app.db.models import ApprovalStatus

router = APIRouter(prefix="/approvals", tags=["approvals"])

# Enough to rank the whole pending queue rather than its newest page.
_QUEUE_LIMIT = 500


def _reviewer(body: DecisionRequest, caller: Caller, settings: Settings) -> str:
    """Who is deciding. With reviewer keys configured, the key says who, not the request."""
    if settings.reviewer_keys:
        if caller.reviewer is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Deciding an approval needs a reviewer's own key."
            )
        if body.reviewer is not None and body.reviewer != caller.reviewer:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "The reviewer named does not own this key."
            )
        return caller.reviewer
    if body.reviewer is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A reviewer name is required.")
    return body.reviewer


async def _decide(
    approval_id: str,
    body: DecisionRequest,
    container: ContainerDep,
    caller: Caller,
    *,
    approve: bool,
) -> DecisionResponse:
    reviewer = _reviewer(body, caller, container.settings)
    approval = await container.approvals.decide(
        approval_id, approve=approve, reviewer=reviewer, note=body.note
    )
    run = await container.runs.resume(approval.run_id)
    return DecisionResponse(approval=ApprovalView.of(approval), run=RunView.of(run))


@router.get("", response_model=list[ApprovalView])
async def list_approvals(
    container: ContainerDep, status: ApprovalStatus | None = None
) -> list[ApprovalView]:
    """Approval requests, newest first. The pending queue is ordered by priority instead."""
    if status is ApprovalStatus.PENDING:
        pending = await container.approvals.list_approvals(status, _QUEUE_LIMIT)
        return [
            ApprovalView.of(approval, priority)
            for approval, priority in prioritise(pending, utcnow(), container.settings)
        ]
    found = await container.approvals.list_approvals(status)
    return [ApprovalView.of(approval) for approval in found]


@router.get("/{approval_id}", response_model=ApprovalView)
async def get_approval(approval_id: str, container: ContainerDep) -> ApprovalView:
    return ApprovalView.of(await container.approvals.get(approval_id))


@router.post("/{approval_id}/approve", response_model=DecisionResponse)
async def approve(
    approval_id: str, body: DecisionRequest, container: ContainerDep, caller: CallerDep
) -> DecisionResponse:
    """Approve a pending action. The run resumes and carries it out."""
    return await _decide(approval_id, body, container, caller, approve=True)


@router.post("/{approval_id}/reject", response_model=DecisionResponse)
async def reject(
    approval_id: str, body: DecisionRequest, container: ContainerDep, caller: CallerDep
) -> DecisionResponse:
    """Reject a pending action. The run resumes and the agent handles the refusal."""
    return await _decide(approval_id, body, container, caller, approve=False)
