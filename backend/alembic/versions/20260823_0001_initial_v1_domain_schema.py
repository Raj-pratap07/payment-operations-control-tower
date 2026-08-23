"""Initial V1 payment operations domain schema.

Revision ID: 20260823_0001
Revises:
Create Date: 2026-08-23 00:00:00
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260823_0001"
down_revision = None
branch_labels = None
depends_on = None


financial_event_processing_status = postgresql.ENUM("RECEIVED", "PROCESSING", "PROCESSED", "FAILED", name="financial_event_processing_status", create_type=False)
payment_status = postgresql.ENUM("CREATED", "AUTHORIZED", "CAPTURED", "FAILED", "REFUND_PENDING", "REFUNDED", name="payment_status", create_type=False)
refund_status = postgresql.ENUM("CREATED", "PENDING", "PROCESSED", "FAILED", name="refund_status", create_type=False)
settlement_status = postgresql.ENUM("CREATED", "PROCESSED", "FAILED", name="settlement_status", create_type=False)
incident_type = postgresql.ENUM("PAYMENT_STATE_CONFLICT", "PAYMENT_SIGNAL_OVERDUE", "SETTLEMENT_DISCREPANCY", "REFUND_FINANCIAL_DRIFT", "EVENT_INTEGRITY", "SETTLEMENT_CREDIT_DELAY", "FINANCIAL_EXPOSURE", "PAYMENT_PATH_DEGRADATION", name="incident_type", create_type=False)
incident_severity = postgresql.ENUM("LOW", "MEDIUM", "HIGH", "CRITICAL", name="incident_severity", create_type=False)
incident_status = postgresql.ENUM("OPEN", "INVESTIGATING", "ACTION_REQUIRED", "RESOLVING", "RESOLVED", "DISMISSED", name="incident_status", create_type=False)
action_proposal_status = postgresql.ENUM("PROPOSED", "APPROVED", "REJECTED", "EXECUTING", "EXECUTED", "FAILED", "CANCELLED", name="action_proposal_status", create_type=False)
approval_status = postgresql.ENUM("PENDING", "APPROVED", "REJECTED", "EXPIRED", name="approval_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (financial_event_processing_status, payment_status, refund_status, settlement_status, incident_type, incident_severity, incident_status, action_proposal_status, approval_status):
        enum.create(bind, checkfirst=True)

    op.create_table("financial_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("external_event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("processing_status", financial_event_processing_status, nullable=False),
        sa.Column("processing_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source", "external_event_id", name="uq_financial_events_source_external_event_id"),
    )
    op.create_table("payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("provider_order_id", sa.String(length=255)), sa.Column("amount", sa.BIGINT(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False), sa.Column("status", payment_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True)), sa.Column("captured_at", sa.DateTime(timezone=True)), sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table("payment_state_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("from_state", payment_status), sa.Column("to_state", payment_status, nullable=False),
        sa.Column("financial_event_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("financial_events.id"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_payment_state_transitions_payment_id", "payment_state_transitions", ["payment_id"])
    op.create_index("ix_payment_state_transitions_financial_event_id", "payment_state_transitions", ["financial_event_id"])
    op.create_table("refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("provider_refund_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("payments.id"), nullable=False), sa.Column("amount", sa.BIGINT(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False), sa.Column("status", refund_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("processed_at", sa.DateTime(timezone=True)), sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_refunds_payment_id", "refunds", ["payment_id"])
    op.create_table("settlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("provider_settlement_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("amount", sa.BIGINT(), nullable=False), sa.Column("currency", sa.String(length=3), nullable=False), sa.Column("fees", sa.BIGINT(), nullable=False), sa.Column("tax", sa.BIGINT(), nullable=False),
        sa.Column("utr", sa.String(length=255)), sa.Column("status", settlement_status, nullable=False),
        sa.Column("settlement_period_start", sa.DateTime(timezone=True)), sa.Column("settlement_period_end", sa.DateTime(timezone=True)), sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table("bank_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("external_transaction_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("transaction_type", sa.String(length=100), nullable=False), sa.Column("amount", sa.BIGINT(), nullable=False), sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("utr", sa.String(length=255)), sa.Column("reference", sa.String(length=255)), sa.Column("transaction_at", sa.DateTime(timezone=True), nullable=False), sa.Column("source", sa.String(length=100), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table("incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("incident_code", sa.String(length=100), nullable=False, unique=True),
        sa.Column("incident_type", incident_type, nullable=False), sa.Column("severity", incident_severity, nullable=False), sa.Column("status", incident_status, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False), sa.Column("description", sa.Text()), sa.Column("financial_exposure", sa.BIGINT()), sa.Column("currency", sa.String(length=3)),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False), sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table("incident_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("incident_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("evidence_type", sa.String(length=100), nullable=False), sa.Column("entity_type", sa.String(length=100), nullable=False), sa.Column("entity_id", sa.String(length=255), nullable=False), sa.Column("relationship", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_incident_evidence_incident_id", "incident_evidence", ["incident_id"])
    op.create_table("policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("name", sa.String(length=255), nullable=False), sa.Column("description", sa.Text()), sa.Column("action_type", sa.String(length=100), nullable=False),
        sa.Column("max_amount", sa.BIGINT()), sa.Column("min_confidence", sa.Numeric(precision=5, scale=4)), sa.Column("requires_approval", sa.Boolean(), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table("action_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("incident_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("action_type", sa.String(length=100), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("amount", sa.BIGINT()), sa.Column("currency", sa.String(length=3)),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4)), sa.Column("requires_approval", sa.Boolean(), nullable=False), sa.Column("status", action_proposal_status, nullable=False), sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_action_proposals_incident_id", "action_proposals", ["incident_id"])
    op.create_table("approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("action_proposal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("action_proposals.id"), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False), sa.Column("approved_by", sa.String(length=255)), sa.Column("status", approval_status, nullable=False), sa.Column("reason", sa.Text()),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("approved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_approvals_action_proposal_id", "approvals", ["action_proposal_id"])
    op.create_table("action_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("action_proposal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("action_proposals.id"), nullable=False),
        sa.Column("execution_id", sa.String(length=255), nullable=False), sa.Column("status", sa.String(length=100), nullable=False), sa.Column("provider_reference", sa.String(length=255)),
        sa.Column("before_state", postgresql.JSONB()), sa.Column("after_state", postgresql.JSONB()), sa.Column("error", sa.Text()), sa.Column("executed_at", sa.DateTime(timezone=True)), sa.Column("verified_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_action_executions_action_proposal_id", "action_executions", ["action_proposal_id"])
    op.create_table("audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True), sa.Column("actor_type", sa.String(length=100), nullable=False), sa.Column("actor_id", sa.String(length=255)),
        sa.Column("action_type", sa.String(length=100), nullable=False), sa.Column("entity_type", sa.String(length=100), nullable=False), sa.Column("entity_id", sa.String(length=255)), sa.Column("reason", sa.Text()),
        sa.Column("evidence", postgresql.JSONB()), sa.Column("metadata", postgresql.JSONB()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    for table in ("audit_events", "action_executions", "approvals", "action_proposals", "policies", "incident_evidence", "incidents", "bank_transactions", "settlements", "refunds", "payment_state_transitions", "payments", "financial_events"):
        op.drop_table(table)
    bind = op.get_bind()
    for enum in (approval_status, action_proposal_status, incident_status, incident_severity, incident_type, settlement_status, refund_status, payment_status, financial_event_processing_status):
        enum.drop(bind, checkfirst=True)
