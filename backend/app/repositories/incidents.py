"""Database access for incidents and deterministic control inputs."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BankTransaction, Incident, IncidentEvidence, Payment, PaymentStateTransition, Refund, Settlement


class IncidentRepository:
    def get_by_code(self, db: Session, incident_code: str) -> Incident | None:
        return db.scalar(select(Incident).where(Incident.incident_code == incident_code))

    def add(self, db: Session, incident: Incident) -> None:
        db.add(incident)


class IncidentEvidenceRepository:
    def get_by_reference(self, db: Session, incident_id: UUID, entity_type: str, entity_id: str) -> IncidentEvidence | None:
        return db.scalar(
            select(IncidentEvidence).where(
                IncidentEvidence.incident_id == incident_id,
                IncidentEvidence.entity_type == entity_type,
                IncidentEvidence.entity_id == entity_id,
            )
        )

    def add(self, db: Session, evidence: IncidentEvidence) -> None:
        db.add(evidence)


class ControlStateRepository:
    def payments(self, db: Session) -> list[Payment]:
        return list(db.scalars(select(Payment)))

    def payment_transitions(self, db: Session, payment_id: UUID) -> list[PaymentStateTransition]:
        return list(db.scalars(select(PaymentStateTransition).where(PaymentStateTransition.payment_id == payment_id)))

    def refunds(self, db: Session) -> list[Refund]:
        return list(db.scalars(select(Refund)))

    def settlements(self, db: Session) -> list[Settlement]:
        return list(db.scalars(select(Settlement)))

    def bank_transactions(self, db: Session) -> list[BankTransaction]:
        return list(db.scalars(select(BankTransaction)))

    def incidents(self, db: Session) -> list[Incident]:
        return list(db.scalars(select(Incident)))

    def incident_evidence(self, db: Session, incident_id: UUID) -> list[IncidentEvidence]:
        return list(db.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)))