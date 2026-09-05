"""The reading half of `POST /prefs` (design spec §10): the four fields the
quick-settings popover's Reading group writes — `mark_read_delay`,
`auto_advance`, `remote_images`, `dark_restyle`.

`tests/unit/test_prefs.py` already covers the endpoint's *shape* — CSRF,
partial updates, per-user isolation — over the three appearance fields, and
none of that is repeated here. What is new about these four is the **column
types**: `mark_read_delay` is an `Integer` column written from a string
`Literal`, and `dark_restyle` is a `Boolean` written from `"true"`/`"false"`.
A string reaching either column persists on sqlite and reads back as a
string, so `mailosh.web.mail` would hand `"3"` to a page that compares it as
a number and quietly behave as though the reader had chosen nothing. Every
test below therefore asserts the stored *type* as well as the value.

Same harness as `test_prefs.py` and for the same reason: the router mounted
on a bare `FastAPI()`, with a real aiosqlite-backed `deps.get_db` (this
route genuinely reads and writes a `UiPref` row) and a stand-in session
carrying only the two attributes the dependency chain reads. `deps.
current_user` and `deps.csrf_protect` stay real. Fixtures are local to this
module rather than added to `tests/conftest.py`, which is another agent's
file this wave.
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

CSRF = "csrf-token-for-tests"

#: `mailosh.db.models.UiPref`'s own column defaults for the four fields
#: below — spelled out rather than re-derived, so a test that changes one
#: can prove the others were never touched instead of merely holding some
#: plausible value.
DEFAULTS = {
    "mark_read_delay": 0,
    "auto_advance": "older",
    "remote_images": "ask",
    "dark_restyle": True,
}


@pytest_asyncio.fixture
async def prefs_env(sqlite_url):
    """`(app, maker)` — see `tests/unit/test_prefs.py::prefs_env` for why
    this is bound to a real temp *file* rather than `:memory:`."""
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


@pytest_asyncio.fixture
async def user(prefs_env):
    """A committed `AppUser` with a real autoincrement id, so `deps.
    current_user`'s real lookup finds a genuine row."""
    _, maker = prefs_env
    async with maker() as session:
        row = AppUser(stalwart_username="reader", email="reader@example.test")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


@pytest.fixture
def post(prefs_env, user):
    """POST to `/prefs` as `user`, with a matching CSRF token."""
    app, _ = prefs_env
    app.dependency_overrides[deps.require_session] = lambda: SimpleNamespace(
        user_id=user.id, csrf_token=CSRF
    )
    client = TestClient(app)

    def _post(**values: str):
        return client.post("/prefs", data=values, headers={"X-CSRF-Token": CSRF})

    return _post


@pytest.fixture
def stored(prefs_env, user):
    """The raw `ui_pref` row, or `None` if the route never created one.

    A plain `session.get`, deliberately not `repo.get_prefs`: that helper
    *creates* a defaults row on a miss, which would hide the exact bug a
    422 test is written to catch — a refused request that nonetheless left
    a row behind.
    """
    _, maker = prefs_env

    async def _stored() -> UiPref | None:
        async with maker() as session:
            return await session.get(UiPref, user.id)

    return _stored


def _trigger(response) -> dict[str, object]:
    return json.loads(response.headers["HX-Trigger"])["om:prefs"]


# ---------------------------------------------------------------------------
# Each field, at the type its column holds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,posted,kept",
    [
        ("mark_read_delay", "0", 0),
        ("mark_read_delay", "1", 1),
        ("mark_read_delay", "3", 3),
        ("mark_read_delay", "-1", -1),
        ("auto_advance", "older", "older"),
        ("auto_advance", "newer", "newer"),
        ("auto_advance", "list", "list"),
        ("remote_images", "ask", "ask"),
        ("remote_images", "contacts", "contacts"),
        ("remote_images", "always", "always"),
        ("dark_restyle", "true", True),
        ("dark_restyle", "false", False),
    ],
)
async def test_each_reading_pref_persists_with_its_column_type(post, stored, field, posted, kept):
    response = post(**{field: posted})

    assert response.status_code == 204
    row = await stored()
    assert row is not None
    assert getattr(row, field) == kept
    # `type(...) is` rather than `isinstance`: `True == 1` in Python, so an
    # `int` in a `Boolean` column would satisfy an equality test and then
    # reach `frames.py`'s `bool(prefs.dark_restyle)` as something the
    # control that posted it could never have produced.
    assert type(getattr(row, field)) is type(kept)
    # ...and the same check on the way *out*, which is where it has teeth.
    # sqlite's column affinity quietly coerces `"3"` into an `Integer`
    # column on the way in, so the row alone cannot tell a cast that
    # happened here from one the database did — while the trigger is
    # whatever this route actually put in it.
    assert type(_trigger(response)[field]) is type(kept)


