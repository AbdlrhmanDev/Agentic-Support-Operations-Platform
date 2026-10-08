"""Alembic environment. Runs migrations over the app's async engine."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from app.config import get_settings
from app.db.models import Base
from app.db.session import create_engine
from app.eventloop import run

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, *_: object) -> bool:
    # The checkpoint tables belong to LangGraph, which creates and migrates them itself.
    return not (type_ == "table" and name is not None and name.startswith("checkpoint"))


def _migrate(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
    engine = create_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_migrate)
            await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline migrations are not supported; run against a database.")
run(_run_online())
