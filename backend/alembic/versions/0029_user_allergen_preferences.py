"""Add allergen_preferences_json column to users for persistent customer dietary allergen exclusions."""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0029_user_allergen_preferences"
down_revision = "0028_normalize_order_enums"
branch_labels = None
depends_on = None


def _cols(bind, table: str) -> set[str]:
    if not inspect(bind).has_table(table):
        return set()
    return {col["name"] for col in inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _cols(bind, "users")
    if cols and "allergen_preferences_json" not in cols:
        op.add_column("users", sa.Column("allergen_preferences_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _cols(bind, "users")
    if "allergen_preferences_json" in cols:
        op.drop_column("users", "allergen_preferences_json")
