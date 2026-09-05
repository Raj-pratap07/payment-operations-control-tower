"""Focused tests for the APPROVED -> EXECUTE -> VERIFY -> RESOLVE flow.

Currencies use USD so the currency-global expected-settlement projection is
isolated from the live INR settlement-discrepancy demo data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.enums import (
    ActionProposalStatus,
    ApprovalStatus,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
    PaymentStatus,
    SettlementStatus,
)
from app.database.database import SessionLocal, engine
from app.engines.controls import ControlEngine
from app.main import app
from app.models import (
    ActionExecution,
    ActionProposal,
    Approval,
    AuditEvent,
    FinancialEvent,
    Incident,
    IncidentEvidence,
    Payment,
    Policy,
    Refund,
    Settlement,
)
from app.services.executor import ActionExecutor
from app.services.verification import VerificationOutcome, VerificationService


EXPECTED = 100_000  # USD minor units: single CAPTURED USD payment
OBSERVED = 90_000  # settlement starts below expected => discrepancy of 10_000
ADJUSTMENT = 10_000


class Scenario:
    """A live-DB-isolated SETTLEMENT_DISCREPANCY scenario in USD."""

    def __init__(self, db: Session) -> None:
        now = datetime.now(UTC)
        self.incident = Incident(
            incident_code=f"EVF-{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
            severity=IncidentSeverity.MEDIUM, status=IncidentStatus.OPEN, title="Reconciliation flow",
            financial_exposure=ADJUSTMENT, currency="USD", detected_at=now,
        )
        self.payment = Payment(
            provider_payment_id=f"pay-evf-{uuid4()}", amount=EXPECTED, currency="USD",
            status=PaymentStatus.CAPTURED, captured_at=now,
        )
        self.settlement = Settlement(
            provider_settlement_id=f"set-evf-{uuid4()}", amount=OBSERVED, currency="USD",
            fees=0, tax=0, status=SettlementStatus.PROCESSED, processed_at=now,
        )
        self.event = FinancialEvent(
            source="EVF_TEST", external_event_id=f"evt-evf-{uuid4()}", event_type="payment.captured",
            occurred_at=now, received_at=now, signature_valid=True, raw_payload={"amount": EXPECTED},
        )
        db.add_all((self.incident, self.payment, self.settlement, self.event))
        db.flush()
        db.add_all((
            IncidentEvidence(
                incident_id=self.incident.id, evidence_type="financial",
                entity_type="Settlement", entity_id=str(self.settlement.id), relationship="observed_amount",
            ),
            IncidentEvidence(
                incident_id=self.incident.id, evidence_type="financial",
                entity_type="Payment", entity_id=str(self.payment.id), relationship="expected_amount",
            ),
            Policy(
                name=f"Policy {uuid4()}", action_type="RECONCILE_ADJUSTMENT", max_amount=1_000_000,
                min_confidence=Decimal("0.9000"), requires_approval=True, is_active=True,
            ),
        ))
        self.proposal = ActionProposal(
            incident_id=self.incident.id, action_type="RECONCILE_ADJUSTMENT",
            description="Apply approved reconciliation adjustment.", amount=ADJUSTMENT, currency="USD",
            confidence=Decimal("0.9500"), requires_approval=True, status=ActionProposalStatus.PROPOSED,
            created_by="test",
        )
        db.add(self.proposal)
        db.flush()
        self.approval = Approval(action_proposal_id=self.proposal.id, requested_by="ops", status=ApprovalStatus.PENDING, requested_at=now)
        db.add(self.approval)
        db.flush()

    def approve(self, db: Session) -> None:
        self.approval.status = ApprovalStatus.APPROVED
        self.approval.approved_by = "operator-demo"
        self.approval.approved_at = datetime.now(UTC)
        self.proposal.status = ActionProposalStatus.APPROVED
        db.flush()


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


def execution_ids(db: Session, proposal_id: object) -> list[str]:
    return list(db.scalars(select(ActionExecution.id).where(ActionExecution.action_proposal_id == proposal_id)))


def test_approved_reconcile_proposal_executes_and_applies_adjustment(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    result = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)

    assert result.success is True
    assert result.execution is not None
    assert result.execution.status == "EXECUTED"
    assert result.execution.verified_at is None
    assert result.verification is None
    assert result.execution.provider_reference is None
    assert scenario.proposal.status is ActionProposalStatus.EXECUTED
    db_session.expire_all()
    assert db_session.get(Settlement, scenario.settlement.id).amount == EXPECTED


def test_unapproved_proposal_is_rejected(db_session: Session) -> None:
    scenario = Scenario(db_session)

    result = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)

    assert result.success is False
    assert result.execution is None
    assert "approval" in result.reason.lower()
    assert execution_ids(db_session, scenario.proposal.id) == []
    assert scenario.incident.status is IncidentStatus.OPEN


@pytest.mark.parametrize("terminal_status", [
    ActionProposalStatus.REJECTED,
    ActionProposalStatus.FAILED,
    ActionProposalStatus.CANCELLED,
])
def test_terminal_proposal_states_cannot_execute(db_session: Session, terminal_status: ActionProposalStatus) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)
    scenario.proposal.status = terminal_status
    db_session.flush()

    result = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)

    assert result.success is False
    assert result.execution is None
    assert execution_ids(db_session, scenario.proposal.id) == []
    assert scenario.incident.status is IncidentStatus.OPEN


def test_repeated_execution_is_idempotent(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    first = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)
    second = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)

    assert first.success is True and second.success is True
    assert first.execution is not None and second.execution is not None
    assert first.execution.id == second.execution.id
    assert execution_ids(db_session, scenario.proposal.id) == [first.execution.id]


def test_verification_before_execution_is_rejected(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    verification = VerificationService().verify_execution(db_session, f"action-proposal:{scenario.proposal.id}")

    assert verification.passed is False
    assert verification.outcome == VerificationOutcome.VERIFICATION_FAILED
    assert "not found" in verification.reason.lower()


def test_execution_then_verification_resolves_incident(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    executed = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)
    assert executed.success is True

    recomputed = VerificationService().verify_execution(db_session, f"action-proposal:{scenario.proposal.id}")
    assert recomputed.passed is True
    assert recomputed.outcome == VerificationOutcome.VERIFIED_RESOLVED
    db_session.expire_all()
    assert scenario.incident.status is IncidentStatus.RESOLVED
    assert scenario.incident.resolved_at is not None


def test_incident_resolves_only_after_successful_verification(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)
    db_session.expire_all()
    # Execution alone must NOT resolve the incident; only verification may.
    assert scenario.incident.status is IncidentStatus.OPEN
    assert scenario.incident.resolved_at is None

    VerificationService().verify_execution(db_session, f"action-proposal:{scenario.proposal.id}")
    db_session.expire_all()
    assert scenario.incident.status is IncidentStatus.RESOLVED


def test_reconcile_adjustment_clears_the_originating_control(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)
    controls = ControlEngine()
    now = datetime.now(UTC)

    assert controls.settlement_discrepancy(scenario.settlement, expected_amount=EXPECTED, detected_at=now) is not None
    ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)
    db_session.expire_all()
    assert db_session.get(Settlement, scenario.settlement.id).amount == EXPECTED
    assert controls.settlement_discrepancy(scenario.settlement, expected_amount=EXPECTED, detected_at=now) is None


def test_audit_events_record_execution_and_verification(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    result = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)
    VerificationService().verify_execution(db_session, f"action-proposal:{scenario.proposal.id}")

    recorded = set(db_session.scalars(select(AuditEvent.action_type)))
    assert {
        "RECONCILE_ADJUSTMENT_APPLIED", "EXECUTION_STARTED", "EXECUTION_COMPLETED",
        "VERIFICATION_PASSED", "INCIDENT_RESOLVED",
    }.issubset(recorded)
    assert "EXECUTION_FAILED" not in recorded
    assert result.execution is not None


def test_financial_raw_events_remain_immutable(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.event.raw_payload = {"amount": 999_999}
    with pytest.raises(Exception):
        db_session.flush()


def test_execution_makes_no_live_provider_mutation(db_session: Session) -> None:
    scenario = Scenario(db_session)
    scenario.approve(db_session)

    result = ActionExecutor().execute(db_session, scenario.proposal.id, verify=False)

    assert result.execution is not None
    assert result.execution.provider_reference is None
    assert db_session.scalar(select(func.count()).select_from(Refund).where(Refund.payment_id == scenario.payment.id)) == 0
    assert scenario.settlement.amount == EXPECTED


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def _clean_api_rows(db: Session, incident: Incident, action: ActionProposal, settlement: Settlement, payment: Payment, approval: Approval) -> None:
    db.execute(delete(AuditEvent).where(
        (AuditEvent.entity_id.in_([str(action.id), str(settlement.id), str(approval.id)]))
        | (AuditEvent.metadata_["incident_id"].astext == str(incident.id))
    ))
    db.execute(delete(ActionExecution).where(ActionExecution.action_proposal_id == action.id))
    db.execute(delete(Approval).where(Approval.action_proposal_id == action.id))
    db.execute(delete(ActionProposal).where(ActionProposal.id == action.id))
    db.execute(delete(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id))
    db.execute(delete(Settlement).where(Settlement.id == settlement.id))
    db.execute(delete(Payment).where(Payment.id == payment.id))
    db.execute(delete(Incident).where(Incident.id == incident.id))
    db.commit()


@pytest.fixture
def api_reconcile() -> dict[str, object]:
    now = datetime.now(UTC)
    incident = Incident(
        incident_code=f"EVFAPI:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM, status=IncidentStatus.OPEN, title="API reconcile flow",
        financial_exposure=ADJUSTMENT, currency="USD", detected_at=now,
    )
    payment = Payment(provider_payment_id=f"pay-api-evf-{uuid4()}", amount=EXPECTED, currency="USD", status=PaymentStatus.CAPTURED, captured_at=now)
    settlement = Settlement(provider_settlement_id=f"set-api-evf-{uuid4()}", amount=OBSERVED, currency="USD", fees=0, tax=0, status=SettlementStatus.PROCESSED, processed_at=now)
    db = SessionLocal()
    db.add_all((incident, payment, settlement))
    db.flush()
    db.add_all((
        IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Settlement", entity_id=str(settlement.id), relationship="observed_amount"),
        IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Payment", entity_id=str(payment.id), relationship="expected_amount"),
    ))
    action = ActionProposal(
        incident_id=incident.id, action_type="RECONCILE_ADJUSTMENT", description="API reconcile",
        amount=ADJUSTMENT, currency="USD", confidence=Decimal("0.95"), requires_approval=True,
        status=ActionProposalStatus.PROPOSED, created_by="test",
    )
    db.add(action)
    db.flush()
    approval = Approval(action_proposal_id=action.id, requested_by="ops", status=ApprovalStatus.PENDING, requested_at=now)
    db.add(approval)
    db.commit()
    values = {"incident": incident, "payment": payment, "settlement": settlement, "action": action, "approval": approval}
    try:
        yield values
    finally:
        _clean_api_rows(db, incident, action, settlement, payment, approval)
        db.close()


def test_api_execute_requires_approved_proposal(client: TestClient, api_reconcile: dict[str, object]) -> None:
    action = api_reconcile["action"]
    response = client.post(f"/actions/{action.id}/execute")
    assert response.status_code == 409
    assert "approval" in response.json()["detail"].lower()


def test_api_verify_before_execution_is_rejected(client: TestClient, api_reconcile: dict[str, object]) -> None:
    action = api_reconcile["action"]
    response = client.post(f"/actions/{action.id}/verify")
    assert response.status_code == 409
    assert "not been executed" in response.json()["detail"]


def test_api_execute_then_verify_resolves_incident(client: TestClient, api_reconcile: dict[str, object]) -> None:
    action = api_reconcile["action"]
    db = SessionLocal()
    approval = db.get(Approval, api_reconcile["approval"].id)
    assert approval is not None
    proposal = db.get(ActionProposal, action.id)
    assert proposal is not None
    approval.status = ApprovalStatus.APPROVED
    approval.approved_by = "operator-demo"
    approval.approved_at = datetime.now(UTC)
    proposal.status = ActionProposalStatus.APPROVED
    db.commit()
    db.close()

    executed = client.post(f"/actions/{action.id}/execute")
    assert executed.status_code == 200
    body = executed.json()
    assert body["success"] is True
    assert body["execution"]["status"] == "EXECUTED"
    assert body["execution"]["verified_at"] is None
    assert body["action"]["executions"]

    verified = client.post(f"/actions/{action.id}/verify")
    assert verified.status_code == 200
    vbody = verified.json()
    assert vbody["success"] is True
    assert vbody["verification"]["outcome"] == "VERIFIED_RESOLVED"
    assert vbody["verification"]["passed"] is True

    db = SessionLocal()
    assert db.get(Incident, api_reconcile["incident"].id).status is IncidentStatus.RESOLVED
    assert db.get(Settlement, api_reconcile["settlement"].id).amount == EXPECTED
    db.close()