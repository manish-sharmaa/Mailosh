"""Reading-view schema addition: sender_pref (design spec §7's per-sender
"Show original" memory for the dark restyle — Task 12).

Hand-written to mirror `mailosh.db.models.SenderPref` column-for-column,
same convention as `migrations/versions/0001_foundation.py`: this is not
`alembic revision --autogenerate` output.

Revision ID: 0002_reading
Revises: 0001_foundation
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_reading"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sender_pref",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("sender_email", sa.String(255), primary_key=True),
        sa.Column("dark_restyle", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sender_pref")
