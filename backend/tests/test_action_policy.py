from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus, ApprovalStatus, IncidentSeverity, IncidentStatus, IncidentType
from app.database.database import engine
from app.models import ActionProposal, Approval, AuditEvent, Incident, IncidentEvidence, Policy
from app.policies.engine import PolicyOutcome
from app.schemas.investigation import InvestigationEvidence, InvestigationOutput
from app.services.action_planning import (
    ActionPlanner,
    ActionPlanningError,
    ApprovalService,
    InvalidActionEvidenceError,
    InvalidApprovalTransitionError,
    PolicyEvaluationService,
    UnsupportedActionError,
)


@pytest.fixture
def db_session() -> Session:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def setup_incident(db: Session, *, status: IncidentStatus = IncidentStatus.OPEN, amount: int = 5000) -> tuple[Incident, str]:
    incident = Incident(
        incident_code=f"INCIDENT:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM, status=status, title="Settlement issue", description="Mismatch",
        financial_exposure=amount, currency="INR", detected_at=now(),
    )
    db.add(incident)
    db.flush()
    evidence_id = f"settlement-{uuid4()}"
    db.add(IncidentEvidence(
        incident_id=incident.id, evidence_type="financial", entity_type="Settlement",
        entity_id=evidence_id, relationship="supports",
    ))
    db.flush()
    return incident, evidence_id


def investigation(incident: Incident, evidence_id: str, *, amount: int = 5000, confidence: float = 0.95) -> InvestigationOutput:
    return InvestigationOutput(
        incident_id=incident.id, root_cause="Settlement mismatch", summary="Records differ.",
        observed_facts=["Evidence was retrieved."], derived_findings=["Difference is deterministic."],
        evidence=[InvestigationEvidence(entity_type="Settlement", entity_id=evidence_id, relationship="supports")],
        financial_impact_minor=amount, unresolved_amount_minor=amount,
        recommended_action="Review the settlement records.", confidence=confidence, uncertainties=[],
    )


def policy(db: Session, *, active: bool = True, maximum: int = 10000, minimum: str = "0.9000", requires_approval: bool = False, action_type: str = "RECONCILE_ADJUSTMENT") -> Policy:
    value = Policy(
        name=f"Policy {uuid4()}", action_type=action_type, max_amount=maximum,
        min_confidence=Decimal(minimum), requires_approval=requires_approval, is_active=active,
    )
    db.add(value)
    db.flush()
    return value


def proposal(db: Session, *, amount: int = 5000, confidence: float = 0.95) -> ActionProposal:
    incident, evidence_id = setup_incident(db, amount=amount)
    result = ActionPlanner().create_proposal(db, investigation(incident, evidence_id, amount=amount, confidence=confidence), action_type="RECONCILE_ADJUSTMENT")
    return result


def test_valid_proposal_creation_preserves_investigation_values(db_session: Session) -> None:
    incident, evidence_id = setup_incident(db_session)
    result = investigation(incident, evidence_id, amount=7500, confidence=0.93)

    created = ActionPlanner().create_proposal(db_session, result, action_type="RECONCILE_ADJUSTMENT")

    assert created.incident_id == incident.id
    assert created.amount == 7500
    assert created.currency == "INR"
    assert created.confidence == Decimal("0.9300")
    assert created.status is ActionProposalStatus.PROPOSED
    assert created.created_by == "ai_investigator"


def test_auto_allow_under_persisted_policy(db_session: Session) -> None:
    policy(db_session)
    created = proposal(db_session, amount=10000, confidence=0.90)

    decision = PolicyEvaluationService().evaluate(db_session, created.id)

    assert decision.outcome == PolicyOutcome.ALLOW_AUTO
    assert decision.policy_id is not None
    assert decision.evaluated_conditions["maximum_amount"] == 10000


def test_low_confidence_requires_approval(db_session: Session) -> None:
    policy(db_session)
    created = proposal(db_session, confidence=0.80)

    decision = PolicyEvaluationService().evaluate(db_session, created.id)

    assert decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert "confidence" in decision.reason.lower()
    assert created.requires_approval is True


def test_amount_above_threshold_requires_approval(db_session: Session) -> None:
    policy(db_session, maximum=10000)
    created = proposal(db_session, amount=10001)

    decision = PolicyEvaluationService().evaluate(db_session, created.id)

    assert decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert "amount" in decision.reason.lower()


def test_missing_evidence_rejects_proposal(db_session: Session) -> None:
    incident = Incident(
        incident_code=f"INCIDENT:{uuid4()}", incident_type=IncidentType.EVENT_INTEGRITY,
        severity=IncidentSeverity.LOW, status=IncidentStatus.OPEN, title="No evidence", detected_at=now(),
    )
    db_session.add(incident)
    db_session.flush()
    result = investigation(incident, str(uuid4()))

    with pytest.raises(InvalidActionEvidenceError):
        ActionPlanner().create_proposal(db_session, result, action_type="FLAG_FOR_REVIEW")


def test_inactive_policy_rejects(db_session: Session) -> None:
    policy(db_session, active=False)
    created = proposal(db_session)

    decision = PolicyEvaluationService().evaluate(db_session, created.id)

    assert decision.outcome == PolicyOutcome.REJECT
    assert "inactive" in decision.reason.lower()
    assert created.status is ActionProposalStatus.REJECTED


