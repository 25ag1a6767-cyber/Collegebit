"""CampusBite AI v3.0 schema upgrade:
- Partial collection tracking on order_items (collected_quantity, is_collected, collected_at, collected_by_staff_id)
- Order status partially_collected support
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0021_campusbite_v3"
down_revision = "0020_campusbite_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    existing_cols = {c["name"] for c in insp.get_columns("order_items")}

    if "collected_quantity" not in existing_cols:
        op.add_column("order_items", sa.Column("collected_quantity", sa.Integer(), nullable=False, server_default="0"))
    if "is_collected" not in existing_cols:
        op.add_column("order_items", sa.Column("is_collected", sa.Boolean(), nullable=False, server_default="0"))
    if "collected_at" not in existing_cols:
        op.add_column("order_items", sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True))
    if "collected_by_staff_id" not in existing_cols:
        op.add_column("order_items", sa.Column("collected_by_staff_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    pass
