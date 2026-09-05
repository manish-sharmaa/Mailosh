"""Async engine/session-factory construction, plus the `get_db` FastAPI
dependency.

`make_engine`/`make_sessionmaker` are split out from wherever they end up
actually being called (a later task's `create_app` lifespan, mirroring how
`mailosh/web/app.py` already builds its JMAP client there — see that
module's own docstring) so this stays two plain, independently testable
functions with no FastAPI/`Settings` coupling of their own.
`tests/conftest.py`'s `db` fixture doesn't call these directly (its own
aiosqlite URL and defaults are deliberately spelled out inline there), but
builds an engine/sessionmaker pair the same shape these two produce.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(url: str, echo: bool = False) -> AsyncEngine:
    """Build the async SQLAlchemy engine for `url` (typically
    `Settings().database_url`, e.g. `postgresql+asyncpg://...`).
    """
    return create_async_engine(url, echo=echo)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to `engine`.

    `expire_on_commit=False`, matching `tests/conftest.py`'s `db` fixture:
    attributes on a row a caller already holds (e.g. what
    `mailosh.db.repo.get_or_create_user` returns) stay readable after that
    caller's own `commit()`, with no implicit re-`SELECT` on next access.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yield an `AsyncSession` from
    `request.app.state.sessionmaker`.

    Mirrors `mailosh.web.deps.get_client`'s shape — read something the app
    factory's lifespan already built and stored on `app.state`. Wiring
    `app.state.sessionmaker` itself (calling `make_engine`/`make_sessionmaker`
    from `create_app`'s lifespan) is a later task's job, not this one's;
    this module only defines the dependency function every route that needs
    a `db: Annotated[AsyncSession, Depends(get_db)]` will import.
    """
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
