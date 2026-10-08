"""Runs a tool with a transaction, a timeout, bounded retries and a trace record."""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import DBAPIError, IntegrityError

from app.config import Settings
from app.db.session import SessionFactory
from app.observability.recorder import EventKind, TraceRecorder
from app.observability.tracing import span
from app.policies.embeddings import Embedder
from app.services.decisions import Decision
from app.services.errors import DomainError, TransientError
from app.tools.base import RunScope, ToolArgs, ToolContext, ToolSpec

# Worth another attempt. An IntegrityError here is two calls racing on one
# idempotency key; the retry finds the row the other call wrote.
_RETRYABLE = (TransientError, TimeoutError, DBAPIError, IntegrityError)


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    payload: dict[str, Any]
    attempts: int
    # True when every attempt hit a transient failure, as opposed to a
    # business rule saying no.
    unavailable: bool = False


class ToolExecutor:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        embedder: Embedder,
        recorder: TraceRecorder,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._embedder = embedder
        self._recorder = recorder

    def _context(self, scope: RunScope, session: Any, approval_id: str | None) -> ToolContext:
        return ToolContext(scope, session, self._settings, self._embedder, approval_id)

    async def decide(
        self, spec: ToolSpec, scope: RunScope, args: ToolArgs, *, review_requested: bool
    ) -> Decision:
        """Ask the tool's business rule whether this write may run."""
        if spec.decide is None:
            raise ValueError(f"{spec.name} has no decision rule.")
        async with self._session_factory() as session:
            try:
                return await spec.decide(
                    self._context(scope, session, None), args, review_requested
                )
            except DomainError as exc:
                return Decision.deny(exc.message)

    async def execute(
        self,
        spec: ToolSpec,
        scope: RunScope,
        args: ToolArgs,
        *,
        approval_id: str | None = None,
        injected_failures: int = 0,
    ) -> ToolOutcome:
        """Run the tool. `injected_failures` makes the first N attempts fail, for tests."""
        started = time.perf_counter()
        attempts = 0
        outcome: ToolOutcome | None = None
        last_error = "unknown"

        with span(f"tool.{spec.name}", **{"tool.kind": spec.kind.value, "run.id": scope.run_id}):
            for attempt in range(1, self._settings.tool_max_retries + 2):
                attempts = attempt
                try:
                    if attempt <= injected_failures:
                        raise TransientError("injected fault")
                    async with asyncio.timeout(self._settings.tool_timeout_seconds):
                        async with self._session_factory.begin() as session:
                            payload = await spec.handler(
                                self._context(scope, session, approval_id), args
                            )
                    outcome = ToolOutcome(True, payload, attempts)
                    break
                except DomainError as exc:
                    outcome = ToolOutcome(
                        False, {"error": exc.code, "message": exc.message}, attempts
                    )
                    break
                except _RETRYABLE as exc:
                    last_error = type(exc).__name__
                    if attempt <= self._settings.tool_max_retries:
                        backoff = self._settings.tool_retry_backoff_seconds * 2 ** (attempt - 1)
                        await asyncio.sleep(backoff)

        if outcome is None:
            outcome = ToolOutcome(
                False,
                {
                    "error": "tool_unavailable",
                    "message": f"{spec.name} is not responding. It failed {attempts} times.",
                },
                attempts,
                unavailable=True,
            )

        await self._recorder.record(
            scope.run_id,
            EventKind.TOOL_CALL,
            spec.name,
            input=args.model_dump(mode="json"),
            output=outcome.payload,
            error=None if outcome.ok else (outcome.payload.get("error") or last_error),
            attempts=attempts,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return outcome
