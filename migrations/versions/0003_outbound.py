"""Outbound delivery tracking: outbound_submission — one row per message this
app sent, carrying what ``EmailSubmission/get`` (RFC 8621 §7) has since
said about its delivery (`mailosh.services.outbound`).

Hand-written to mirror `mailosh.db.models.OutboundSubmission`
column-for-column, same convention as `0001_foundation.py`/
`0002_reading.py`: this is not `alembic revision --autogenerate` output.

Revision ID: 0003_outbound
Revises: 0002_reading
Create Date: 2026-09-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_outbound"
down_revision: str | None = "0002_reading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbound_submission",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("submission_id", sa.String(64), primary_key=True),
        sa.Column("account_id", sa.String(64), nullable=False),
        sa.Column("email_id", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_outbound_submission_email_id", "outbound_submission", ["email_id"], unique=False
    )
    op.create_index("ix_outbound_submission_state", "outbound_submission", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_outbound_submission_state", table_name="outbound_submission")
    op.drop_index("ix_outbound_submission_email_id", table_name="outbound_submission")
    op.drop_table("outbound_submission")
