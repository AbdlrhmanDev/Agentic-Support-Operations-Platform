"""Shared fixtures. Tests run against a real PostgreSQL with pgvector, never a mock.

Point TEST_DATABASE_URL at a server you can create a database on. The test
database is created if missing and migrated to head.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app.agent.llm.base import LLMClient
from app.config import Settings
from app.container import Container, build_container
from app.eventloop import loop_factory
from app.policies.retrieval import ingest_policies
from evals.scenarios.model import Expected, OrderSeed, Scenario
from evals.seeding import SeededScenario, seed_scenario

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://support:support@localhost:5434/support_ops_test",
)

ContainerFactory = Callable[[LLMClient], Awaitable[Container]]
Seeder = Callable[..., Awaitable[SeededScenario]]


def pytest_asyncio_loop_factories() -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    # psycopg's async mode needs a selector loop on Windows.
    return {"default": loop_factory}


@pytest.fixture(scope="session")
def database_url() -> str:
    """Create the test database if needed and migrate it to head."""
    url = make_url(TEST_DATABASE_URL)
    admin = url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    with psycopg.connect(admin, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)
        ).fetchone()
        if not exists:
            connection.execute(f'CREATE DATABASE "{url.database}"')

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        tool_retry_backoff_seconds=0.0,
        tool_timeout_seconds=5.0,
    )


@pytest.fixture
async def make_container(settings: Settings) -> AsyncIterator[ContainerFactory]:
    """Build the full application stack around a given model stand-in."""
    built: list[Container] = []

    async def factory(llm: LLMClient) -> Container:
        container = await build_container(settings, llm)
        built.append(container)
        async with container.session_factory.begin() as session:
            await ingest_policies(session, container.embedder)
        return container

    yield factory
    for container in built:
        await container.aclose()


@pytest.fixture
def seed() -> Seeder:
    """Seed an isolated customer with the given orders."""

    async def seeder(container: Container, *orders: OrderSeed) -> SeededScenario:
        scenario = Scenario(
            id="test", category="test", message="", orders=orders, expected=Expected(calls=())
        )
        async with container.session_factory.begin() as session:
            return await seed_scenario(session, scenario)

    return seeder
