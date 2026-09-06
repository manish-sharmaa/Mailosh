"""Settings-page schema additions (design spec §10's Compose page):
`ui_pref.default_reply` and the `signature` table.

Hand-written to mirror `mailosh.db.models` column-for-column, same
convention as `0001_foundation.py` and `0002_reading.py`: this is not
`alembic revision --autogenerate` output.

`default_reply` lands with `server_default="reply"` and is then dropped
back to a plain NOT NULL. The server default is only there for the ALTER
itself — an existing row has no value for a new NOT NULL column, and
without it this migration fails on any database that already has users.
Dropping it afterwards keeps the column's shape identical to
`models.UiPref`'s (`default="reply"`, a Python-side default, per that
module's dialect-neutral rule) rather than leaving a Postgres-only
default that sqlite's `create_all` would never produce.

Revision ID: 0003_settings
Revises: 0002_reading
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_settings"
down_revision: str | None = "0002_reading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ui_pref",
        sa.Column("default_reply", sa.String(16), nullable=False, server_default="reply"),
    )
    op.alter_column("ui_pref", "default_reply", server_default=None)
    op.create_table(
        "signature",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("account_id", sa.String(64), primary_key=True),
        sa.Column("identity_id", sa.String(64), primary_key=True),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("signature")
    op.drop_column("ui_pref", "default_reply")
