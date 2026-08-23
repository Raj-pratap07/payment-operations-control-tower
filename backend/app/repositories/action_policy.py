"""Database access for action planning, policy evaluation, and approvals."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionProposal, Approval, Incident, IncidentEvidence, Policy


ACTIVE_PROPOSAL_STATUSES = ("PROPOSED", "APPROVED", "EXECUTING")


class ActionPolicyRepository:
    def get_incident(self, db: Session, incident_id: UUID) -> Incident | None:
        return db.get(Incident, incident_id)

    def get_evidence(self, db: Session, incident_id: UUID) -> list[IncidentEvidence]:
        return list(db.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)))

    def get_policy(self, db: Session, action_type: str) -> Policy | None:
        return db.scalar(select(Policy).where(Policy.action_type == action_type).order_by(Policy.created_at, Policy.id))

    def get_active_proposal(self, db: Session, incident_id: UUID, action_type: str) -> ActionProposal | None:
        return db.scalar(
            select(ActionProposal).where(
                ActionProposal.incident_id == incident_id,
                ActionProposal.action_type == action_type,
                ActionProposal.status.in_(ACTIVE_PROPOSAL_STATUSES),
            ).order_by(ActionProposal.created_at, ActionProposal.id)
        )

    def get_proposal(self, db: Session, proposal_id: UUID) -> ActionProposal | None:
        return db.get(ActionProposal, proposal_id)

    def get_approval(self, db: Session, approval_id: UUID) -> Approval | None:
        return db.get(Approval, approval_id)

    def add_proposal(self, db: Session, proposal: ActionProposal) -> None:
        db.add(proposal)

    def get_pending_approval(self, db: Session, proposal_id: UUID) -> Approval | None:
        return db.scalar(select(Approval).where(Approval.action_proposal_id == proposal_id, Approval.status == "PENDING"))

    def add_approval(self, db: Session, approval: Approval) -> None:
        db.add(approval)