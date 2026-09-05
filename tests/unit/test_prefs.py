"""Unit tests for the quick-settings prefs endpoint (Task 12, design spec
§10): `POST /prefs`'s per-field validation, partial updates, CSRF gate
(including its precedence over field validation), and per-user isolation.

`mailosh.web.prefs`'s router mounted on a bare `FastAPI()` — never
`create_app` (that module is unclaimed this wave; see `prefs.py`'s own
docstring) — the same shape `test_actions.py` uses for `mailosh.web.
actions`. Unlike that module's fakes, this router genuinely reads/writes a
`UiPref` row, so `deps.get_db` is overridden to a real aiosqlite-backed
session factory (schema built via `Base.metadata.create_all`, same as
`tests/conftest.py`'s own `db` fixture) rather than a fake. `deps.
require_session` is overridden to a bare stand-in carrying just the two
attributes this router's dependency chain actually reads (`user_id`,
`csrf_token`) — no real cookie/login flow to exercise here, that's Task 3/5's
own test suite. `deps.current_user` and `deps.csrf_protect` are left real, so
both the actual per-user DB lookup and the actual CSRF check run exactly as
they would in production.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mailosh.db.base import Base
from mailosh.db.models import AppUser, UiPref
from mailosh.web import deps
from mailosh.web.prefs import router as prefs_router

#: Matches every request's session CSRF token unless a test deliberately
#: overrides one side or the other.
CSRF = "csrf-token-for-tests"

#: `mailosh.db.models.UiPref`'s own column defaults — asserted against
#: directly (rather than re-deriving them) so a test that changes one field
#: can prove the *other* two were never touched, not just that they hold
#: some plausible value.
DEFAULT_THEME = "system"
DEFAULT_DENSITY = "comfortable"
DEFAULT_SHORTCUTS = True


async def _seed_user(maker: async_sessionmaker, username: str) -> AppUser:
    """A committed `AppUser` row with a real autoincrement `id` — the
    session override below hands out `user_id=<this id>`, so `deps.
    current_user`'s real `db.get(AppUser, ...)` lookup finds a genuine row
    instead of raising `SessionRequired`.
    """
    async with maker() as session:
        user = AppUser(stalwart_username=username, email=f"{username}@example.test")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _load_prefs(maker: async_sessionmaker, user_id: int) -> UiPref | None:
    """The raw `ui_pref` row for `user_id`, or `None` if none exists yet.

    Deliberately a plain `session.get` rather than `mailosh.db.repo.
    get_prefs` — that helper *creates* a defaults row on a miss, which would
    silently hide the exact bug several tests below are written to catch:
    a 422/403 request that nonetheless left a row behind.
    """
    async with maker() as session:
        return await session.get(UiPref, user_id)


@pytest_asyncio.fixture
async def prefs_env(sqlite_url):
    """`(app, maker)`: a bare `FastAPI()` carrying only `prefs_router`, plus
    the sessionmaker `deps.get_db` is overridden to hand out sessions from.

    Bound to `sqlite_url` (`tests/conftest.py`'s fixture — a real temp
    *file*, not `sqlite+aiosqlite:///:memory:`): every session opened below
    goes through a fresh connection (a new one per `TestClient` request, a
    separate one again for this fixture's own setup and for `_seed_user`/
    `_load_prefs`), and an in-memory sqlite database is private to whichever
    single connection first created it — every connection after that would
    see a blank, tableless database instead of what `create_all` just built.
    """
    engine = create_async_engine(sqlite_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with maker() as session:
            yield session

    app = FastAPI()
    app.include_router(prefs_router)
    app.dependency_overrides[deps.get_db] = _get_db

    yield app, maker
    await engine.dispose()


def _post(
    app: FastAPI,
    user: AppUser,
    data: dict[str, str],
    *,
    session_csrf: str = CSRF,
    header_csrf: str | None = CSRF,
):
    """POST `data` to `/prefs` as `user`. `session_csrf` is what the
    (faked) session itself carries; `header_csrf` is what the request
    actually sends as `X-CSRF-Token` (`None` omits the header entirely) —
    kept as two independent knobs so a mismatch between them is exactly how
    the wrong/missing-token tests below are expressed.
    """
    app.dependency_overrides[deps.require_session] = lambda: SimpleNamespace(
        user_id=user.id, csrf_token=session_csrf
    )
    headers = {} if header_csrf is None else {"X-CSRF-Token": header_csrf}
    return TestClient(app).post("/prefs", data=data, headers=headers)


def _trigger(response) -> dict[str, object]:
    return json.loads(response.headers["HX-Trigger"])["om:prefs"]


# ---------------------------------------------------------------------------
# Each field's valid values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["system", "light", "dark"])
async def test_theme_accepts_each_valid_value(prefs_env, value):
    app, maker = prefs_env
    user = await _seed_user(maker, f"theme-{value}")

    response = _post(app, user, {"theme": value})

    assert response.status_code == 204
    assert response.content == b""
    assert _trigger(response) == {"theme": value}
    row = await _load_prefs(maker, user.id)
    assert row.theme == value
    assert (row.density, row.shortcuts) == (DEFAULT_DENSITY, DEFAULT_SHORTCUTS)


@pytest.mark.parametrize("value", ["compact", "standard", "comfortable"])
async def test_density_accepts_each_valid_value(prefs_env, value):
    app, maker = prefs_env
    user = await _seed_user(maker, f"density-{value}")

    response = _post(app, user, {"density": value})

    assert response.status_code == 204
    assert _trigger(response) == {"density": value}
    row = await _load_prefs(maker, user.id)
    assert row.density == value
    assert (row.theme, row.shortcuts) == (DEFAULT_THEME, DEFAULT_SHORTCUTS)


@pytest.mark.parametrize(("value", "expected"), [("true", True), ("false", False)])
async def test_shortcuts_accepts_each_valid_value(prefs_env, value, expected):
    app, maker = prefs_env
    user = await _seed_user(maker, f"shortcuts-{value}")

    response = _post(app, user, {"shortcuts": value})

    assert response.status_code == 204
    assert _trigger(response) == {"shortcuts": expected}  # a real JSON bool, not "true"/"false"
    row = await _load_prefs(maker, user.id)
    assert row.shortcuts is expected
    assert (row.theme, row.density) == (DEFAULT_THEME, DEFAULT_DENSITY)


# ---------------------------------------------------------------------------
# Partial / combined updates
# ---------------------------------------------------------------------------


async def test_partial_update_touches_only_the_field_sent(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "partial")

    response = _post(app, user, {"density": "compact"})

    assert _trigger(response) == {"density": "compact"}  # exactly one key: no phantom fields
    row = await _load_prefs(maker, user.id)
    assert row.density == "compact"
    assert row.theme == DEFAULT_THEME
    assert row.shortcuts == DEFAULT_SHORTCUTS


async def test_all_three_fields_together(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "all-three")

    response = _post(app, user, {"theme": "dark", "density": "compact", "shortcuts": "false"})

    assert _trigger(response) == {"theme": "dark", "density": "compact", "shortcuts": False}
    row = await _load_prefs(maker, user.id)
    assert (row.theme, row.density, row.shortcuts) == ("dark", "compact", False)


async def test_a_later_partial_update_does_not_revert_an_earlier_field(prefs_env):
    # Two independent partial requests in sequence: the second must not
    # reset the first field back to its default just because this request
    # never mentioned it.
    app, maker = prefs_env
    user = await _seed_user(maker, "sequence")

    first = _post(app, user, {"theme": "dark"})
    second = _post(app, user, {"density": "compact"})

    assert (first.status_code, second.status_code) == (204, 204)
    row = await _load_prefs(maker, user.id)
    assert (row.theme, row.density) == ("dark", "compact")


async def test_empty_update_writes_no_row_and_reports_nothing_changed(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "empty")

    response = _post(app, user, {})

    assert response.status_code == 204
    assert _trigger(response) == {}
    assert await _load_prefs(maker, user.id) is None  # no row ever created


# ---------------------------------------------------------------------------
# Invalid values -> 422, and nothing written
# ---------------------------------------------------------------------------


async def test_invalid_theme_value_is_422_and_writes_nothing(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "bad-theme")

    response = _post(app, user, {"theme": "blue"})

    assert response.status_code == 422
    assert await _load_prefs(maker, user.id) is None


async def test_invalid_density_value_is_422_and_writes_nothing(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "bad-density")

    response = _post(app, user, {"density": "spacious"})

    assert response.status_code == 422
    assert await _load_prefs(maker, user.id) is None


async def test_invalid_shortcuts_value_is_422_and_writes_nothing(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "bad-shortcuts")

    # Not a bare typo: "yes"/"1"/"True" are all real, common truthy spellings
    # a lenient bool parser would accept -- this route must not, since the
    # brief pins the wire values to exactly "true"/"false".
    response = _post(app, user, {"shortcuts": "yes"})

    assert response.status_code == 422
    assert await _load_prefs(maker, user.id) is None


async def test_one_bad_field_does_not_stop_the_others_from_being_reported_invalid(prefs_env):
    # Sanity on the error itself: FastAPI collects every field error, not
    # just the first -- both bad fields must show up, not one masking the
    # other.
    app, maker = prefs_env
    user = await _seed_user(maker, "bad-multi")

    response = _post(app, user, {"theme": "blue", "density": "spacious"})

    assert response.status_code == 422
    locations = {tuple(err["loc"]) for err in response.json()["detail"]}
    assert ("body", "theme") in locations
    assert ("body", "density") in locations
    assert await _load_prefs(maker, user.id) is None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def test_missing_csrf_token_is_403_and_writes_nothing(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "no-csrf")

    response = _post(app, user, {"theme": "dark"}, header_csrf=None)

    assert response.status_code == 403
    assert await _load_prefs(maker, user.id) is None


async def test_wrong_csrf_token_is_403_and_writes_nothing(prefs_env):
    app, maker = prefs_env
    user = await _seed_user(maker, "wrong-csrf")

    response = _post(app, user, {"theme": "dark"}, header_csrf="not-the-token")

    assert response.status_code == 403
    assert await _load_prefs(maker, user.id) is None


async def test_csrf_failure_wins_over_an_invalid_field_value(prefs_env):
    # The ordering guarantee `mailosh.web.actions`'s own routes carry
    # (design spec §9): a bad token 403s before any field is even looked
    # at, so this must come back 403, not 422, even though "blue" is also
    # not a real theme.
    app, maker = prefs_env
    user = await _seed_user(maker, "csrf-vs-422")

    response = _post(app, user, {"theme": "blue"}, header_csrf="not-the-token")

    assert response.status_code == 403
    assert await _load_prefs(maker, user.id) is None


# ---------------------------------------------------------------------------
# Per-user isolation
# ---------------------------------------------------------------------------


async def test_one_users_write_never_touches_another_users_row(prefs_env):
    app, maker = prefs_env
    alice = await _seed_user(maker, "alice")
    bob = await _seed_user(maker, "bob")

    _post(app, alice, {"theme": "dark"})
    _post(app, bob, {"theme": "light"})

    alice_row = await _load_prefs(maker, alice.id)
    bob_row = await _load_prefs(maker, bob.id)
    assert alice_row.theme == "dark"
    assert bob_row.theme == "light"


async def test_a_users_partial_update_does_not_disturb_another_users_prior_value(prefs_env):
    app, maker = prefs_env
    alice = await _seed_user(maker, "alice2")
    bob = await _seed_user(maker, "bob2")

    _post(app, alice, {"theme": "dark", "density": "compact"})
    _post(app, bob, {"density": "compact"})  # bob's own request never mentions theme

    alice_row = await _load_prefs(maker, alice.id)
    bob_row = await _load_prefs(maker, bob.id)
    assert (alice_row.theme, alice_row.density) == ("dark", "compact")
    assert bob_row.theme == DEFAULT_THEME  # untouched by bob's own request
    assert bob_row.density == "compact"


async def test_a_rejected_write_from_one_user_leaves_another_users_row_alone(prefs_env):
    # Cross-user isolation has to hold on the failure paths too, not just
    # the happy one: bob's bad CSRF token must not somehow land on alice's
    # row (or anyone else's).
    app, maker = prefs_env
    alice = await _seed_user(maker, "alice3")
    bob = await _seed_user(maker, "bob3")
    _post(app, alice, {"theme": "dark"})

    response = _post(app, bob, {"theme": "light"}, header_csrf="not-the-token")

    assert response.status_code == 403
    assert (await _load_prefs(maker, alice.id)).theme == "dark"
    assert await _load_prefs(maker, bob.id) is None


# ---------------------------------------------------------------------------
# Route shape
# ---------------------------------------------------------------------------


def test_route_is_post_only(prefs_env):
    app, _maker = prefs_env
    paths = {route.path: route.methods for route in prefs_router.routes}
    assert paths == {"/prefs": {"POST"}}
    assert TestClient(app).get("/prefs").status_code == 405
