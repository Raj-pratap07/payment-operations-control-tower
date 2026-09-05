"""Tests for the demo RECONCILE_ADJUSTMENT policy seed.

Validates that the demo policy is created with correct fields, is idempotent,
and causes the PolicyEngine to return REQUIRE_APPROVAL for high-value proposals.
"""

import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

# Add project root so `simulator.run` is importable during pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from datetime import UTC, datetime
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    ActionProposalStatus,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
)
from app.database.database import engine
from app.models import (
    Incident,
    IncidentEvidence,
    Policy,
)
from app.policies.engine import PolicyEngine, PolicyOutcome
from app.schemas.investigation import (
    InvestigationEvidence,
    InvestigationOutput,
)
from app.services.investigation_action_planning import (
    InvestigationActionService,
)
from simulator.run import seed_demo_policies

DEMO_AMOUNT = 2_800_000


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


def _setup_incident(db: Session) -> tuple[Incident, IncidentEvidence]:
    incident = Incident(
        incident_code=f"DEMO_POLICY_TEST_{uuid4()}",
        incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        title="Settlement discrepancy for demo policy test",
        description="Testing demo policy evaluation",
        financial_exposure=DEMO_AMOUNT,
        currency="INR",
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()

    evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="Settlement",
        entity_id=f"settlement-{uuid4()}",
        relationship="supports",
    )
    db.add(evidence)
    db.flush()
    return incident, evidence


def _make_investigation(
    incident: Incident,
    evidence: IncidentEvidence,
) -> InvestigationOutput:
    return InvestigationOutput(
        incident_id=incident.id,
        root_cause="Settlement amount differs from expected records.",
        summary="The available records show a settlement discrepancy.",
        observed_facts=[
            "Settlement is less than expected.",
        ],
        derived_findings=[
            "The deterministic difference is 2,800,000 minor units.",
        ],
        evidence=[
            InvestigationEvidence(
                entity_type="Settlement",
                entity_id=evidence.entity_id,
                relationship="supports",
            )
        ],
        financial_impact_minor=DEMO_AMOUNT,
        unresolved_amount_minor=DEMO_AMOUNT,
        recommended_action="Reconcile the settlement adjustment.",
        confidence=Decimal("0.90"),
        uncertainties=[],
    )


# ── Tests ────────────────────────────────────────────────────────────────


def test_demo_policy_creation(db_session: Session) -> None:
    """1. Policy is created with the correct fields."""
    seed_demo_policies(db_session)

    policy = db_session.scalar(
        select(Policy).where(Policy.name == "DEMO:RECONCILE_ADJUSTMENT")
    )
    assert policy is not None
    assert policy.action_type == "RECONCILE_ADJUSTMENT"
    assert policy.max_amount == 10_000
    assert policy.min_confidence == Decimal("0.90")
    assert policy.requires_approval is True
    assert policy.is_active is True


def test_demo_policy_idempotent(db_session: Session) -> None:
    """2. Running seed twice does not create duplicates."""
    seed_demo_policies(db_session)
    seed_demo_policies(db_session)

    count = db_session.scalar(
        select(func.count()).select_from(Policy).where(
            Policy.name == "DEMO:RECONCILE_ADJUSTMENT"
        )
    )
    assert count == 1


def test_reconcile_adjustment_policy_evaluates_to_require_approval(
    db_session: Session,
) -> None:
    """3. RECONCILE_ADJUSTMENT with amount=2,800,000 triggers REQUIRE_APPROVAL."""
    seed_demo_policies(db_session)
    incident, evidence = _setup_incident(db_session)
    investigation = _make_investigation(incident, evidence)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(
        db_session, investigation
    )

    assert result.proposal.action_type == "RECONCILE_ADJUSTMENT"
    assert result.proposal.amount == DEMO_AMOUNT
    assert result.policy_decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert result.proposal.requires_approval is True


def test_policy_decision_reason_contains_amount_exceeds(
    db_session: Session,
) -> None:
    """4. Policy reason contains the expected text about amount exceeding."""
    seed_demo_policies(db_session)
    incident, evidence = _setup_incident(db_session)
    investigation = _make_investigation(incident, evidence)

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(
        db_session, investigation
    )

    assert "amount" in result.policy_decision.reason.lower()
    assert "exceed" in result.policy_decision.reason.lower()


def test_unrelated_actions_not_allowed_by_demo_policy(
    db_session: Session,
) -> None:
    """5. The demo RECONCILE_ADJUSTMENT policy does not affect other action types."""
    seed_demo_policies(db_session)
    incident, evidence = _setup_incident(db_session)
    investigation = _make_investigation(incident, evidence)
    investigation = investigation.model_copy(
        update={"recommended_action": "Flag for review."}
    )

    service = InvestigationActionService()
    result = service.create_proposal_from_investigation(
        db_session, investigation, action_type="FLAG_FOR_REVIEW"
    )

    assert result.proposal.action_type == "FLAG_FOR_REVIEW"
    assert result.policy_decision.outcome == PolicyOutcome.REJECT
    assert "no policy" in result.policy_decision.reason.lower()


def test_engine_not_modified(db_session: Session) -> None:
    """6. PolicyEngine evaluation is identical before and after seed."""
    from app.models import ActionProposal

    incident, evidence = _setup_incident(db_session)
    proposal = ActionProposal(
        incident_id=incident.id,
        action_type="RECONCILE_ADJUSTMENT",
        description="Test",
        amount=5000,
        currency="INR",
        confidence=Decimal("0.95"),
        requires_approval=False,
        status=ActionProposalStatus.PROPOSED,
        created_by="test",
    )
    db_session.add(proposal)
    db_session.flush()

    # Before seed: no policy → REJECT
    decision_before = PolicyEngine().evaluate(
        proposal=proposal,
        incident=incident,
        evidence=[],
        policy=None,
    )
    assert decision_before.outcome == PolicyOutcome.REJECT

    # After seed: policy exists → engine returns the policy decision
    seed_demo_policies(db_session)
    from app.repositories.action_policy import ActionPolicyRepository

    policy = ActionPolicyRepository().get_policy(
        db_session, "RECONCILE_ADJUSTMENT"
    )
    decision_after = PolicyEngine().evaluate(
        proposal=proposal,
        incident=incident,
        evidence=[],
        policy=policy,
    )
    assert decision_after.outcome == PolicyOutcome.REJECT
    assert decision_after.reason == decision_before.reason


def test_no_financial_state_mutation(db_session: Session) -> None:
    """7. Seeding the policy does not mutate any financial entities."""
    from app.models import (
        BankTransaction,
        FinancialEvent,
        Payment,
        Refund,
        Settlement,
    )

    # Record initial counts.
    payment_count = db_session.scalar(
        select(func.count()).select_from(Payment)
    )
    refund_count = db_session.scalar(
        select(func.count()).select_from(Refund)
    )
    settlement_count = db_session.scalar(
        select(func.count()).select_from(Settlement)
    )
    event_count = db_session.scalar(
        select(func.count()).select_from(FinancialEvent)
    )
    bank_count = db_session.scalar(
        select(func.count()).select_from(BankTransaction)
    )

    seed_demo_policies(db_session)

    assert db_session.scalar(select(func.count()).select_from(Payment)) == payment_count
    assert db_session.scalar(select(func.count()).select_from(Refund)) == refund_count
    assert db_session.scalar(select(func.count()).select_from(Settlement)) == settlement_count
    assert db_session.scalar(select(func.count()).select_from(FinancialEvent)) == event_count
    assert db_session.scalar(select(func.count()).select_from(BankTransaction)) == bank_count
