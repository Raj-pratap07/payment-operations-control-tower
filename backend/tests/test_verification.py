from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus, IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus, RefundStatus, SettlementStatus
from app.database.database import engine
from app.models import ActionExecution, ActionProposal, AuditEvent, BankTransaction, FinancialEvent, Incident, IncidentEvidence, Payment, PaymentStateTransition, Refund, Settlement
from app.services.verification import VerificationOutcome, VerificationService


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
    return datetime.now(UTC).replace(microsecond=0)


def base_action(db: Session, incident_type: IncidentType) -> tuple[Incident, ActionProposal]:
    incident = Incident(
        incident_code=f"VERIFY:{uuid4()}", incident_type=incident_type,
        severity=IncidentSeverity.HIGH, status=IncidentStatus.OPEN,
        title="Verification incident", description="Test condition", financial_exposure=100,
        currency="INR", detected_at=now(),
    )
    proposal = ActionProposal(
        incident=incident, action_type="RECONCILE_ADJUSTMENT", description="Reconcile",
        amount=100, currency="INR", confidence="0.95", requires_approval=False,
        status=ActionProposalStatus.EXECUTED, created_by="test",
    )
    db.add(proposal)
    db.flush()
    return incident, proposal


def execution(db: Session, proposal: ActionProposal, incident: Incident) -> ActionExecution:
    value = ActionExecution(
        action_proposal_id=proposal.id, execution_id=f"verify:{proposal.id}", status="EXECUTED",
        after_state={"action_type": "RECONCILE_ADJUSTMENT", "incident_id": str(incident.id), "adjustment_recorded": True},
    )
    db.add(value)
    db.flush()
    return value