def test_resolved_incident_cannot_create_actionable_proposal(db_session: Session) -> None:
    incident, evidence_id = setup_incident(db_session, status=IncidentStatus.RESOLVED)

    with pytest.raises(ActionPlanningError, match="Resolved or dismissed"):
        ActionPlanner().create_proposal(db_session, investigation(incident, evidence_id), action_type="FLAG_FOR_REVIEW")


def test_duplicate_active_proposal_is_reused(db_session: Session) -> None:
    incident, evidence_id = setup_incident(db_session)
    result = investigation(incident, evidence_id)
    planner = ActionPlanner()

    first = planner.create_proposal(db_session, result, action_type="FLAG_FOR_REVIEW")
    second = planner.create_proposal(db_session, result, action_type="FLAG_FOR_REVIEW")

    assert first.id == second.id
    assert db_session.scalar(select(func.count()).select_from(ActionProposal)) == 1


def test_approval_is_created_for_required_policy(db_session: Session) -> None:
    policy(db_session, requires_approval=True)
    created = proposal(db_session)
    decision = PolicyEvaluationService().evaluate(db_session, created.id)
    approval = ApprovalService().request(db_session, created.id, requested_by="operator", requested_at=now())

    assert decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert approval.status is ApprovalStatus.PENDING
    assert approval.requested_by == "operator"
    assert created.requires_approval is True


def test_approval_rejection_is_deterministic(db_session: Session) -> None:
    created = proposal(db_session)
    approval = ApprovalService().request(db_session, created.id, requested_by="operator", requested_at=now())

    rejected = ApprovalService().reject(db_session, approval.id, reason="Insufficient support")

    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.reason == "Insufficient support"
    assert created.status is ActionProposalStatus.REJECTED
    with pytest.raises(InvalidApprovalTransitionError):
        ApprovalService().approve(db_session, approval.id, approved_by="operator")


def test_approval_grant_creates_one_audit_event_and_retry_is_idempotent(db_session: Session) -> None:
    created = proposal(db_session)
    approval = ApprovalService().request(db_session, created.id, requested_by="operator", requested_at=now())
    service = ApprovalService()

    service.approve(db_session, approval.id, approved_by="operator", approved_at=now())
    service.approve(db_session, approval.id, approved_by="operator", approved_at=now())

    audits = list(db_session.scalars(select(AuditEvent).where(AuditEvent.action_type == "APPROVAL_GRANTED", AuditEvent.entity_id == str(approval.id))))
    assert len(audits) == 1
    assert audits[0].entity_type == "Approval"
    assert audits[0].metadata_["action_proposal_id"] == str(created.id)


def test_approval_rejection_creates_one_audit_event_and_retry_is_idempotent(db_session: Session) -> None:
    created = proposal(db_session)
    approval = ApprovalService().request(db_session, created.id, requested_by="operator", requested_at=now())
    service = ApprovalService()

    service.reject(db_session, approval.id, reason="Insufficient support")
    service.reject(db_session, approval.id, reason="Insufficient support")

    assert db_session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action_type == "APPROVAL_REJECTED", AuditEvent.entity_id == str(approval.id))) == 1


def test_approval_expiry_creates_one_audit_event_and_retry_is_idempotent(db_session: Session) -> None:
    created = proposal(db_session)
    approval = ApprovalService().request(db_session, created.id, requested_by="operator", requested_at=now())
    service = ApprovalService()

    service.expire(db_session, approval.id, reason="Window elapsed")
    service.expire(db_session, approval.id, reason="Window elapsed")

    assert db_session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action_type == "APPROVAL_EXPIRED", AuditEvent.entity_id == str(approval.id))) == 1


def test_approval_expiry_is_deterministic(db_session: Session) -> None:
    created = proposal(db_session)
    approval = ApprovalService().request(db_session, created.id, requested_by="operator", requested_at=now())

    expired = ApprovalService().expire(db_session, approval.id, reason="Window elapsed")

    assert expired.status is ApprovalStatus.EXPIRED
    assert created.status is ActionProposalStatus.CANCELLED


def test_unsupported_action_type_is_rejected(db_session: Session) -> None:
    incident, evidence_id = setup_incident(db_session)

    with pytest.raises(UnsupportedActionError):
        ActionPlanner().create_proposal(db_session, investigation(incident, evidence_id), action_type="ISSUE_REFUND")


def test_repeated_policy_evaluation_is_identical(db_session: Session) -> None:
    policy(db_session)
    created = proposal(db_session)
    service = PolicyEvaluationService()

    first = service.evaluate(db_session, created.id)
    second = service.evaluate(db_session, created.id)

    assert first == second


def test_policy_decision_contains_structured_conditions(db_session: Session) -> None:
    policy(db_session)
    created = proposal(db_session)

    decision = PolicyEvaluationService().evaluate(db_session, created.id)

    assert decision.outcome == PolicyOutcome.ALLOW_AUTO
    assert {
        "action_type_supported", "incident_actionable", "evidence_available", "policy_active",
        "amount", "maximum_amount", "confidence", "minimum_confidence", "policy_requires_approval",
    }.issubset(decision.evaluated_conditions)


def test_no_execution_capability_is_exposed() -> None:
    assert not any(name in {"execute", "refund", "payout", "run"} for name in dir(ActionPlanner))
    assert not any(name in {"execute", "refund", "payout", "run"} for name in dir(PolicyEvaluationService))
    assert not any(name in {"execute", "refund", "payout", "run"} for name in dir(ApprovalService))
