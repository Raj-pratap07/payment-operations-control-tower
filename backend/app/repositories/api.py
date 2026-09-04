"""Read queries used by the HTTP API layer."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionProposal, AuditEvent, Incident, PaymentStateTransition


class APIRepository:
    def incidents(self, db: Session, *, status: str | None = None, severity: str | None = None, incident_type: str | None = None) -> list[Incident]:
        query = select(Incident)
        if status is not None:
            query = query.where(Incident.status == status)
        if severity is not None:
            query = query.where(Incident.severity == severity)
        if incident_type is not None:
            query = query.where(Incident.incident_type == incident_type)
        return list(db.scalars(query.order_by(Incident.detected_at.desc(), Incident.id)))

    def incident(self, db: Session, incident_id: UUID) -> Incident | None:
        return db.get(Incident, incident_id)

    def actions(self, db: Session, *, status: str | None = None, incident_id: UUID | None = None, action_type: str | None = None) -> list[ActionProposal]:
        query = select(ActionProposal)
        if status is not None:
            query = query.where(ActionProposal.status == status)
        if incident_id is not None:
            query = query.where(ActionProposal.incident_id == incident_id)
        if action_type is not None:
            query = query.where(ActionProposal.action_type == action_type)
        return list(db.scalars(query.order_by(ActionProposal.created_at.desc(), ActionProposal.id)))

    def action(self, db: Session, action_id: UUID) -> ActionProposal | None:
        return db.get(ActionProposal, action_id)

    def pending_approval(self, db: Session, action_id: UUID):
        action = self.action(db, action_id)
        if action is None:
            return None
        return next((approval for approval in action.approvals if approval.status.value == "PENDING"), None)

    def payment_journey(self, db: Session, payment_id: UUID) -> list[PaymentStateTransition]:
        return list(db.scalars(select(PaymentStateTransition).where(PaymentStateTransition.payment_id == payment_id).order_by(PaymentStateTransition.occurred_at, PaymentStateTransition.created_at)))

    def audit(self, db: Session, entity_type: str, entity_id: str) -> list[AuditEvent]:
        return list(db.scalars(select(AuditEvent).where(AuditEvent.entity_type == entity_type, AuditEvent.entity_id == entity_id).order_by(AuditEvent.created_at, AuditEvent.id)))