import asyncio
import secrets
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from pydantic import SecretStr

from app.container import Container

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


@dataclass(frozen=True)
class Caller:
    # The reviewer who owns the presented key. None for the shared key or an open API.
    reviewer: str | None = None


def _matches(presented: str, expected: SecretStr) -> bool:
    return secrets.compare_digest(presented.encode(), expected.get_secret_value().encode())


def authenticate(
    container: ContainerDep, presented: Annotated[str | None, Depends(_api_key_header)]
) -> Caller:
    """Identify the caller by API key. Open when no key of either kind is configured."""
    settings = container.settings
    if presented is not None:
        for name, key in settings.reviewer_keys.items():
            if _matches(presented, key):
                return Caller(reviewer=name)
        if settings.api_key is not None and _matches(presented, settings.api_key):
            return Caller()
    if settings.api_key is None and not settings.reviewer_keys:
        return Caller()
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid API key.")


CallerDep = Annotated[Caller, Depends(authenticate)]


def run_in_background(request: Request, work: Coroutine[Any, Any, object]) -> None:
    """Start work that outlives the request."""
    task = asyncio.create_task(work)
    # Keep a reference so the task is not garbage collected mid-run.
    request.app.state.background_tasks.add(task)
    task.add_done_callback(request.app.state.background_tasks.discard)
