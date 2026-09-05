"""Unit tests for `mailosh.db.models`/`mailosh.db.repo` (Task 1 brief,
Step 1): schema creation via the `db` fixture (`tests/conftest.py`, an
in-memory aiosqlite engine built from `Base.metadata`) plus the repo
helpers these two tests exercise — `get_or_create_user`/`get_prefs`/
`set_prefs` (user + prefs round-trip) and `label_meta_map` (keyed by
mailbox id).
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.db import models, repo
from mailosh.db.base import Base


async def test_schema_creates_and_user_roundtrip(db):
    user = await repo.get_or_create_user(db, "demo@mailosh.test", "demo@mailosh.test")
    again = await repo.get_or_create_user(db, "demo@mailosh.test", "demo@mailosh.test")
    assert user.id == again.id
    prefs = await repo.get_prefs(db, user.id)
    assert (prefs.theme, prefs.density, prefs.shortcuts) == ("system", "comfortable", True)
    await repo.set_prefs(db, user.id, theme="dark", density="compact")
    prefs = await repo.get_prefs(db, user.id)
    assert (prefs.theme, prefs.density) == ("dark", "compact")


async def test_label_meta_map_keyed_by_mailbox(db):
    user = await repo.get_or_create_user(db, "u", "u@x")
    db.add(
        models.LabelMeta(
            user_id=user.id, account_id="a", mailbox_id="m1", color="indigo", visibility="show"
        )
    )
    await db.commit()
    m = await repo.label_meta_map(db, user.id, "a")
    assert m["m1"].color == "indigo"


async def test_get_or_create_user_survives_a_concurrent_first_login_race(tmp_path):
    """Task 1's own deferred note ("get_or_create_user has no duplicate-
    insert race handling (revisit in T5 concurrent login)"): two requests
    for the SAME brand-new username (e.g. two browser tabs both racing to
    complete a first-ever login) can both SELECT "not found" before either
    commits its INSERT — the second commit then hits `stalwart_username`'s
    UNIQUE constraint. `get_or_create_user` must recover (return the row
    the other request just created) rather than let that IntegrityError
    propagate as a 500.

    Needs two genuinely independent `AsyncSession`s sharing one real
    database — the `db` fixture's single session can't reproduce this (all
    operations on one `AsyncSession` are inherently sequential) — so this
    builds its own file-backed engine (a temp file, not `:memory:`, so both
    sessions' connections see the same schema/data; see `tests/conftest.py`'s
    `sqlite_url` fixture for the same reasoning) and races two real
    coroutines against it with `asyncio.gather`.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _login_attempt():
        async with maker() as session:
            return await repo.get_or_create_user(session, "race@x", "race@x")

    try:
        results = await asyncio.gather(_login_attempt(), _login_attempt())
    finally:
        await engine.dispose()

    assert results[0].id == results[1].id
    assert results[0].stalwart_username == "race@x"
