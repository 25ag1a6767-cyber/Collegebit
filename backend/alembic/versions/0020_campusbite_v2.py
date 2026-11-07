"""CampusBite AI v2.0 schema upgrade:
- UUID identifiers (public_uuid) across core transactional entities
- Staff granular permissions (users.staff_permissions_json) and order handler attribution (orders.handled_by_staff_id)
- Unit cost tracking (products.cost_paise, purchase_orders.total_cost_paise) and operational_expenses
- Four-day hybrid retention tables (daily_sales_archives, retention_cleanup_runs, temporary_api_logs)
- Encrypted GFS backup ledger (backup_runs), report ledger (generated_reports), and security monitoring (security_events)
"""
from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

from app.database import Base
from app import models  # noqa: F401

revision = "0020_campusbite_v2"
down_revision = "0019_production_spec_v1"
branch_labels = None
depends_on = None

UUID_TABLES = (
    "users",
    "orders",
    "order_status_history",
    "payments",
    "refunds",
    "pickup_tokens",
    "pickups",
    "order_reviews",
    "batch_reservations",
)

def _ensure_columns(bind, table: str, columns: list[sa.Column]) -> None:
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns(table)}
    for col in columns:
        if col.name not in existing:
            op.add_column(table, col)

def _ensure_index(bind, name: str, table: str, cols: list[str], unique: bool = False) -> None:
    inspector = inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {idx["name"] for idx in inspector.get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, cols, unique=unique)

def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    big_int = sa.Integer() if is_sqlite else sa.BigInteger()

    Base.metadata.create_all(bind=bind)

    for tbl in UUID_TABLES:
        _ensure_columns(bind, tbl, [sa.Column("public_uuid", sa.String(length=36), nullable=True)])
        inspector = inspect(bind)
        if tbl in inspector.get_table_names():
            rows = bind.execute(text(f"SELECT id FROM {tbl} WHERE public_uuid IS NULL")).fetchall()
            for (row_id,) in rows:
                bind.execute(
                    text(f"UPDATE {tbl} SET public_uuid = :u WHERE id = :id"),
                    {"u": str(uuid.uuid4()), "id": row_id},
                )
        _ensure_index(bind, f"ix_{tbl}_public_uuid", tbl, ["public_uuid"], unique=True)

    _ensure_columns(
        bind,
        "users",
        [sa.Column("staff_permissions_json", sa.Text(), nullable=True)],
    )
    _ensure_columns(
        bind,
        "products",
        [sa.Column("cost_paise", big_int, nullable=True, server_default="0")],
    )
    _ensure_columns(
        bind,
        "orders",
        [sa.Column("handled_by_staff_id", sa.Integer(), nullable=True)],
    )
    _ensure_index(bind, "ix_orders_handled_by_staff_id", "orders", ["handled_by_staff_id"], unique=False)
    _ensure_columns(
        bind,
        "purchase_orders",
        [sa.Column("total_cost_paise", big_int, nullable=True, server_default="0")],
    )

def downgrade() -> None:
    for tbl in (
        "temporary_api_logs",
        "security_events",
        "generated_reports",
        "backup_runs",
        "retention_cleanup_runs",
        "daily_sales_archives",
        "operational_expenses",
    ):
        op.drop_table(tbl)
