"""Normalize legacy lowercase order/user/inventory enum values to uppercase names."""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text

revision = "0028_normalize_order_enums"
down_revision = "0027_free_notification_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "orders" in tables:
        bind.execute(text("UPDATE orders SET status = UPPER(status) WHERE status IS NOT NULL AND status != UPPER(status)"))
        bind.execute(
            text(
                "UPDATE orders SET payment_status = UPPER(payment_status) "
                "WHERE payment_status IS NOT NULL AND payment_status != UPPER(payment_status)"
            )
        )
    if "users" in tables:
        bind.execute(text("UPDATE users SET role = UPPER(role) WHERE role IS NOT NULL AND role != UPPER(role)"))
    if "inventory_movements" in tables:
        bind.execute(
            text(
                "UPDATE inventory_movements SET movement_type = UPPER(movement_type) "
                "WHERE movement_type IS NOT NULL AND movement_type != UPPER(movement_type)"
            )
        )


def downgrade() -> None:
    # Data normalization to canonical uppercase enum member names is safe to leave intact on downgrade.
    pass
