from fastapi import APIRouter, Request, Response, status

from app.api.deps import ContainerDep, run_in_background
from app.api.schemas import EmailIntakeRequest, RunView

router = APIRouter(prefix="/intake", tags=["intake"])


@router.post("/email", response_model=RunView, status_code=status.HTTP_202_ACCEPTED)
async def email_intake(
    body: EmailIntakeRequest, request: Request, response: Response, container: ContainerDep
) -> RunView:
    """Take in an inbound email and start a run for it in the background.

    Meant to be called by a mail gateway. Delivering the same Message-ID again
    returns the run that already exists, with 200, and starts nothing.
    """
    message = f"Subject: {body.subject}\n\n{body.body}" if body.subject else body.body
    run, state = await container.runs.intake(
        channel="email",
        external_id=body.message_id,
        customer_email=body.from_email,
        message=message,
    )
    if state is None:
        response.status_code = status.HTTP_200_OK
    else:
        run_in_background(request, container.runs.drive(run, state))
    return RunView.of(run)
