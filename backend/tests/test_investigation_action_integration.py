"""Tests for investigation-to-action-proposal integration."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus, ApprovalStatus, IncidentSeverity, IncidentStatus, IncidentType
from app.database.database import engine
from app.models import Incident, IncidentEvidence, Policy
from app.policies.engine import PolicyOutcome
from app.schemas.investigation import InvestigationEvidence, InvestigationOutput
from app.services.investigation_action_planning import (
    InvestigationActionService,
    InvalidActionTypeError,
    MissingInvestigationError,
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


def create_incident(db: Session) -> tuple[Incident, IncidentEvidence]:
    incident = Incident(
        incident_code=f"TEST_INCIDENT_{uuid4()}",
        incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM,
        status=IncidentStatus.OPEN,
        title="Test settlement discrepancy",
        description="Test incident for integration testing",
        financial_exposure=5000,
        currency="INR",
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()

    evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="Settlement",
        entity_id=str(uuid4()),
        relationship="supports",
    )
    db.add(evidence)
    db.flush()

    return incident, evidence


def create_policy(db: Session, action_type: str, *, max_amount: int = 10000, min_confidence: Decimal = Decimal("0.9"), requires_approval: bool = False) -> Policy:
    policy = Policy(
        name=f"Test policy for {action_type}",
        description="Test policy",
        action_type=action_type,
        max_amount=max_amount,
        min_confidence=min_confidence,
        requires_approval=requires_approval,
        is_active=True,
    )
    db.add(policy)
    db.flush()
    return policy


def create_investigation(incident_id, evidence_id: str, *, confidence: float = 0.95, recommended_action: str = "Reconcile the settlement adjustment.") -> InvestigationOutput:
    return InvestigationOutput(
        incident_id=incident_id,
        root_cause="Settlement amount differs from expected records.",
        summary="The available records show a settlement discrepancy.",
        observed_facts=["Settlement is 5000 INR less than expected."],
        derived_findings=["The deterministic difference is 5000 minor units."],
        evidence=[InvestigationEvidence(entity_type="Settlement", entity_id=evidence_id, relationship="supports")],
        financial_impact_minor=5000,
        unresolved_amount_minor=5000,
        recommended_action=recommended_action,
        confidence=confidence,
        uncertainties=[],
    )


def test_valid_investigation_creates_proposal(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT")
    investigation = create_investigation(incident.id, evidence.entity_id)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.proposal.incident_id == incident.id
    assert result.proposal.action_type == "RECONCILE_ADJUSTMENT"
    assert result.proposal.amount == 5000
    assert result.proposal.currency == "INR"
    assert result.proposal.confidence == Decimal("0.95")
    assert result.proposal.status == ActionProposalStatus.PROPOSED
    assert result.proposal.created_by == "ai_investigator"


def test_proposal_preserves_incident_association(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT")
    investigation = create_investigation(incident.id, evidence.entity_id)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.proposal.incident_id == incident.id
    assert result.proposal.incident.incident_code == incident.incident_code


def test_proposal_preserves_confidence(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT")
    investigation = create_investigation(incident.id, evidence.entity_id, confidence=0.87)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.proposal.confidence == Decimal("0.87")


def test_evidence_validation_requires_existing_evidence(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT")
    investigation = create_investigation(incident.id, str(uuid4()))  # Wrong evidence ID

    service = InvestigationActionService()
    with pytest.raises(ValueError, match="evidence"):
        service.create_proposal_from_investigation(db_session, investigation)


def test_unsupported_action_type_rejected(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    investigation = create_investigation(incident.id, evidence.entity_id, recommended_action="Issue a refund.")

    service = InvestigationActionService()
    with pytest.raises(InvalidActionTypeError, match="Could not determine"):
        service.create_proposal_from_investigation(db_session, investigation)


def test_duplicate_proposal_idempotency(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT")
    investigation = create_investigation(incident.id, evidence.entity_id)

    service = InvestigationActionService()
    result1 = service.create_proposal_from_investigation(db_session, investigation)
    result2 = service.create_proposal_from_investigation(db_session, investigation)

    assert result1.proposal.id == result2.proposal.id


def test_policy_allow_auto_for_valid_proposal(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT", max_amount=10000, min_confidence=Decimal("0.9"))
    investigation = create_investigation(incident.id, evidence.entity_id, confidence=0.95)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.policy_decision.outcome == PolicyOutcome.ALLOW_AUTO
    assert result.approval_id is None


def test_policy_require_approval_for_high_amount(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT", max_amount=1000, min_confidence=Decimal("0.9"))
    investigation = create_investigation(incident.id, evidence.entity_id, confidence=0.95)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.policy_decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert result.proposal.requires_approval is True
    assert result.approval_id is not None


def test_policy_require_approval_for_low_confidence(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT", max_amount=10000, min_confidence=Decimal("0.95"))
    investigation = create_investigation(incident.id, evidence.entity_id, confidence=0.85)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.policy_decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert result.proposal.requires_approval is True
    assert result.approval_id is not None


def test_approval_created_when_required(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT", max_amount=1000, min_confidence=Decimal("0.9"))
    investigation = create_investigation(incident.id, evidence.entity_id)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.approval_id is not None
    approval = db_session.get(Incident, result.approval_id)
    assert approval is None  # Approval model is separate
    from app.models import Approval
    approval = db_session.get(Approval, result.approval_id)
    assert approval.status == ApprovalStatus.PENDING
    assert approval.action_proposal_id == result.proposal.id


def test_policy_reject_for_inactive_policy(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    policy = create_policy(db_session, "RECONCILE_ADJUSTMENT")
    policy.is_active = False
    db_session.flush()
    investigation = create_investigation(incident.id, evidence.entity_id)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.policy_decision.outcome == PolicyOutcome.REJECT
    assert result.proposal.status == ActionProposalStatus.REJECTED


def test_action_type_inference_from_recommendation(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "FLAG_FOR_REVIEW")
    investigation = create_investigation(incident.id, evidence.entity_id, recommended_action="Flag this incident for manual review by the operations team.")

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation)

    assert result.proposal.action_type == "FLAG_FOR_REVIEW"


def test_action_type_override_takes_precedence(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "ESCALATE_INCIDENT")
    investigation = create_investigation(incident.id, evidence.entity_id, recommended_action="Review the settlement.")

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(db_session, investigation, action_type="ESCALATE_INCIDENT")

    assert result.proposal.action_type == "ESCALATE_INCIDENT"


def test_no_financial_state_mutation(db_session: Session) -> None:
    incident, evidence = create_incident(db_session)
    create_policy(db_session, "RECONCILE_ADJUSTMENT")
    investigation = create_investigation(incident.id, evidence.entity_id)
    original_exposure = incident.financial_exposure

    service = InvestigationActionService()
    service.create_proposal_from_investigation(db_session, investigation)
    db_session.flush()
    db_session.expire_all()

    assert incident.financial_exposure == original_exposure
    assert incident.status == IncidentStatus.OPEN


def test_missing_investigation_rejected(db_session: Session) -> None:
    service = InvestigationActionService()
    with pytest.raises(MissingInvestigationError):
        service.create_proposal_from_investigation(None, None)  # type: ignore[arg-type]
