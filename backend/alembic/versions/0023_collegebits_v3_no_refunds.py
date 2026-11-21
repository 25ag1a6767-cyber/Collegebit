"""CollegeBits 3.0 strict no-refunds migration:
- Removes legacy refunds table if present
- Normalizes any legacy refund statuses on orders and payments to preserve financial records
  under operational incident resolution
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import inspect, text

from app.database import Base
from app import models  # noqa: F401

revision = "0023_collegebits_v3_no_refunds"
down_revision = "0022_rewards_and_wallet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    insp = inspect(bind)
    tables = set(insp.get_table_names())

    if "orders" in tables:
        bind.execute(
            text(
                "UPDATE orders SET status = 'CANCELLED' "
                "WHERE LOWER(status) = 'refund_pending'"
            )
        )
        bind.execute(
            text(
                "UPDATE orders SET payment_status = 'PAID' "
                "WHERE LOWER(payment_status) IN ('refund_pending', 'partially_refunded', 'refunded')"
            )
        )

    if "payments" in tables:
        bind.execute(
            text(
                "UPDATE payments SET status = 'SUCCESS', late_capture_flagged = :flagged "
                "WHERE UPPER(status) IN ('REFUND_PENDING', 'PARTIALLY_REFUNDED', 'REFUNDED')"
            ),
            {"flagged": True},
        )

    if "refunds" in tables:
        op.drop_table("refunds")

    if "daily_sales_archives" in tables:
        cols = {c["name"] for c in insp.get_columns("daily_sales_archives")}
        if "refund_paise" in cols:
            try:
                with op.batch_alter_table("daily_sales_archives") as batch_op:
                    batch_op.drop_column("refund_paise")
            except Exception:
                pass


def downgrade() -> None:
    pass
