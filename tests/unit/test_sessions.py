from datetime import datetime, timedelta, timezone

from mailosh.config import Settings
from mailosh.db import repo
from mailosh.security import sessions


def _settings(**kw):
    # NOTE (Task 3 implementer): the brief's verbatim helper passed
    # cookie_secure=False as a fixed kwarg *and* spread **kw over it, which
    # raises "got multiple values for keyword argument 'cookie_secure'" as
    # soon as a caller overrides it -- exactly what
    # test_cookie_name_depends_on_secure does. setdefault preserves the
    # same default for every other call site while letting this one
    # override it in both directions, which is what the test clearly needs.
    kw.setdefault("cookie_secure", False)
    return Settings(stalwart_admin_secret="s", secret_key="k" * 40, **kw)


async def test_create_load_touch_and_secret(db):
    s = _settings()
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    row = await sessions.create_session(
        db,
        user=user,
        remember=False,
        user_agent="ua",
        ip="1.1.1.1",
        api_key_id="k1",
        api_key_secret="sekrit",
        settings=s,
    )
    assert len(row.id) >= 32 and row.csrf_token and row.api_key_secret_enc != b"sekrit"
    loaded = await sessions.load_session(db, row.id, s, now=now)
    assert loaded.user_id == user.id and sessions.api_key_secret(loaded, s) == "sekrit"
    assert loaded.last_seen_at >= now - timedelta(seconds=1)


async def test_idle_and_absolute_expiry(db):
    s = _settings(session_idle_days=14, session_absolute_days=90)
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    row = await sessions.create_session(
        db,
        user=user,
        remember=False,
        user_agent=None,
        ip=None,
        api_key_id="k",
        api_key_secret="x",
        settings=s,
    )
    created = row.created_at
    assert await sessions.load_session(db, row.id, s, now=created + timedelta(days=13)) is not None
    # idle > 14d since last touch
    assert await sessions.load_session(db, row.id, s, now=created + timedelta(days=13 + 15)) is None
    row2 = await sessions.create_session(
        db,
        user=user,
        remember=True,
        user_agent=None,
        ip=None,
        api_key_id="k",
        api_key_secret="x",
        settings=s,
    )
    # absolute cap
    loaded2 = await sessions.load_session(db, row2.id, s, now=row2.created_at + timedelta(days=91))
    assert loaded2 is None


async def test_revoke_all_returns_api_key_ids(db):
    s = _settings()
    user = await repo.get_or_create_user(db, "d@x", "d@x")
    a = await sessions.create_session(
        db,
        user=user,
        remember=False,
        user_agent=None,
        ip=None,
        api_key_id="ka",
        api_key_secret="x",
        settings=s,
    )
    await sessions.create_session(
        db,
        user=user,
        remember=False,
        user_agent=None,
        ip=None,
        api_key_id="kb",
        api_key_secret="x",
        settings=s,
    )
    revoked = await sessions.revoke_all(db, user.id)
    assert sorted(r.api_key_id for r in revoked) == ["ka", "kb"]
    assert await sessions.load_session(db, a.id, s) is None


def test_cookie_name_depends_on_secure():
    assert sessions.cookie_name(_settings(cookie_secure=True)) == "__Host-sid"
    assert sessions.cookie_name(_settings(cookie_secure=False)) == "sid"
    p = sessions.cookie_params(_settings(cookie_secure=True))
    assert p["httponly"] and p["samesite"] == "lax" and p["secure"] and p["path"] == "/"
