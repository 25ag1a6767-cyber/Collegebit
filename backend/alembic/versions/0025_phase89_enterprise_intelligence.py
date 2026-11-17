"""Phase 89 Enterprise Intelligence & Reliability 3.0 schema migration.
Adds AI Decision Intelligence, Operational Memory, Non-Autonomous Multi-Agent Proposals,
Smart Campus Utilities/Water/Fire/Sensors, Procurement RFQ/Receiving, Institutional Meal Credits,
Recovery Drills, Webhook Subscriptions & Versioned Tenant Configuration Blueprints.
"""
from __future__ import annotations

from alembic import op
from app.database import Base
from app import models  # noqa: F401

revision = "0025_phase89_enterprise_intelligence"
down_revision = "0024_phase60_to_85_enterprise"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    pass
