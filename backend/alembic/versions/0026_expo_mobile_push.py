"""Add additive device_push_tokens table for native mobile push notifications."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0026_expo_mobile_push"
down_revision = "0025_phase89_enterprise_intelligence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("device_push_tokens"):
        op.create_table(
            "device_push_tokens",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("expo_push_token", sa.String(255), nullable=False),
            sa.Column("platform", sa.String(24), nullable=False, server_default="unknown"),
            sa.Column("app_version", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("expo_push_token", name="uq_device_push_tokens_token"),
        )
        op.create_index("ix_device_push_tokens_user_id", "device_push_tokens", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if inspect(bind).has_table("device_push_tokens"):
        op.drop_index("ix_device_push_tokens_user_id", table_name="device_push_tokens")
        op.drop_table("device_push_tokens")
