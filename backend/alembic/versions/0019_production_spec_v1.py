"""Production specification v1 schema: counters, sessions/MFA, paise fields,
carts, discounts, payments/refunds, short-lived pickup tokens, recipes/ingredients,
forecasts, privacy-safe AI telemetry, web push and outbox events.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.database import Base
from app import models  # noqa: F401

revision = "0019_production_spec_v1"
down_revision = "0018_read_path_indexes"
branch_labels = None
depends_on = None


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


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Create any brand-new tables cleanly on both SQLite and PostgreSQL.
    Base.metadata.create_all(bind=bind, checkfirst=True)

    # Widen enum VARCHAR columns on PostgreSQL before storing longer state names.
    if dialect == "postgresql":
        op.alter_column("orders", "status", existing_type=sa.String(9), type_=sa.String(30), existing_nullable=False)
        op.alter_column("orders", "payment_status", existing_type=sa.String(8), type_=sa.String(30), existing_nullable=False)
        op.alter_column("users", "role", existing_type=sa.String(7), type_=sa.String(20), existing_nullable=False)
        op.alter_column("inventory_movements", "movement_type", existing_type=sa.String(11), type_=sa.String(20), existing_nullable=False)

    big_int = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

    _ensure_columns(bind, "users", [
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("dietary_preference", sa.String(50), nullable=True),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("mfa_secret_encrypted", sa.String(255), nullable=True),
        sa.Column("mfa_recovery_hashes_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ])

    _ensure_columns(bind, "categories", [
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
    ])

    _ensure_columns(bind, "products", [
        sa.Column("counter_id", sa.Integer(), nullable=True),
        sa.Column("price_paise", big_int, nullable=True),
        sa.Column("preparation_minutes", sa.Integer(), server_default="10", nullable=False),
        sa.Column("dietary_tags", sa.String(255), nullable=True),
        sa.Column("allergen_tags", sa.String(255), nullable=True),
        sa.Column("ingredients_list", sa.Text(), nullable=True),
        sa.Column("customizations_json", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ])

    _ensure_columns(bind, "carts", [
        sa.Column("counter_id", sa.Integer(), nullable=True),
        sa.Column("coupon_code", sa.String(50), nullable=True),
        sa.Column("pickup_slot", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(500), nullable=True),
    ])

    _ensure_columns(bind, "discounts", [
        sa.Column("usage_limit", sa.Integer(), nullable=True),
        sa.Column("staff_approvable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
    ])

    _ensure_columns(bind, "orders", [
        sa.Column("order_number", sa.String(40), nullable=True),
        sa.Column("counter_id", sa.Integer(), nullable=True),
        sa.Column("manual_pickup_code", sa.String(20), nullable=True),
        sa.Column("discount_amount", sa.Numeric(10, 2), server_default="0.00", nullable=False),
        sa.Column("tax_amount", sa.Numeric(10, 2), server_default="0.00", nullable=False),
        sa.Column("final_total", sa.Numeric(10, 2), server_default="0.00", nullable=False),
        sa.Column("subtotal_paise", big_int, server_default="0", nullable=True),
        sa.Column("discount_paise", big_int, server_default="0", nullable=True),
        sa.Column("tax_paise", big_int, server_default="0", nullable=True),
        sa.Column("total_paise", big_int, server_default="0", nullable=True),
        sa.Column("coupon_code", sa.String(50), nullable=True),
        sa.Column("exception_reason", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(100), nullable=True),
        sa.Column("pickup_slot_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    ])

    _ensure_columns(bind, "order_items", [
        sa.Column("unit_price_paise", big_int, nullable=True),
        sa.Column("customizations_json", sa.Text(), nullable=True),
        sa.Column("line_total_paise", big_int, nullable=True),
    ])

    _ensure_columns(bind, "refunds", [
        sa.Column("payment_record_id", sa.Integer(), nullable=True),
        sa.Column("amount_paise", big_int, nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ])

    _ensure_columns(bind, "payment_webhook_events", [
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("processing_status", sa.String(30), server_default="processed", nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    ])

    _ensure_columns(bind, "ingredients", [
        sa.Column("cost_per_unit", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    ])

    _ensure_columns(bind, "forecast_runs", [
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
    ])

    _ensure_columns(bind, "forecast_items", [
        sa.Column("low_confidence_warning", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("staff_approved_quantity", sa.Integer(), nullable=True),
        sa.Column("override_by_user_id", sa.Integer(), nullable=True),
        sa.Column("override_reason", sa.String(255), nullable=True),
        sa.Column("actual_sold", sa.Integer(), nullable=True),
        sa.Column("actual_waste", sa.Integer(), nullable=True),
    ])

    _ensure_columns(bind, "ai_interactions", [
        sa.Column("model_version", sa.String(60), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
        sa.Column("output_metadata_json", sa.Text(), nullable=True),
    ])

    # Backfill integer paise fields for existing rows.
    bind.execute(sa.text("UPDATE products SET price_paise = CAST(ROUND(price * 100) AS INTEGER) WHERE price_paise IS NULL"))
    bind.execute(sa.text("UPDATE order_items SET unit_price_paise = CAST(ROUND(unit_price * 100) AS INTEGER), line_total_paise = CAST(ROUND(line_total * 100) AS INTEGER) WHERE unit_price_paise IS NULL"))
    bind.execute(sa.text(
        "UPDATE orders SET "
        "subtotal_paise = CAST(ROUND(subtotal * 100) AS INTEGER), "
        "discount_paise = CAST(ROUND(COALESCE(discount_amount, 0) * 100) AS INTEGER), "
        "tax_paise = CAST(ROUND(COALESCE(tax_amount, 0) * 100) AS INTEGER), "
        "final_total = CASE WHEN COALESCE(final_total, 0) = 0 THEN subtotal ELSE final_total END, "
        "total_paise = CAST(ROUND((CASE WHEN COALESCE(final_total, 0) = 0 THEN subtotal ELSE final_total END) * 100) AS INTEGER) "
        "WHERE subtotal_paise IS NULL OR subtotal_paise = 0"
    ))

    # Normalize empty phone strings to NULL so the unique index allows unset phones.
    bind.execute(sa.text("UPDATE users SET phone = NULL WHERE phone IS NOT NULL AND TRIM(phone) = ''"))
    bind.execute(sa.text("UPDATE users SET phone = NULL WHERE id NOT IN (SELECT MIN(id) FROM users WHERE phone IS NOT NULL GROUP BY phone)"))

    # Indexes specified in Section 5 of the production specification.
    _ensure_index(bind, "ix_orders_counter_status_created", "orders", ["counter_id", "status", "created_at"])
    _ensure_index(bind, "ix_orders_order_number", "orders", ["order_number"], unique=True)
    _ensure_index(bind, "ix_products_counter_available", "products", ["counter_id", "is_available"])
    _ensure_index(bind, "ix_payments_provider_payment", "payments", ["provider_payment_id"])
    _ensure_index(bind, "ix_notifications_user_read_at", "notifications", ["user_id", "read_at"])
    _ensure_index(bind, "ix_users_phone", "users", ["phone"], unique=True)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    for table in ("outbox_events", "push_subscriptions", "stock_movements", "pickup_tokens", "payments", "menu_item_availability", "refresh_sessions"):
        if table in inspector.get_table_names():
            op.drop_table(table)
