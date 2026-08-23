from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus, RefundStatus, SettlementStatus
from app.database.database import engine
from app.engines.controls import ControlConfig, ControlEngine, EvidenceReference, PaymentConflictInput
from app.models import BankTransaction, FinancialEvent, Incident, IncidentEvidence, Payment, PaymentStateTransition, Refund, Settlement
from app.repositories.incidents import IncidentEvidenceRepository
from app.services.incidents import IncidentDetectionService


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


def payment(db: Session, *, status: PaymentStatus = PaymentStatus.CAPTURED, amount: int = 1250) -> Payment:
    value = Payment(provider_payment_id=f"pay_control_{uuid4()}", amount=amount, currency="INR", status=status)
    db.add(value)
    db.flush()
    return value


def event(db: Session, event_type: str, occurred_at: datetime) -> FinancialEvent:
    value = FinancialEvent(
        source="RAZORPAY",
        external_event_id=f"evt_control_{uuid4()}",
        event_type=event_type,
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(minutes=1),
        signature_valid=True,
        raw_payload={"event": event_type},
    )
    db.add(value)
    db.flush()
    return value


def test_payment_state_conflict_creates_incident_with_evidence(db_session: Session) -> None:
    current = payment(db_session)
    source_event = event(db_session, "payment.authorized", now())
    service = IncidentDetectionService()

    incident = service.detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)

    assert incident is not None
    assert incident.incident_type is IncidentType.PAYMENT_STATE_CONFLICT
    assert incident.status is IncidentStatus.OPEN
    evidence = list(db_session.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id)))
    assert {item.entity_type for item in evidence} == {"Payment", "FinancialEvent"}


def test_payment_state_conflict_is_idempotent(db_session: Session) -> None:
    current = payment(db_session)
    source_event = event(db_session, "payment.authorized", now())
    service = IncidentDetectionService()

    first = service.detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)
    second = service.detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)

    assert first is not None and second is not None and first.id == second.id
    assert db_session.scalar(select(func.count()).select_from(Incident)) == 1
    assert db_session.scalar(select(func.count()).select_from(IncidentEvidence)) == 2


def test_payment_signal_overdue_uses_configured_window(db_session: Session) -> None:
    authorized_at = now()
    current = payment(db_session, status=PaymentStatus.AUTHORIZED)
    current.authorized_at = authorized_at
    service = IncidentDetectionService(controls=ControlEngine(ControlConfig(payment_capture_timeout=timedelta(hours=2))))

    assert service.detect_payment_signal_overdue(db_session, detected_at=authorized_at + timedelta(hours=1)) == []
    findings = service.detect_payment_signal_overdue(db_session, detected_at=authorized_at + timedelta(hours=2, seconds=1))

    assert len(findings) == 1
    assert findings[0].incident_type is IncidentType.PAYMENT_SIGNAL_OVERDUE
    assert findings[0].financial_exposure == current.amount


def test_settlement_discrepancy_exposure_and_evidence(db_session: Session) -> None:
    settlement = Settlement(provider_settlement_id=f"setl_{uuid4()}", amount=900, currency="INR", fees=0, tax=0, status=SettlementStatus.PROCESSED)
    db_session.add(settlement)
    db_session.flush()
    service = IncidentDetectionService()

    incident = service.detect_settlement_discrepancy(db_session, settlement, detected_at=now(), expected_amount=1000)

    assert incident is not None
    assert incident.financial_exposure == 100
    assert "difference is 100" in (incident.description or "")
    assert db_session.scalar(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id)) is not None


def test_refund_financial_drift_is_detected(db_session: Session) -> None:
    current = payment(db_session, amount=100)
    refund = Refund(provider_refund_id=f"rfnd_{uuid4()}", payment_id=current.id, amount=150, currency="INR", status=RefundStatus.PROCESSED)
    db_session.add(refund)
    db_session.flush()

    findings = IncidentDetectionService().detect_refund_financial_drift(db_session, detected_at=now())

    assert len(findings) == 1
    assert findings[0].incident_type is IncidentType.REFUND_FINANCIAL_DRIFT
    assert findings[0].financial_exposure == 150


def test_event_integrity_only_reports_material_projection_mismatch(db_session: Session) -> None:
    current = payment(db_session, status=PaymentStatus.CAPTURED)
    source_event = event(db_session, "payment.captured", now())
    transition = PaymentStateTransition(
        payment_id=current.id,
        financial_event_id=source_event.id,
        from_state=PaymentStatus.AUTHORIZED,
        to_state=PaymentStatus.AUTHORIZED,
        occurred_at=now(),
    )
    db_session.add(transition)
    db_session.flush()
    service = IncidentDetectionService()

    findings = service.detect_event_integrity(db_session, current)

    assert len(findings) == 1
    assert findings[0].incident_type is IncidentType.EVENT_INTEGRITY


