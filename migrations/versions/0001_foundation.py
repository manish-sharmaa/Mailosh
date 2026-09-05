"""Foundation schema: app_user, session, label_meta, ui_pref, contact,
image_sender_allow, login_attempt, audit_log (design spec §12's Postgres
app-state tables).

Hand-written to mirror `mailosh.db.models` column-for-column, table by
table in the same order — deliberately NOT the output of
`alembic revision --autogenerate` (Task 1 brief: that output is not to be
trusted blindly for a from-scratch baseline). `server_default`s below are a
DB-level safety net matching each column's Python-side `default=` in the
model (so a row inserted outside the ORM still gets a sane value); the ORM
itself never relies on them — every insert goes through
`mailosh.db.repo`/SQLAlchemy, which always supplies these client-side
before the INSERT is even built.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("stalwart_username", sa.String(255), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "session",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("remember", sa.Boolean(), nullable=False),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("api_key_id", sa.String(64), nullable=True),
        sa.Column("api_key_secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False),
    )
    op.create_index("ix_session_user_id", "session", ["user_id"])

    op.create_table(
        "label_meta",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("account_id", sa.String(64), primary_key=True),
        sa.Column("mailbox_id", sa.String(64), primary_key=True),
        sa.Column("color", sa.String(32), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="show"),
        sa.Column("sort_order", sa.Integer(), nullable=True),
    )

    op.create_table(
        "ui_pref",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("theme", sa.String(16), nullable=False, server_default="system"),
        sa.Column("density", sa.String(16), nullable=False, server_default="comfortable"),
        sa.Column("reading_pane", sa.String(16), nullable=False, server_default="none"),
        sa.Column("conversation_view", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mark_read_delay", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_advance", sa.String(16), nullable=False, server_default="older"),
        sa.Column("undo_send_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("remote_images", sa.String(16), nullable=False, server_default="ask"),
        sa.Column("dark_restyle", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("shortcuts", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("font_size", sa.String(8), nullable=False, server_default="md"),
    )

    op.create_table(
        "contact",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
    )

    op.create_table(
        "image_sender_allow",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), primary_key=True),
        sa.Column("sender_email", sa.String(255), primary_key=True),
    )

    op.create_table(
        "login_attempt",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    # Reverse dependency order: every table with a FK to app_user drops
    # before app_user itself (Postgres refuses to drop a still-referenced
    # table); login_attempt has no FK to anything, so its position among
    # these doesn't matter.
    op.drop_table("audit_log")
    op.drop_table("login_attempt")
    op.drop_table("image_sender_allow")
    op.drop_table("contact")
    op.drop_table("ui_pref")
    op.drop_table("label_meta")
    op.drop_index("ix_session_user_id", table_name="session")
    op.drop_table("session")
    op.drop_table("app_user")
