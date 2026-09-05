"""`GET /healthz` -- the container liveness probe (`mailosh.web.app`).

Two container healthchecks (dev and prod compose) poll this every 30
seconds, and before it existed they rendered the whole `/login` page each
time because that was the only route answering an anonymous 200.

The properties worth pinning are not "it returns 204". They are that it
answers *without a session* and *without touching the database*: a liveness
probe that consults Postgres marks the app container unhealthy during a
Postgres outage, and Docker's answer to an unhealthy container is to
restart it -- which does not fix Postgres and does drop every open SSE
stream the app was serving. `test_healthz_answers_with_the_database_down`
is therefore the test that matters here; the rest are guardrails.
"""

from __future__ import annotations

import pytest
from conftest import make_settings
from fastapi.testclient import TestClient

from mailosh.web.app import create_app


@pytest.fixture
def app(sqlite_url):
    application = create_app(settings=make_settings(sqlite_url))
    with TestClient(application):
        yield application


def test_healthz_is_204_and_empty(app):
    response = TestClient(app).get("/healthz")
    assert response.status_code == 204
    assert response.content == b""


def test_healthz_needs_no_session(app):
    """Anonymous, and *not* a redirect to `/login`.

    `follow_redirects=False` is the point: a 303 would still look like a
    pass to a healthcheck that only asks "did this raise?", so the
    assertion has to name the status.
    """
    response = TestClient(app).get("/healthz", follow_redirects=False)
    assert response.status_code == 204
    assert "location" not in response.headers


def test_healthz_sets_no_cookie(app):
    """A probe running twice a minute forever must not mint session state."""
    assert TestClient(app).get("/healthz").cookies == {}


def test_healthz_answers_with_the_database_down(app):
    """Liveness must not depend on Postgres.

    Sabotaging `app.state.sessionmaker` stands in for a database outage:
    any route that opens a session raises, and `/healthz` must not care.
    The paired assertion on a DB-backed route is what makes this test
    meaningful -- without it, a `/healthz` that quietly stopped being
    routed at all would still "pass".
    """

    def exploded(*args, **kwargs):
        raise RuntimeError("database is down")

    app.state.sessionmaker = exploded

    assert TestClient(app).get("/healthz").status_code == 204

    with pytest.raises(RuntimeError, match="database is down"):
        TestClient(app).get("/mail/inbox", follow_redirects=False)