def test_settlement_credit_delay_respects_window_and_matching_credit(db_session: Session) -> None:
    processed_at = now()
    settlement = Settlement(
        provider_settlement_id=f"setl_{uuid4()}", amount=1000, currency="INR", fees=0, tax=0,
        status=SettlementStatus.PROCESSED, processed_at=processed_at, utr="UTR-1",
    )
    db_session.add(settlement)
    db_session.flush()
    service = IncidentDetectionService(controls=ControlEngine(ControlConfig(settlement_credit_timeout=timedelta(hours=1))))
    detected_at = processed_at + timedelta(hours=2)

    assert service.detect_settlement_credit_delay(db_session, detected_at=detected_at)
    db_session.add(BankTransaction(
        external_transaction_id=f"bank_{uuid4()}", transaction_type="CREDIT", amount=1000,
        currency="INR", utr="UTR-1", transaction_at=detected_at, source="synthetic",
    ))
    db_session.flush()

    assert service.detect_settlement_credit_delay(db_session, detected_at=detected_at) == []


def test_financial_exposure_aggregates_without_double_counting(db_session: Session) -> None:
    current = payment(db_session, amount=40000)
    first = Incident(
        incident_code=f"TEST-A:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.HIGH, status=IncidentStatus.OPEN, title="A", financial_exposure=40000,
        currency="INR", detected_at=now(),
    )
    duplicate = Incident(
        incident_code=f"TEST-B:{uuid4()}", incident_type=IncidentType.SETTLEMENT_CREDIT_DELAY,
        severity=IncidentSeverity.HIGH, status=IncidentStatus.OPEN, title="B", financial_exposure=40000,
        currency="INR", detected_at=now(),
    )
    db_session.add_all((first, duplicate))
    db_session.flush()
    db_session.add_all(
        (
            IncidentEvidence(incident_id=first.id, evidence_type="payment", entity_type="Payment", entity_id=str(current.id), relationship="caused_by"),
            IncidentEvidence(incident_id=duplicate.id, evidence_type="payment", entity_type="Payment", entity_id=str(current.id), relationship="caused_by"),
        )
    )
    db_session.flush()

    exposure = IncidentDetectionService().detect_financial_exposure(db_session, detected_at=now())

    assert exposure is not None
    assert exposure.financial_exposure == 40000
    assert exposure.severity is IncidentSeverity.HIGH


def test_incident_severity_is_deterministic(db_session: Session) -> None:
    current = payment(db_session, amount=100000)
    source_event = event(db_session, "payment.authorized", now())

    incident = IncidentDetectionService().detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)

    assert incident is not None and incident.severity is IncidentSeverity.CRITICAL


def test_existing_resolved_incident_reopens_without_duplication(db_session: Session) -> None:
    current = payment(db_session)
    source_event = event(db_session, "payment.authorized", now())
    service = IncidentDetectionService()
    first = service.detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)
    assert first is not None
    first.status = IncidentStatus.RESOLVED
    first.resolved_at = now()
    db_session.flush()

    reopened = service.detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)

    assert reopened is not None and reopened.id == first.id
    assert reopened.status is IncidentStatus.OPEN
    assert reopened.resolved_at is None


class FailingEvidenceRepository(IncidentEvidenceRepository):
    def add(self, db: Session, evidence: IncidentEvidence) -> None:
        raise RuntimeError("evidence write failed")


def test_evidence_failure_rolls_back_incident(db_session: Session) -> None:
    current = payment(db_session)
    source_event = event(db_session, "payment.authorized", now())
    service = IncidentDetectionService(evidence=FailingEvidenceRepository())

    with pytest.raises(RuntimeError, match="evidence write failed"):
        service.detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)

    assert db_session.scalar(select(func.count()).select_from(Incident)) == 0
    assert db_session.scalar(select(func.count()).select_from(IncidentEvidence)) == 0


def test_controls_do_not_mutate_payment_projection(db_session: Session) -> None:
    current = payment(db_session, status=PaymentStatus.CAPTURED)
    source_event = event(db_session, "payment.authorized", now())
    original_status = current.status

    IncidentDetectionService().detect_payment_state_conflict(db_session, current, source_event, PaymentStatus.AUTHORIZED)

    db_session.refresh(current)
    assert current.status is original_status
    assert current.status is PaymentStatus.CAPTURED


def test_control_engine_can_disable_a_control() -> None:
    engine = ControlEngine(ControlConfig(enabled_controls=frozenset()))
    finding = engine.payment_signal_overdue(
        Payment(provider_payment_id="pay_disabled", amount=1, currency="INR", status=PaymentStatus.AUTHORIZED, authorized_at=now()),
        detected_at=now() + timedelta(days=2),
    )
    assert finding is None


def test_timezone_aware_finding_timestamp() -> None:
    current = Payment(provider_payment_id="pay_timezone", amount=1, currency="INR", status=PaymentStatus.AUTHORIZED, authorized_at=now())
    finding = ControlEngine().payment_signal_overdue(current, detected_at=now() + timedelta(days=2))
    assert finding is not None and finding.detected_at.tzinfo is not None
