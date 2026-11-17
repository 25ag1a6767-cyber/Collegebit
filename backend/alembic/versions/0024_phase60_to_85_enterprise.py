"""Phase 60 to 85 Enterprise, Parcel/Ready-to-Eat Fulfillment, Kitchen Stations,
Support Tickets, Food Safety, Notifications, Sustainability Waste, Shifts,
Procurement, Smart Pricing, Group Orders, Meal Passes, Daily Closing & Multi-College
Admin Governance / Ownership Transfer schema migration.
"""
from __future__ import annotations

from alembic import op
from app.database import Base
from app import models  # noqa: F401

revision = "0024_phase60_to_85_enterprise"
down_revision = "0023_collegebits_v3_no_refunds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    pass
