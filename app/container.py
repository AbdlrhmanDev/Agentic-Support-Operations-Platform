"""Builds the application's long-lived objects once and wires them together."""

from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.graph import AgentDeps, build_graph
from app.agent.llm.base import LLMClient
from app.agent.llm.factory import build_embedder, build_llm
from app.agent.prompts import Prompt, load_prompt
from app.agent.runner import RunService
from app.approvals.service import ApprovalService
from app.config import Settings
from app.db.session import SessionFactory, create_engine, create_session_factory
from app.observability.recorder import TraceRecorder
from app.policies.embeddings import Embedder
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry, default_registry


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: SessionFactory
    checkpoint_pool: AsyncConnectionPool
    embedder: Embedder
    llm: LLMClient
    prompt: Prompt
    registry: ToolRegistry
    recorder: TraceRecorder
    approvals: ApprovalService
    runs: RunService

    async def aclose(self) -> None:
        await self.checkpoint_pool.close()
        await self.engine.dispose()


async def build_container(settings: Settings, llm: LLMClient | None = None) -> Container:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    # The checkpointer needs autocommit connections that return dict rows.
    pool = AsyncConnectionPool(
        settings.psycopg_dsn,
        max_size=10,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open()
    checkpointer = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await checkpointer.setup()

    llm = llm or build_llm(settings)
    prompt = load_prompt()
    embedder = build_embedder(settings)
    registry = default_registry()
    recorder = TraceRecorder(session_factory)
    approvals = ApprovalService(session_factory, recorder)
    executor = ToolExecutor(session_factory, settings, embedder, recorder)
    graph = build_graph(
        AgentDeps(
            settings=settings,
            llm=llm,
            prompt=prompt,
            registry=registry,
            executor=executor,
            approvals=approvals,
            recorder=recorder,
            session_factory=session_factory,
        ),
        checkpointer,
    )
    return Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        checkpoint_pool=pool,
        embedder=embedder,
        llm=llm,
        prompt=prompt,
        registry=registry,
        recorder=recorder,
        approvals=approvals,
        runs=RunService(graph, session_factory, llm, prompt),
    )
