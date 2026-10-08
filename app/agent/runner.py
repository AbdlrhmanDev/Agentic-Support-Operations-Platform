"""Starts runs, resumes them after an approval, and keeps the run record current."""

import logging
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.llm.base import LLMClient
from app.agent.prompts import Prompt
from app.agent.state import RunState, initial_state
from app.clock import utcnow
from app.db.models import AgentRun, RunStatus, Ticket, TicketStatus, new_id
from app.db.session import SessionFactory
from app.observability.quality import score_run
from app.observability.tracing import span
from app.services import customers, tickets
from app.services.errors import RunNotFound
from app.tools.base import RunScope

logger = logging.getLogger(__name__)

# Each tool call takes up to three graph steps, so the default of 25 is too low.
_RECURSION_LIMIT = 200


class RunService:
    def __init__(
        self,
        graph: CompiledStateGraph[RunState, None, RunState, RunState],
        session_factory: SessionFactory,
        llm: LLMClient,
        prompt: Prompt,
    ) -> None:
        self._graph = graph
        self._session_factory = session_factory
        self._llm = llm
        self._prompt = prompt

    async def start(
        self,
        *,
        customer_email: str,
        message: str,
        ticket_id: str | None = None,
        faults: dict[str, int] | None = None,
    ) -> AgentRun:
        """Open a run for a customer message and drive it until it finishes or pauses."""
        run, state = await self.open(
            customer_email=customer_email, message=message, ticket_id=ticket_id, faults=faults
        )
        return await self.drive(run, state)

    async def open(
        self,
        *,
        customer_email: str,
        message: str,
        ticket_id: str | None = None,
        faults: dict[str, int] | None = None,
        channel: str = "api",
        external_id: str | None = None,
    ) -> tuple[AgentRun, RunState]:
        """Record a new run without starting it. Pass the result to `drive`."""
        async with self._session_factory.begin() as session:
            customer = await customers.get_customer_by_email(session, customer_email)
            ticket = (
                await tickets.get_owned_ticket(session, ticket_id, customer.id)
                if ticket_id
                else await tickets.create_ticket(
                    session, customer.id, channel=channel, external_id=external_id
                )
            )
            await tickets.append_message(session, ticket.id, customer.id, "customer", message)
            run = AgentRun(
                id=new_id("run"),
                ticket_id=ticket.id,
                customer_id=customer.id,
                model=self._llm.model,
                prompt_version=self._prompt.version,
            )
            session.add(run)

        scope = RunScope(run.id, run.customer_id, run.ticket_id)
        return run, initial_state(scope, message, faults)

    async def drive(self, run: AgentRun, state: RunState) -> AgentRun:
        """Run an opened run until it finishes or pauses. Never raises."""
        return await self._advance(RunScope(run.id, run.customer_id, run.ticket_id), state)

    async def intake(
        self, *, channel: str, external_id: str, customer_email: str, message: str
    ) -> tuple[AgentRun, RunState | None]:
        """Open a run for a message from an outside channel, once per external id.

        A message that was already taken in returns its run and no state:
        there is nothing left to drive.
        """
        existing = await self._find_external(channel, external_id)
        if existing is not None:
            return existing, None
        try:
            return await self.open(
                customer_email=customer_email,
                message=message,
                channel=channel,
                external_id=external_id,
            )
        except IntegrityError:
            # Lost a race with another delivery of the same message.
            raced = await self._find_external(channel, external_id)
            if raced is None:
                raise
            return raced, None

    async def _find_external(self, channel: str, external_id: str) -> AgentRun | None:
        async with self._session_factory() as session:
            found: AgentRun | None = await session.scalar(
                select(AgentRun)
                .join(Ticket, Ticket.id == AgentRun.ticket_id)
                .where(Ticket.channel == channel, Ticket.external_id == external_id)
                .order_by(AgentRun.started_at)
                .limit(1)
            )
            return found

    async def resume(self, run_id: str) -> AgentRun:
        """Continue a run that was waiting on an approval that is now decided."""
        run = await self.get(run_id)
        scope = RunScope(run.id, run.customer_id, run.ticket_id)
        return await self._advance(scope, Command(resume=True))

    async def get(self, run_id: str) -> AgentRun:
        async with self._session_factory() as session:
            run = await session.get(AgentRun, run_id)
        if run is None:
            raise RunNotFound(f"No run {run_id}.")
        return run

    async def list_runs(self, limit: int = 50) -> list[AgentRun]:
        async with self._session_factory() as session:
            result = await session.scalars(
                select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
            )
            return list(result.all())

    async def _advance(self, scope: RunScope, graph_input: RunState | Command[Any]) -> AgentRun:
        config: Any = {
            "configurable": {"thread_id": scope.run_id},
            "recursion_limit": _RECURSION_LIMIT,
        }
        try:
            with span("agent.run", **{"run.id": scope.run_id}):
                await self._graph.ainvoke(graph_input, config)
            snapshot = await self._graph.aget_state(config)
        except Exception as exc:
            # The run boundary: whatever broke, record it and hand the ticket to a person.
            logger.exception("Run %s failed", scope.run_id)
            return await self._fail(scope, exc)

        state: dict[str, Any] = dict(snapshot.values)
        paused = bool(snapshot.next)
        async with self._session_factory.begin() as session:
            run = await session.get(AgentRun, scope.run_id, with_for_update=True)
            assert run is not None
            run.state_json = state
            if paused:
                run.status = RunStatus.AWAITING_APPROVAL
            else:
                run.status = state["status"]
                run.outcome = state["outcome"]
                run.final_response = state["final_response"]
                run.quality = score_run(state)
                run.completed_at = utcnow()
        return run

    async def _fail(self, scope: RunScope, exc: Exception) -> AgentRun:
        async with self._session_factory.begin() as session:
            run = await session.get(AgentRun, scope.run_id, with_for_update=True)
            assert run is not None
            run.status = RunStatus.FAILED
            run.outcome = "failed"
            run.error = f"{type(exc).__name__}: {exc}"
            run.completed_at = utcnow()
            await tickets.update_ticket(
                session,
                ticket_id=scope.ticket_id,
                customer_id=scope.customer_id,
                status=TicketStatus.ESCALATED,
                note="Escalated automatically: the agent run failed.",
            )
        return run
