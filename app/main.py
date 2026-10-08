import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.agent.llm.base import LLMClient
from app.api import approvals, evals, intake, runs
from app.api.deps import authenticate
from app.config import Settings, get_settings
from app.container import build_container
from app.observability.tracing import configure_tracing
from app.services.errors import ApprovalStateError, DomainError, NotFoundError

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(level=logging.INFO)
        configure_tracing(settings)
        app.state.container = await build_container(settings, llm)
        app.state.background_tasks = set()
        try:
            yield
        finally:
            await app.state.container.aclose()

    app = FastAPI(title="Agentic Support Operations Platform", version="0.1.0", lifespan=lifespan)
    protected = [Depends(authenticate)]
    app.include_router(runs.router, dependencies=protected)
    app.include_router(intake.router, dependencies=protected)
    app.include_router(approvals.router, dependencies=protected)
    app.include_router(evals.router, dependencies=protected)

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            code = 404
        elif isinstance(exc, ApprovalStateError):
            code = 409
        else:
            code = 422
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=code)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
