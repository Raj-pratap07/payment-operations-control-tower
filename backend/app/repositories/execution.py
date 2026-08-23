"""Database access for controlled action execution and audit records."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionExecution, ActionProposal, Approval, AuditEvent, BankTransaction, Incident, IncidentEvidence, Payment, Refund


class ExecutionRepository:
    def get_proposal(self, db: Session, proposal_id: UUID) -> ActionProposal | None:
        return db.get(ActionProposal, proposal_id)

    def get_execution(self, db: Session, execution_id: str) -> ActionExecution | None:
        return db.scalar(select(ActionExecution).where(ActionExecution.execution_id == execution_id))

    def get_execution_by_proposal(self, db: Session, proposal_id: UUID) -> ActionExecution | None:
        return db.scalar(select(ActionExecution).where(ActionExecution.action_proposal_id == proposal_id))

    def add_execution(self, db: Session, execution: ActionExecution) -> None:
        db.add(execution)

    def get_approval(self, db: Session, proposal_id: UUID) -> Approval | None:
        return db.scalar(select(Approval).where(Approval.action_proposal_id == proposal_id, Approval.status == "APPROVED"))

    def get_incident(self, db: Session, incident_id: UUID) -> Incident | None:
        return db.get(Incident, incident_id)

    def get_incident_evidence(self, db: Session, incident_id: UUID) -> list[IncidentEvidence]:
        return list(db.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)))
    def get_payment(self, db: Session, payment_id: UUID) -> Payment | None:
        return db.get(Payment, payment_id)

    def get_refund(self, db: Session, refund_id: UUID) -> Refund | None:
        return db.get(Refund, refund_id)

    def get_bank_transactions(self, db: Session) -> list[BankTransaction]:
        return list(db.scalars(select(BankTransaction)))

    def get_payments(self, db: Session) -> list[Payment]:
        return list(db.scalars(select(Payment)))

    def get_refunds(self, db: Session) -> list[Refund]:
        return list(db.scalars(select(Refund)))

    def get_incident_evidence(self, db: Session, incident_id: UUID) -> list[IncidentEvidence]:
        return list(db.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)))

    def add_audit(self, db: Session, audit: AuditEvent) -> None:
        db.add(audit)

    def get_audit(self, db: Session, *, action_type: str, entity_type: str, entity_id: str) -> AuditEvent | None:
        return db.scalar(
            select(AuditEvent).where(
                AuditEvent.action_type == action_type,
                AuditEvent.entity_type == entity_type,
                AuditEvent.entity_id == entity_id,
            )
        )