@pytest.mark.parametrize(
    "field,posted,announced",
    [
        ("mark_read_delay", "3", 3),
        ("auto_advance", "list", "list"),
        ("dark_restyle", "false", False),
    ],
)
def test_the_trigger_announces_what_was_actually_stored(post, field, posted, announced):
    """The `om:prefs` payload is the canonical "this is what I kept", so it
    carries the stored value, not the posted string — a client applying `"3"`
    where the server holds `3` would be applying something else."""
    assert _trigger(post(**{field: posted})) == {field: announced}


# ---------------------------------------------------------------------------
# Values no control offers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        # The one the `Literal` exists for: an `int`-typed field would take
        # this happily, and a delay nobody chose is what the conversation
        # page would then arm its timer with.
        ("mark_read_delay", "7"),
        ("mark_read_delay", "2"),
        ("mark_read_delay", "never"),
        ("auto_advance", "sideways"),
        ("remote_images", "never"),
        ("dark_restyle", "maybe"),
        # Pydantic coerces several of these into booleans when a field is
        # typed `bool`; naming the two legal strings is what makes them a
        # 422 instead.
        ("dark_restyle", "on"),
        ("dark_restyle", "1"),
    ],
)
async def test_an_illegal_value_is_refused_and_writes_nothing_at_all(post, stored, field, value):
    assert post(**{field: value}).status_code == 422
    # Not "the field is unchanged" — no row exists, because a refused
    # request must not even reach `repo.get_prefs`'s create-on-first-look.
    assert await stored() is None


async def test_a_refusal_in_one_field_takes_the_whole_request_with_it(post, stored):
    """Validation runs before the handler body, so a body carrying one good
    field and one bad one persists neither — the alternative is a partial
    write the client was never told about."""
    assert post(mark_read_delay="3").status_code == 204
    assert post(auto_advance="list", remote_images="never").status_code == 422

    row = await stored()
    assert (row.mark_read_delay, row.auto_advance, row.remote_images) == (3, "older", "ask")


# ---------------------------------------------------------------------------
# Partial updates, across the appearance/reading split
# ---------------------------------------------------------------------------


async def test_a_one_field_post_leaves_every_other_preference_alone(post, stored):
    """Each control posts itself, so almost every real request names exactly
    one field. Passing the others at whatever they currently hold would turn
    every click into a full overwrite — and would race the other half of the
    panel, which posts through a different client entirely."""
    assert post(theme="dark", mark_read_delay="3").status_code == 204
    assert post(remote_images="always").status_code == 204
    assert post(dark_restyle="false").status_code == 204

    row = await stored()
    assert row.theme == "dark"
    assert row.mark_read_delay == 3
    assert row.remote_images == "always"
    assert row.dark_restyle is False
    # Never named in any of the three bodies.
    assert row.auto_advance == DEFAULTS["auto_advance"]
    assert row.density == "comfortable"
    assert row.shortcuts is True


async def test_the_reading_fields_can_all_be_written_in_one_request(post, stored):
    """1D's settings pages write the same fields through this same route,
    a form at a time rather than a control at a time."""
    response = post(
        mark_read_delay="-1", auto_advance="newer", remote_images="contacts", dark_restyle="false"
    )

    assert response.status_code == 204
    assert _trigger(response) == {
        "mark_read_delay": -1,
        "auto_advance": "newer",
        "remote_images": "contacts",
        "dark_restyle": False,
    }
    row = await stored()
    assert (row.mark_read_delay, row.auto_advance, row.remote_images, row.dark_restyle) == (
        -1,
        "newer",
        "contacts",
        False,
    )


async def test_a_body_naming_nothing_creates_no_row(post, stored):
    """`repo.set_prefs` creates an all-defaults row as a side effect of
    looking one up, which a request that changed nothing has no business
    triggering."""
    response = post()

    assert response.status_code == 204
    assert _trigger(response) == {}
    assert await stored() is None


# ---------------------------------------------------------------------------
# The fields this route deliberately does not take
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("conversation_view", "false"),
        ("reading_pane", "right"),
        ("undo_send_seconds", "30"),
        ("font_size", "lg"),
    ],
)
async def test_a_preference_with_no_consumer_is_ignored_rather_than_stored(
    post, stored, field, value
):
    """Spec §3: no control exists for a preference nothing reads, and this
    route and the panel are one contract — so a field the panel cannot
    honestly offer is one this route must not quietly accept either. Each
    arrives with the code that branches on it.

    Ignored rather than refused, because an unknown form field is not an
    invalid value: FastAPI simply does not bind it, and a 422 here would
    make adding a field to the panel a breaking change for every client
    that had learned to send it.
    """
    assert post(**{field: value}).status_code == 204
    assert await stored() is None
