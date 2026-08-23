"""Add normalized source-event links and database defaults.

Revision ID: 20260823_0002
Revises: 20260823_0001
Create Date: 2026-08-23 00:00:01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260823_0002"
down_revision = "20260823_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("refunds", sa.Column("source_financial_event_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_refunds_source_financial_event_id_financial_events",
        "refunds",
        "financial_events",
        ["source_financial_event_id"],
        ["id"],
    )
    op.create_index("ix_refunds_source_financial_event_id", "refunds", ["source_financial_event_id"])

    op.add_column("settlements", sa.Column("source_financial_event_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_settlements_source_financial_event_id_financial_events",
        "settlements",
        "financial_events",
        ["source_financial_event_id"],
        ["id"],
    )
    op.create_index("ix_settlements_source_financial_event_id", "settlements", ["source_financial_event_id"])

    op.alter_column("financial_events", "processing_status", server_default=sa.text("'RECEIVED'"))
    op.alter_column("policies", "is_active", server_default=sa.text("true"))
    op.alter_column("action_proposals", "requires_approval", server_default=sa.text("false"))
    op.alter_column("action_proposals", "status", server_default=sa.text("'PROPOSED'"))
    op.alter_column("approvals", "status", server_default=sa.text("'PENDING'"))


def downgrade() -> None:
    op.alter_column("approvals", "status", server_default=None)
    op.alter_column("action_proposals", "status", server_default=None)
    op.alter_column("action_proposals", "requires_approval", server_default=None)
    op.alter_column("policies", "is_active", server_default=None)
    op.alter_column("financial_events", "processing_status", server_default=None)

    op.drop_index("ix_settlements_source_financial_event_id", table_name="settlements")
    op.drop_constraint("fk_settlements_source_financial_event_id_financial_events", "settlements", type_="foreignkey")
    op.drop_column("settlements", "source_financial_event_id")
    op.drop_index("ix_refunds_source_financial_event_id", table_name="refunds")
    op.drop_constraint("fk_refunds_source_financial_event_id_financial_events", "refunds", type_="foreignkey")
    op.drop_column("refunds", "source_financial_event_id")
