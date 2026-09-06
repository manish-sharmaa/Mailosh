"""Operations hardening: orphan_api_key -- Stalwart API keys whose destroy
failed and are retried by the maintenance loop (`mailosh.web.orphan_keys`).

Hand-written to mirror `mailosh.db.models.OrphanApiKey` column-for-column,
same convention as the two migrations before it: this is not
`alembic revision --autogenerate` output.

Revision ID: 0004_orphan_api_key
Revises: 0002_reading
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_orphan_api_key"
down_revision: str | None = "0003_outbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orphan_api_key",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stalwart_username", sa.String(255), nullable=False),
        sa.Column("api_key_id", sa.String(64), nullable=False),
        sa.Column(
            "first_failed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("orphan_api_key")