def settlement_case(db: Session, amount: int) -> tuple[Incident, ActionExecution, Settlement]:
    incident, proposal = base_action(db, IncidentType.SETTLEMENT_DISCREPANCY)
    payment = Payment(provider_payment_id=f"pay-{uuid4()}", amount=1000, currency="INR", status=PaymentStatus.CAPTURED)
    settlement = Settlement(provider_settlement_id=f"setl-{uuid4()}", amount=amount, currency="INR", fees=0, tax=0, status=SettlementStatus.PROCESSED)
    db.add_all((payment, settlement))
    db.flush()
    db.add(IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Settlement", entity_id=str(settlement.id), relationship="derived_from"))
    db.flush()
    return incident, execution(db, proposal, incident), settlement


def test_settlement_discrepancy_resolves_when_control_clears(db_session: Session) -> None:
    incident, action, settlement = settlement_case(db_session, 900)
    service = VerificationService()
    settlement.amount = 1000
    db_session.flush()

    result = service.verify_execution(db_session, action.execution_id)

    assert result.outcome is VerificationOutcome.VERIFIED_RESOLVED
    assert result.passed is True
    assert incident.status is IncidentStatus.RESOLVED
    assert action.verified_at is not None


def test_settlement_discrepancy_remains_active_when_control_triggers(db_session: Session) -> None:
    incident, action, _ = settlement_case(db_session, 900)

    result = VerificationService().verify_execution(db_session, action.execution_id)

    assert result.outcome is VerificationOutcome.VERIFIED_STILL_ACTIVE
    assert result.passed is False
    assert incident.status is IncidentStatus.ACTION_REQUIRED


def test_refund_drift_verification(db_session: Session) -> None:
    incident, proposal = base_action(db_session, IncidentType.REFUND_FINANCIAL_DRIFT)
    payment = Payment(provider_payment_id=f"pay-{uuid4()}", amount=100, currency="INR", status=PaymentStatus.CAPTURED)
    db_session.add(payment)
    db_session.flush()
    refund = Refund(provider_refund_id=f"rfnd-{uuid4()}", payment_id=payment.id, amount=150, currency="INR", status=RefundStatus.PROCESSED)
    db_session.add(refund)
    db_session.flush()
    db_session.add_all(
        (
            IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Payment", entity_id=str(payment.id), relationship="compared_with"),
            IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Refund", entity_id=str(refund.id), relationship="causes_drift"),
        )
    )
    db_session.flush()
    action = execution(db_session, proposal, incident)

    result = VerificationService().verify_execution(db_session, action.execution_id)

    assert result.outcome is VerificationOutcome.VERIFIED_STILL_ACTIVE
    assert incident.status is IncidentStatus.ACTION_REQUIRED


def test_credit_delay_resolves_when_matching_credit_appears(db_session: Session) -> None:
    incident, proposal = base_action(db_session, IncidentType.SETTLEMENT_CREDIT_DELAY)
    processed_at = now() - timedelta(days=3)
    settlement = Settlement(provider_settlement_id=f"setl-{uuid4()}", amount=1000, currency="INR", fees=0, tax=0, utr="UTR-1", status=SettlementStatus.PROCESSED, processed_at=processed_at)
    db_session.add(settlement)
    db_session.flush()
    db_session.add(IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Settlement", entity_id=str(settlement.id), relationship="awaits_credit"))
    db_session.add(BankTransaction(external_transaction_id=f"bank-{uuid4()}", transaction_type="CREDIT", amount=1000, currency="INR", utr="UTR-1", transaction_at=processed_at + timedelta(days=1), source="synthetic"))
    db_session.flush()
    action = execution(db_session, proposal, incident)

    result = VerificationService().verify_execution(db_session, action.execution_id)

    assert result.outcome is VerificationOutcome.VERIFIED_RESOLVED
    assert incident.status is IncidentStatus.RESOLVED


def test_payment_conflict_verification(db_session: Session) -> None:
    incident, proposal = base_action(db_session, IncidentType.PAYMENT_STATE_CONFLICT)
    payment = Payment(provider_payment_id=f"pay-{uuid4()}", amount=100, currency="INR", status=PaymentStatus.CAPTURED)
    event = FinancialEvent(source="RAZORPAY", external_event_id=f"evt-{uuid4()}", event_type="payment.authorized", occurred_at=now(), received_at=now(), signature_valid=True, raw_payload={})
    db_session.add_all((payment, event))
    db_session.flush()
    transition = PaymentStateTransition(payment_id=payment.id, financial_event_id=event.id, from_state=PaymentStatus.AUTHORIZED, to_state=PaymentStatus.AUTHORIZED, occurred_at=now())
    db_session.add(transition)
    db_session.flush()
    db_session.add_all(
        (
            IncidentEvidence(incident_id=incident.id, evidence_type="state", entity_type="Payment", entity_id=str(payment.id), relationship="has_integrity_issue"),
            IncidentEvidence(incident_id=incident.id, evidence_type="state", entity_type="PaymentStateTransition", entity_id=str(transition.id), relationship="contradicts"),
        )
    )
    db_session.flush()
    action = execution(db_session, proposal, incident)

    result = VerificationService().verify_execution(db_session, action.execution_id)

    assert result.outcome is VerificationOutcome.VERIFIED_STILL_ACTIVE
    assert incident.status is IncidentStatus.ACTION_REQUIRED


def test_verification_is_idempotent_and_does_not_duplicate_audits(db_session: Session) -> None:
    incident, action, settlement = settlement_case(db_session, 1000)
    service = VerificationService()

    first = service.verify_execution(db_session, action.execution_id)
    second = service.verify_execution(db_session, action.execution_id)

    assert first == second
    assert db_session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action_type == "VERIFICATION_PASSED", AuditEvent.entity_id == str(action.id))) == 1
    assert db_session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action_type == "INCIDENT_RESOLVED", AuditEvent.entity_id == str(action.id))) == 1


def test_verification_does_not_mutate_financial_projection(db_session: Session) -> None:
    incident, action, settlement = settlement_case(db_session, 1000)
    before = (settlement.amount, settlement.status, incident.financial_exposure)

    VerificationService().verify_execution(db_session, action.execution_id)

    db_session.expire_all()
    assert (settlement.amount, settlement.status, incident.financial_exposure) == before


def test_missing_control_evidence_is_explicit_failure(db_session: Session) -> None:
    incident, proposal = base_action(db_session, IncidentType.SETTLEMENT_DISCREPANCY)
    action = execution(db_session, proposal, incident)

    result = VerificationService().verify_execution(db_session, action.execution_id)

    assert result.outcome is VerificationOutcome.VERIFICATION_FAILED
    assert result.passed is False
    assert incident.status is IncidentStatus.ACTION_REQUIRED
