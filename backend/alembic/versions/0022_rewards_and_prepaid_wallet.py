"""Add auditable student rewards and prepaid wallet ledgers."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

from app.database import Base
from app import models  # noqa: F401

revision = "0022_rewards_and_wallet"
down_revision = "0021_campusbite_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)
    columns = {column["name"] for column in inspect(bind).get_columns("orders")}
    if "loyalty_points_redeemed" not in columns:
        op.add_column(
            "orders",
            sa.Column("loyalty_points_redeemed", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("wallet_transactions", "reward_transactions"):
        if table in inspect(bind).get_table_names():
            op.drop_table(table)
    if "loyalty_points_redeemed" in {
        column["name"] for column in inspect(bind).get_columns("orders")
    }:
        op.drop_column("orders", "loyalty_points_redeemed")
