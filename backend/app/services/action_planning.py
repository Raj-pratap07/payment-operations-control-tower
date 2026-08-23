"""Action proposal planning, policy evaluation, and approval workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus, ApprovalStatus, IncidentStatus
from app.models import ActionProposal, Approval, IncidentEvidence, Policy
from app.policies.engine import PolicyDecision, PolicyEngine, PolicyOutcome
from app.repositories.action_policy import ActionPolicyRepository
from app.repositories.execution import ExecutionRepository
from app.schemas.investigation import InvestigationOutput
from app.services.audit import AuditService


SUPPORTED_ACTIONS = frozenset({
    "RECONCILE_ADJUSTMENT", "FLAG_FOR_REVIEW", "REQUEST_EVIDENCE", "ESCALATE_INCIDENT",
})


class ActionPlanningError(ValueError):
    """Raised when an investigation cannot become an action proposal."""


class UnsupportedActionError(ActionPlanningError):
    pass


class MissingIncidentError(ActionPlanningError):
    pass


class InvalidActionEvidenceError(ActionPlanningError):
    pass


class InvalidApprovalTransitionError(ValueError):
    pass


class ActionPlanner:
    def __init__(self, repository: ActionPolicyRepository | None = None) -> None:
        self._repository = repository or ActionPolicyRepository()

    def create_proposal(
        self,
        db: Session,
        investigation: InvestigationOutput,
        *,
        action_type: str,
        created_by: str = "ai_investigator",
    ) -> ActionProposal:
        if action_type not in SUPPORTED_ACTIONS:
            raise UnsupportedActionError(f"Unsupported action type: {action_type}")
        incident = self._repository.get_incident(db, investigation.incident_id)
        if incident is None:
            raise MissingIncidentError(f"Incident {investigation.incident_id} was not found.")
        if incident.status in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}:
            raise ActionPlanningError("Resolved or dismissed incidents cannot receive actionable proposals.")
        evidence = self._repository.get_evidence(db, incident.id)
        evidence_keys = {(item.entity_type, item.entity_id, item.relationship) for item in evidence}
        requested_keys = {(item.entity_type, item.entity_id, item.relationship) for item in investigation.evidence}
        if not requested_keys or not requested_keys.issubset(evidence_keys):
            raise InvalidActionEvidenceError("Investigation evidence must reference existing incident evidence.")
        existing = self._repository.get_active_proposal(db, incident.id, action_type)
        if existing is not None:
            return existing
        proposal = ActionProposal(
            incident_id=incident.id,
            action_type=action_type,
            description=investigation.recommended_action,
            amount=investigation.financial_impact_minor,
            currency=incident.currency,
            confidence=Decimal(str(investigation.confidence)),
            requires_approval=False,
            status=ActionProposalStatus.PROPOSED,
            created_by=created_by,
        )
        self._repository.add_proposal(db, proposal)
        db.flush()
        return proposal

    plan = create_proposal


class PolicyEvaluationService:
    def __init__(self, repository: ActionPolicyRepository | None = None, engine: PolicyEngine | None = None) -> None:
        self._repository = repository or ActionPolicyRepository()
        self._engine = engine or PolicyEngine()

    def evaluate(self, db: Session, proposal_id: UUID) -> PolicyDecision:
        proposal = self._repository.get_proposal(db, proposal_id)
        if proposal is None:
            raise ActionPlanningError(f"Action proposal {proposal_id} was not found.")
        incident = self._repository.get_incident(db, proposal.incident_id)
        if incident is None:
            raise MissingIncidentError(f"Incident {proposal.incident_id} was not found.")
        policy = self._repository.get_policy(db, proposal.action_type)
        evidence = self._repository.get_evidence(db, incident.id)
        decision = self._engine.evaluate(proposal=proposal, incident=incident, evidence=evidence, policy=policy)
        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            proposal.requires_approval = True
        elif decision.outcome == PolicyOutcome.REJECT:
            proposal.status = ActionProposalStatus.REJECTED
        return decision


class ApprovalService:
    def __init__(
        self,
        repository: ActionPolicyRepository | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._repository = repository or ActionPolicyRepository()
        self._audit = audit or AuditService(ExecutionRepository())

    def request(self, db: Session, proposal_id: UUID, *, requested_by: str, requested_at: datetime | None = None) -> Approval:
        proposal = self._require_proposal(db, proposal_id)
        existing = self._repository.get_pending_approval(db, proposal.id)
        if proposal.status is ActionProposalStatus.APPROVED and existing is None:
            raise InvalidApprovalTransitionError("Approval has already been completed.")
        if proposal.status is not ActionProposalStatus.PROPOSED:
            raise InvalidApprovalTransitionError("Only proposed action proposals can request approval.")
        if existing is not None:
            return existing
        approval = Approval(
            action_proposal_id=proposal.id,
            requested_by=requested_by,
            status=ApprovalStatus.PENDING,
            requested_at=_utc(requested_at or datetime.now(UTC)),
        )
        self._repository.add_approval(db, approval)
        proposal.requires_approval = True
        db.flush()
        return approval

    def approve(self, db: Session, approval_id: UUID, *, approved_by: str, approved_at: datetime | None = None) -> Approval:
        approval = self._require_approval(db, approval_id)
        proposal = self._require_proposal(db, approval.action_proposal_id)
        if approval.status is ApprovalStatus.APPROVED:
            self._record_transition_audit(db, "APPROVAL_GRANTED", approval, proposal, approval.approved_by, "Approval granted.")
            return approval
        self._require_pending(approval)
        approval.status = ApprovalStatus.APPROVED
        approval.approved_by = approved_by
        approval.approved_at = _utc(approved_at or datetime.now(UTC))
        proposal.status = ActionProposalStatus.APPROVED
        db.flush()
        self._record_transition_audit(db, "APPROVAL_GRANTED", approval, proposal, approved_by, "Approval granted.")
        return approval

    def reject(self, db: Session, approval_id: UUID, *, reason: str | None = None) -> Approval:
        approval = self._require_approval(db, approval_id)
        proposal = self._require_proposal(db, approval.action_proposal_id)
        if approval.status is ApprovalStatus.REJECTED:
            self._record_transition_audit(db, "APPROVAL_REJECTED", approval, proposal, None, approval.reason or "Approval rejected.")
            return approval
        self._require_pending(approval)
        approval.status = ApprovalStatus.REJECTED
        approval.reason = reason
        proposal.status = ActionProposalStatus.REJECTED
        db.flush()
        self._record_transition_audit(db, "APPROVAL_REJECTED", approval, proposal, None, reason or "Approval rejected.")
        return approval

    def expire(self, db: Session, approval_id: UUID, *, reason: str | None = None) -> Approval:
        approval = self._require_approval(db, approval_id)
        proposal = self._require_proposal(db, approval.action_proposal_id)
        if approval.status is ApprovalStatus.EXPIRED:
            self._record_transition_audit(db, "APPROVAL_EXPIRED", approval, proposal, None, approval.reason or "Approval expired.")
            return approval
        self._require_pending(approval)
        approval.status = ApprovalStatus.EXPIRED
        approval.reason = reason
        proposal.status = ActionProposalStatus.CANCELLED
        db.flush()
        self._record_transition_audit(db, "APPROVAL_EXPIRED", approval, proposal, None, reason or "Approval expired.")
        return approval

    def _require_approval(self, db: Session, approval_id: UUID) -> Approval:
        approval = self._repository.get_approval(db, approval_id)
        if approval is None:
            raise InvalidApprovalTransitionError(f"Approval {approval_id} was not found.")
        return approval

    def _require_proposal(self, db: Session, proposal_id: UUID) -> ActionProposal:
        proposal = self._repository.get_proposal(db, proposal_id)
        if proposal is None:
            raise ActionPlanningError(f"Action proposal {proposal_id} was not found.")
        return proposal

    @staticmethod
    def _require_pending(approval: Approval) -> None:
        if approval.status is not ApprovalStatus.PENDING:
            raise InvalidApprovalTransitionError("Only pending approvals can change state.")

    def _record_transition_audit(
        self,
        db: Session,
        action_type: str,
        approval: Approval,
        proposal: ActionProposal,
        actor_id: str | None,
        reason: str,
    ) -> None:
        self._audit.record_once(
            db,
            action_type=action_type,
            entity_type="Approval",
            entity_id=approval.id,
            actor_type="HUMAN",
            actor_id=actor_id,
            reason=reason,
            metadata={"action_proposal_id": str(proposal.id), "action_type": proposal.action_type},
        )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)