"""Add free notification channel columns (WhatsApp, ntfy, Telegram) to user_notification_preferences."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0027_free_notification_channels"
down_revision = "0026_expo_mobile_push"
branch_labels = None
depends_on = None


def _cols(bind, table: str) -> set[str]:
    if not inspect(bind).has_table(table):
        return set()
    return {col["name"] for col in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    table = "user_notification_preferences"
    cols = _cols(bind, table)
    if not cols:
        return
    if "whatsapp_enabled" not in cols:
        op.add_column(table, sa.Column("whatsapp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "ntfy_topic" not in cols:
        op.add_column(table, sa.Column("ntfy_topic", sa.String(80), nullable=True))
    if "telegram_chat_id" not in cols:
        op.add_column(table, sa.Column("telegram_chat_id", sa.String(64), nullable=True))
    if "telegram_link_token" not in cols:
        op.add_column(table, sa.Column("telegram_link_token", sa.String(48), nullable=True))

    insp = inspect(bind)
    covered_cols = {col for idx in insp.get_indexes(table) for col in (idx.get("column_names") or [])}
    covered_uq = {col for uq in insp.get_unique_constraints(table) for col in (uq.get("column_names") or [])}
    if "ntfy_topic" not in covered_cols and "ntfy_topic" not in covered_uq:
        op.create_index("ix_user_notification_preferences_ntfy_topic", table, ["ntfy_topic"], unique=True)
    if "telegram_chat_id" not in covered_cols:
        op.create_index("ix_user_notification_preferences_telegram_chat_id", table, ["telegram_chat_id"])


def downgrade() -> None:
    bind = op.get_bind()
    table = "user_notification_preferences"
    if not inspect(bind).has_table(table):
        return
    target_cols = {"telegram_link_token", "telegram_chat_id", "ntfy_topic", "whatsapp_enabled"}
    insp = inspect(bind)
    for idx in insp.get_indexes(table):
        if any(col in target_cols for col in (idx.get("column_names") or [])):
            op.drop_index(idx["name"], table_name=table)
    if bind.dialect.name != "sqlite":
        for uq in insp.get_unique_constraints(table):
            if any(col in target_cols for col in (uq.get("column_names") or [])) and uq.get("name"):
                op.drop_constraint(uq["name"], table_name=table, type_="unique")

    cols = _cols(bind, table)
    to_drop = [col for col in ("telegram_link_token", "telegram_chat_id", "ntfy_topic", "whatsapp_enabled") if col in cols]
    if to_drop:
        with op.batch_alter_table(table) as batch_op:
            for col in to_drop:
                batch_op.drop_column(col)
