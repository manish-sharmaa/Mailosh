"""Alembic environment: builds the database URL from `Settings().database_url`
rather than a `sqlalchemy.url` line in `alembic.ini` (deliberately left
unset there), so migrations always target the same database the app itself
would connect to — `MAILOSH_DATABASE_URL` / `.env`, one source of truth,
never duplicated.

Async throughout (`run_migrations_online` builds an `AsyncEngine` and runs
the actual migration steps via `AsyncConnection.run_sync`) since the app's
own engine (`mailosh.db.session.make_engine`) is async — this environment
uses the same asyncpg driver in production, just synchronously-driven DDL
underneath (Alembic's migration runner itself is not async-native; wrapping
the sync `_do_run_migrations` in `run_sync` is the standard bridge, per
Alembic's own async cookbook recipe).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from mailosh.config import Settings
from mailosh.db import models  # noqa: F401 -- import registers every table on Base.metadata
from mailosh.db.base import Base

# Alembic Config object, giving access to values within alembic.ini.
config = context.config

# Interpret alembic.ini's logging section (handlers/formatters above) —
# this is Alembic's own boilerplate, present whether or not a config file
# was actually passed on the command line.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `mailosh.db.models` (imported above, for its side effect) has already
# registered every table on this shared metadata by the time either
# migration path below reads it — same "import order registers the tables"
# mechanism `mailosh.db.base.Base`'s own docstring describes for the `db`
# test fixture.
target_metadata = Base.metadata

# One source of truth for the DB URL: Settings() (MAILOSH_DATABASE_URL /
# .env), not a second copy hardcoded/duplicated into alembic.ini.
config.set_main_option("sqlalchemy.url", Settings().database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade head --sql`).

    Not this project's normal path (there's always a real Postgres to
    migrate against — dev via compose, prod via whatever `MAILOSH_DATABASE_URL`
    points at) but kept, as Alembic's own template does, since it costs
    nothing and is occasionally useful for eyeballing the generated DDL.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live DB via an async engine — the normal
    path: `alembic upgrade head`, `alembic downgrade -1`, etc.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
