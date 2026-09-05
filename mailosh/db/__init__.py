"""Postgres app-state layer: SQLAlchemy 2 async models (`models.py`) plus
their Alembic migrations (`../../migrations/`) — `app_user`, `session`,
`label_meta`, `ui_pref`, `contact`, `image_sender_allow`, `login_attempt`,
`audit_log` (design spec §12's exact table list).

Mail content itself never lives in any of these tables — that stays in
Stalwart, reached only via `mailosh.jmap`; this package holds webmail
*application* state only (who's logged in, what they've customized, what
they've done).
"""
