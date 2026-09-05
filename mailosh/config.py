from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAILOSH_", env_file=".env", extra="ignore")
    stalwart_url: str = "http://localhost:8080"
    stalwart_admin_user: str = "admin"
    stalwart_admin_secret: str
    # Optional (Task 1 brief, controller decision #2): Phase 1 replaces the
    # single shared demo account with real per-user login (Task 3), so
    # Settings() itself must construct fine with neither set — every
    # consumer that actually *needs* a demo credential (mailosh.cli's
    # import-mbox, scripts/measure.py, tests/integration/test_live_stalwart.py)
    # checks for None itself and skips/fails with a clear message, rather
    # than this class refusing to construct with an opaque ValidationError.
    demo_user: str | None = None
    demo_password: str | None = None
    smtp_port: int = 2525

    # --- Postgres app-state layer (Task 1) ---
    database_url: str = "postgresql+asyncpg://mailosh:mailosh@localhost:5432/mailosh"

    # --- Live updates: cross-process SSE fan-out ---
    # "memory" (the default) is Phase 1's documented single-uvicorn-worker
    # behaviour: each process's `mailosh.sse.HubRegistry` is the whole
    # world, so a second worker would serve tabs from hubs that never hear
    # what another worker's listener received. "postgres" turns on the
    # `LISTEN`/`NOTIFY` fan-out in `mailosh.db.notify`, which relays each
    # change between workers over `database_url` — so it requires a
    # Postgres `database_url` (`asyncpg_dsn` says so, loudly, at startup)
    # and is what makes running more than one worker meaningful at all.
    # Deliberately a mode rather than a bool: the next transport to exist
    # (if one ever does) is another value here, not a second flag whose
    # interaction with this one has to be reasoned about.
    sse_fanout: Literal["memory", "postgres"] = "memory"

    # --- Web session/security (Task 1 lands the settings; Task 3/5 land the
    # logic that reads them: mailosh.security.crypto/csrf/sessions/ratelimit) ---
    secret_key: str
    cookie_secure: bool = True
    # Whether to trust X-Forwarded-* headers for the client's real IP/scheme
    # (design spec §9's "useXForwarded for real client IPs" deployment note)
    # — Task 5 is the first consumer; the setting itself lands here so it's
    # one place, not re-added piecemeal.
    trust_proxy: bool = False
    session_idle_days: int = 14
    session_remember_days: int = 30
    session_absolute_days: int = 90

    @field_validator("secret_key")
    @classmethod
    def _secret_key_is_real(cls, value: str) -> str:
        # Checked before the length check, not just alongside it: the
        # shipped .env.example placeholder ("change-me-to-32-plus-random-
        # characters") is deliberately >=32 chars so the length check alone
        # can't catch it -- an operator who copies .env.example verbatim
        # and never regenerates it would otherwise ship a publicly-known
        # secret_key (code review finding, Task 1). Prefix match, not just
        # the one exact string, so any other "change-me..." placeholder
        # someone pastes in is caught the same way.
        if value.lower().startswith("change-me"):
            raise ValueError(
                "MAILOSH_SECRET_KEY is still the placeholder from .env.example. "
                "Generate a real one with: openssl rand -hex 32"
            )
        if len(value) < 32:
            raise ValueError("MAILOSH_SECRET_KEY must be at least 32 characters long")
        return value
