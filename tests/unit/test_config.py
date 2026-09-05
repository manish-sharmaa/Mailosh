"""Unit tests for `mailosh.config.Settings`.

Every `Settings()` construction below passes `_env_file=None` — a
pydantic-settings init-time override of `model_config`'s `env_file=".env"`
— so these tests read only explicitly `monkeypatch.setenv`'d values plus
each field's own default, never this repo's own `.env` (which carries real,
private local-dev values, notably `MAILOSH_SECRET_KEY` and
`MAILOSH_DEMO_PASSWORD`). `monkeypatch.delenv` alone is not enough for
this: it only clears process environment variables, and pydantic-settings
reads a dotenv file as an independent source, so a stray unset key would
otherwise still resolve to whatever `.env` has, not the field's default —
`_env_file=None` is the actual fix; the autouse `monkeypatch.delenv` sweep
below is extra insurance against a real ``MAILOSH_*`` var exported in the
ambient shell.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mailosh.config import Settings

#: Every MAILOSH_* key any test below might otherwise pick up from a real
#: exported shell env var (belt-and-suspenders alongside `_env_file=None`
#: above, which handles this repo's own `.env` file).
_ALL_KEYS = [
    "MAILOSH_STALWART_URL",
    "MAILOSH_STALWART_ADMIN_USER",
    "MAILOSH_STALWART_ADMIN_SECRET",
    "MAILOSH_DEMO_USER",
    "MAILOSH_DEMO_PASSWORD",
    "MAILOSH_SMTP_PORT",
    "MAILOSH_DATABASE_URL",
    "MAILOSH_SECRET_KEY",
    "MAILOSH_COOKIE_SECURE",
    "MAILOSH_TRUST_PROXY",
    "MAILOSH_SESSION_IDLE_DAYS",
    "MAILOSH_SESSION_REMEMBER_DAYS",
    "MAILOSH_SESSION_ABSOLUTE_DAYS",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_reads_env(monkeypatch):
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "s3cret")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)
    s = Settings(_env_file=None)
    assert s.stalwart_url == "http://localhost:8080"
    assert s.stalwart_admin_secret == "s3cret"
    assert s.demo_user is None
    assert s.demo_password is None
    assert s.database_url == "postgresql+asyncpg://mailosh:mailosh@localhost:5432/mailosh"
    assert s.cookie_secure is True
    assert s.trust_proxy is False
    assert (s.session_idle_days, s.session_remember_days, s.session_absolute_days) == (14, 30, 90)


def test_settings_demo_credentials_used_when_set(monkeypatch):
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "s3cret")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("MAILOSH_DEMO_USER", "demo@mailosh.test")
    monkeypatch.setenv("MAILOSH_DEMO_PASSWORD", "pw")
    s = Settings(_env_file=None)
    assert s.demo_user == "demo@mailosh.test"
    assert s.demo_password == "pw"


def test_settings_rejects_short_secret_key(monkeypatch):
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "s3cret")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "too-short")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_rejects_env_example_placeholder_secret_key(monkeypatch):
    """.env.example's own placeholder (code review finding, Task 1): it is
    deliberately >=32 chars, so the length check alone would silently let
    it through and an operator who forgets to regenerate one would ship a
    publicly-known secret_key. Must be rejected anyway, with guidance.
    """
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "s3cret")
    placeholder = "change-me-to-32-plus-random-characters"
    assert len(placeholder) >= 32, "only the change-me check should catch this, not the length one"
    monkeypatch.setenv("MAILOSH_SECRET_KEY", placeholder)
    with pytest.raises(ValidationError, match="openssl rand -hex 32"):
        Settings(_env_file=None)


def test_settings_rejects_any_change_me_prefixed_secret_key(monkeypatch):
    """Not just the one exact placeholder string — any "change-me..." value,
    e.g. a different placeholder someone pastes in by hand.
    """
    monkeypatch.setenv("MAILOSH_STALWART_ADMIN_SECRET", "s3cret")
    monkeypatch.setenv("MAILOSH_SECRET_KEY", "change-me-" + "x" * 30)
    with pytest.raises(ValidationError, match="openssl rand -hex 32"):
        Settings(_env_file=None)
