"""SQLAlchemy models for the V1 payment operations domain."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BIGINT, Boolean, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, event, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship as orm_relationship

from app.core.enums import (
    ActionProposalStatus,
    ApprovalStatus,
    FinancialEventProcessingStatus,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    PaymentStatus,
    RefundStatus,
    SettlementStatus,
)
from app.database.base import Base


def _uuid() -> UUID:
    return uuid4()


class AppendOnlyRecordError(RuntimeError):
    """Raised when an append-only domain record is changed or deleted."""


class FinancialEvent(Base):
    __tablename__ = "financial_events"
    __table_args__ = (UniqueConstraint("source", "external_event_id", name="uq_financial_events_source_external_event_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processing_status: Mapped[FinancialEventProcessingStatus] = mapped_column(
        Enum(FinancialEventProcessingStatus, name="financial_event_processing_status"),
        nullable=False,
        default=FinancialEventProcessingStatus.RECEIVED,
        server_default=text("'RECEIVED'"),
    )
    processing_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    payment_state_transitions: Mapped[list[PaymentStateTransition]] = orm_relationship(back_populates="financial_event")
    refunds: Mapped[list[Refund]] = orm_relationship(back_populates="source_financial_event")
    settlements: Mapped[list[Settlement]] = orm_relationship(back_populates="source_financial_event")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_payment_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(255))
    amount: Mapped[int] = mapped_column(BIGINT, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus, name="payment_status"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    state_transitions: Mapped[list[PaymentStateTransition]] = orm_relationship(back_populates="payment")
    refunds: Mapped[list[Refund]] = orm_relationship(back_populates="payment")


class PaymentStateTransition(Base):
    __tablename__ = "payment_state_transitions"
    __table_args__ = (Index("ix_payment_state_transitions_payment_id", "payment_id"), Index("ix_payment_state_transitions_financial_event_id", "financial_event_id"))

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"), nullable=False)
    from_state: Mapped[PaymentStatus | None] = mapped_column(Enum(PaymentStatus, name="payment_status", create_type=False))
    to_state: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus, name="payment_status", create_type=False), nullable=False)
    financial_event_id: Mapped[UUID] = mapped_column(ForeignKey("financial_events.id"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    payment: Mapped[Payment] = orm_relationship(back_populates="state_transitions")
    financial_event: Mapped[FinancialEvent] = orm_relationship(back_populates="payment_state_transitions")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        Index("ix_refunds_payment_id", "payment_id"),
        Index("ix_refunds_source_financial_event_id", "source_financial_event_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_refund_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    payment_id: Mapped[UUID] = mapped_column(ForeignKey("payments.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[RefundStatus] = mapped_column(Enum(RefundStatus, name="refund_status"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    payment: Mapped[Payment] = orm_relationship(back_populates="refunds")
    source_financial_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("financial_events.id"))
    source_financial_event: Mapped[FinancialEvent | None] = orm_relationship(back_populates="refunds")


class Settlement(Base):
    __tablename__ = "settlements"
    __table_args__ = (Index("ix_settlements_source_financial_event_id", "source_financial_event_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_settlement_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    amount: Mapped[int] = mapped_column(BIGINT, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fees: Mapped[int] = mapped_column(BIGINT, nullable=False)
    tax: Mapped[int] = mapped_column(BIGINT, nullable=False)
    utr: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[SettlementStatus] = mapped_column(Enum(SettlementStatus, name="settlement_status"), nullable=False)
    settlement_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settlement_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    source_financial_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("financial_events.id"))
    source_financial_event: Mapped[FinancialEvent | None] = orm_relationship(back_populates="settlements")


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    external_transaction_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    transaction_type: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    utr: Mapped[str | None] = mapped_column(String(255))
    reference: Mapped[str | None] = mapped_column(String(255))
    transaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    incident_code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    incident_type: Mapped[IncidentType] = mapped_column(Enum(IncidentType, name="incident_type"), nullable=False)
    severity: Mapped[IncidentSeverity] = mapped_column(Enum(IncidentSeverity, name="incident_severity"), nullable=False)
    status: Mapped[IncidentStatus] = mapped_column(Enum(IncidentStatus, name="incident_status"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    financial_exposure: Mapped[int | None] = mapped_column(BIGINT)
    currency: Mapped[str | None] = mapped_column(String(3))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    evidence_records: Mapped[list[IncidentEvidence]] = orm_relationship(back_populates="incident")
    action_proposals: Mapped[list[ActionProposal]] = orm_relationship(back_populates="incident")


class IncidentEvidence(Base):
    __tablename__ = "incident_evidence"
    __table_args__ = (Index("ix_incident_evidence_incident_id", "incident_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    incident: Mapped[Incident] = orm_relationship(back_populates="evidence_records")


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    max_amount: Mapped[int | None] = mapped_column(BIGINT)
    min_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ActionProposal(Base):
    __tablename__ = "action_proposals"
    __table_args__ = (Index("ix_action_proposals_incident_id", "incident_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int | None] = mapped_column(BIGINT)
    currency: Mapped[str | None] = mapped_column(String(3))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    status: Mapped[ActionProposalStatus] = mapped_column(Enum(ActionProposalStatus, name="action_proposal_status"), nullable=False, default=ActionProposalStatus.PROPOSED, server_default=text("'PROPOSED'"))
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    incident: Mapped[Incident] = orm_relationship(back_populates="action_proposals")
    approvals: Mapped[list[Approval]] = orm_relationship(back_populates="action_proposal")
    executions: Mapped[list[ActionExecution]] = orm_relationship(back_populates="action_proposal")


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_action_proposal_id", "action_proposal_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    action_proposal_id: Mapped[UUID] = mapped_column(ForeignKey("action_proposals.id"), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus, name="approval_status"), nullable=False, default=ApprovalStatus.PENDING, server_default=text("'PENDING'"))
    reason: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    action_proposal: Mapped[ActionProposal] = orm_relationship(back_populates="approvals")


class ActionExecution(Base):
    __tablename__ = "action_executions"
    __table_args__ = (Index("ix_action_executions_action_proposal_id", "action_proposal_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    action_proposal_id: Mapped[UUID] = mapped_column(ForeignKey("action_proposals.id"), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(255))
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    action_proposal: Mapped[ActionProposal] = orm_relationship(back_populates="executions")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    actor_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(255))
    action_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(255))
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


@event.listens_for(Session, "before_flush")
def prevent_append_only_mutation(session: Session, _flush_context: object, _instances: object) -> None:
    """Reject updates and deletes of immutable financial and audit history."""
    protected_types = (FinancialEvent, PaymentStateTransition, AuditEvent)

    for record in session.deleted:
        if isinstance(record, protected_types):
            raise AppendOnlyRecordError(f"{type(record).__name__} records are append-only and cannot be deleted.")

    for record in session.dirty:
        if isinstance(record, protected_types) and session.is_modified(record, include_collections=False):
            raise AppendOnlyRecordError(f"{type(record).__name__} records are append-only and cannot be updated.")
