from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import PaymentStatus, RefundStatus, SettlementStatus
from app.database.database import engine
from app.models import FinancialEvent, Payment, PaymentStateTransition, Refund, Settlement
from app.schemas.canonical_events import (
    CanonicalAggregateType,
    CanonicalEvent,
    CanonicalEventType,
    PaymentEventData,
    RefundEventData,
    SettlementEventData,
)
from app.services.projection import (
    InconsistentFinancialIdentifierError,
    InvalidStateTransitionError,
    MissingPaymentError,
    ProjectionService,
    UnsupportedProjectionError,
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


@pytest.fixture
def projector() -> ProjectionService:
    return ProjectionService()


def persist_event(db: Session, event_type: str, occurred_at: datetime) -> FinancialEvent:
    event = FinancialEvent(
        source="RAZORPAY",
        external_event_id=f"evt_projection_{uuid4()}",
        event_type=event_type,
        occurred_at=occurred_at,
        received_at=occurred_at + timedelta(minutes=1),
        signature_valid=True,
        raw_payload={"event": event_type},
    )
    db.add(event)
    db.flush()
    return event


def payment_event(
    db: Session,
    event_type: CanonicalEventType,
    occurred_at: datetime,
    payment_id: str = "pay_projection",
) -> CanonicalEvent:
    source_event = persist_event(db, event_type.value.lower(), occurred_at)
    return CanonicalEvent(
        event_id=source_event.id,
        source="RAZORPAY",
        event_type=event_type,
        occurred_at=occurred_at,
        aggregate_type=CanonicalAggregateType.PAYMENT,
        aggregate_id=payment_id,
        data=PaymentEventData(payment_id, "order_projection", 1250, "INR"),
    )


def refund_event(
    db: Session,
    event_type: CanonicalEventType,
    occurred_at: datetime,
    refund_id: str = "rfnd_projection",
) -> CanonicalEvent:
    source_event = persist_event(db, event_type.value.lower(), occurred_at)
    return CanonicalEvent(
        event_id=source_event.id,
        source="RAZORPAY",
        event_type=event_type,
        occurred_at=occurred_at,
        aggregate_type=CanonicalAggregateType.REFUND,
        aggregate_id=refund_id,
        data=RefundEventData(refund_id, "pay_projection", 500, "INR"),
    )


def settlement_event(db: Session, occurred_at: datetime) -> CanonicalEvent:
    source_event = persist_event(db, "settlement.processed", occurred_at)
    return CanonicalEvent(
        event_id=source_event.id,
        source="RAZORPAY",
        event_type=CanonicalEventType.SETTLEMENT_PROCESSED,
        occurred_at=occurred_at,
        aggregate_type=CanonicalAggregateType.SETTLEMENT,
        aggregate_id="setl_projection",
        data=SettlementEventData("setl_projection", 1000, "INR", 20, 4, "UTR-PROJECTION"),
    )


def create_payment(db: Session, projector: ProjectionService, occurred_at: datetime) -> None:
    projector.project(db, payment_event(db, CanonicalEventType.PAYMENT_AUTHORIZED, occurred_at))


def test_payment_authorized_creates_payment_and_transition(db_session: Session, projector: ProjectionService) -> None:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    event = payment_event(db_session, CanonicalEventType.PAYMENT_AUTHORIZED, occurred_at)

    projector.project(db_session, event)

    payment = db_session.scalar(select(Payment).where(Payment.provider_payment_id == "pay_projection"))
    transition = db_session.scalar(select(PaymentStateTransition).where(PaymentStateTransition.financial_event_id == event.event_id))
    assert payment is not None
    assert payment.status is PaymentStatus.AUTHORIZED
    assert payment.authorized_at == occurred_at
    assert transition is not None
    assert transition.from_state is None
    assert transition.to_state is PaymentStatus.AUTHORIZED
    assert transition.financial_event_id == event.event_id


def test_payment_captured_updates_payment_and_creates_transition(db_session: Session, projector: ProjectionService) -> None:
    authorized_at = datetime(2026, 1, 1, tzinfo=UTC)
    captured_at = authorized_at + timedelta(minutes=1)
    create_payment(db_session, projector, authorized_at)
    event = payment_event(db_session, CanonicalEventType.PAYMENT_CAPTURED, captured_at)

    projector.project(db_session, event)

    payment = db_session.scalar(select(Payment).where(Payment.provider_payment_id == "pay_projection"))
    assert payment is not None
    assert payment.status is PaymentStatus.CAPTURED
    assert payment.captured_at == captured_at
    assert db_session.scalar(select(func.count()).select_from(PaymentStateTransition)) == 2


def test_payment_failed_updates_payment(db_session: Session, projector: ProjectionService) -> None:
    authorized_at = datetime(2026, 1, 1, tzinfo=UTC)
    failed_at = authorized_at + timedelta(minutes=1)
    create_payment(db_session, projector, authorized_at)
    projector.project(db_session, payment_event(db_session, CanonicalEventType.PAYMENT_FAILED, failed_at))

    payment = db_session.scalar(select(Payment).where(Payment.provider_payment_id == "pay_projection"))
    assert payment is not None
    assert payment.status is PaymentStatus.FAILED
    assert payment.failed_at == failed_at


def test_refund_lifecycle_populates_source_event_links(db_session: Session, projector: ProjectionService) -> None:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    create_payment(db_session, projector, occurred_at)
    created = refund_event(db_session, CanonicalEventType.REFUND_CREATED, occurred_at + timedelta(minutes=1))
    processed = refund_event(db_session, CanonicalEventType.REFUND_PROCESSED, occurred_at + timedelta(minutes=2))

    projector.project(db_session, created)
    projector.project(db_session, processed)

    refund = db_session.scalar(select(Refund).where(Refund.provider_refund_id == "rfnd_projection"))
    assert refund is not None
    assert refund.status is RefundStatus.PROCESSED
    assert refund.processed_at == processed.occurred_at
    assert refund.source_financial_event_id == created.event_id


def test_refund_failed_updates_refund(db_session: Session, projector: ProjectionService) -> None:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    create_payment(db_session, projector, occurred_at)
    created = refund_event(db_session, CanonicalEventType.REFUND_CREATED, occurred_at + timedelta(minutes=1))
    failed = refund_event(db_session, CanonicalEventType.REFUND_FAILED, occurred_at + timedelta(minutes=2))
    projector.project(db_session, created)
    projector.project(db_session, failed)

    refund = db_session.scalar(select(Refund).where(Refund.provider_refund_id == "rfnd_projection"))
    assert refund is not None
    assert refund.status is RefundStatus.FAILED
    assert refund.failed_at == failed.occurred_at


def test_duplicate_refund_lifecycle_events_are_idempotent(db_session: Session, projector: ProjectionService) -> None:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    create_payment(db_session, projector, occurred_at)
    created = refund_event(db_session, CanonicalEventType.REFUND_CREATED, occurred_at + timedelta(minutes=1))
    processed = refund_event(db_session, CanonicalEventType.REFUND_PROCESSED, occurred_at + timedelta(minutes=2))
    projector.project(db_session, created)

    projector.project(db_session, processed)
    projector.project(db_session, processed)
    projector.project(db_session, created)

    assert db_session.scalar(select(func.count()).select_from(Refund)) == 1
    refund = db_session.scalar(select(Refund).where(Refund.provider_refund_id == "rfnd_projection"))
    assert refund is not None and refund.source_financial_event_id == created.event_id


def test_settlement_processed_creates_projection_without_payment_link(db_session: Session, projector: ProjectionService) -> None:
    event = settlement_event(db_session, datetime(2026, 1, 1, tzinfo=UTC))

    projector.project(db_session, event)

    settlement = db_session.scalar(select(Settlement).where(Settlement.provider_settlement_id == "setl_projection"))
    assert settlement is not None
    assert (settlement.amount, settlement.currency, settlement.fees, settlement.tax) == (1000, "INR", 20, 4)
    assert settlement.status is SettlementStatus.PROCESSED
    assert settlement.processed_at == event.occurred_at
    assert settlement.source_financial_event_id == event.event_id


def test_duplicate_financial_event_processing_is_idempotent(db_session: Session, projector: ProjectionService) -> None:
    event = payment_event(db_session, CanonicalEventType.PAYMENT_AUTHORIZED, datetime(2026, 1, 1, tzinfo=UTC))

    first = projector.project(db_session, event)
    second = projector.project(db_session, event)

    assert first == second
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 1
    assert db_session.scalar(select(func.count()).select_from(PaymentStateTransition)) == 1


def test_invalid_transition_is_rejected_without_overwriting_state(db_session: Session, projector: ProjectionService) -> None:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    create_payment(db_session, projector, occurred_at)
    projector.project(db_session, payment_event(db_session, CanonicalEventType.PAYMENT_CAPTURED, occurred_at + timedelta(minutes=1)))
    invalid = payment_event(db_session, CanonicalEventType.PAYMENT_AUTHORIZED, occurred_at + timedelta(minutes=2))

    with pytest.raises(InvalidStateTransitionError):
        projector.project(db_session, invalid)

    payment = db_session.scalar(select(Payment).where(Payment.provider_payment_id == "pay_projection"))
    assert payment is not None and payment.status is PaymentStatus.CAPTURED
    assert db_session.scalar(select(func.count()).select_from(PaymentStateTransition)) == 2


def test_out_of_order_event_does_not_corrupt_current_projection(db_session: Session, projector: ProjectionService) -> None:
    captured = payment_event(db_session, CanonicalEventType.PAYMENT_CAPTURED, datetime(2026, 1, 1, 0, 2, tzinfo=UTC))
    authorized = payment_event(db_session, CanonicalEventType.PAYMENT_AUTHORIZED, datetime(2026, 1, 1, 0, 1, tzinfo=UTC))
    projector.project(db_session, captured)

    with pytest.raises(InvalidStateTransitionError):
        projector.project(db_session, authorized)

    payment = db_session.scalar(select(Payment).where(Payment.provider_payment_id == "pay_projection"))
    assert payment is not None and payment.status is PaymentStatus.CAPTURED


def test_projection_failure_is_atomic(db_session: Session, projector: ProjectionService) -> None:
    event = refund_event(db_session, CanonicalEventType.REFUND_PROCESSED, datetime(2026, 1, 1, tzinfo=UTC))

    with pytest.raises(MissingPaymentError):
        projector.project(db_session, event)

    assert db_session.scalar(select(func.count()).select_from(Refund)) == 0
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0


def test_inconsistent_payment_identifiers_are_rejected(db_session: Session, projector: ProjectionService) -> None:
    occurred_at = datetime(2026, 1, 1, tzinfo=UTC)
    create_payment(db_session, projector, occurred_at)
    event = payment_event(db_session, CanonicalEventType.PAYMENT_CAPTURED, occurred_at + timedelta(minutes=1))
    assert isinstance(event.data, PaymentEventData)
    event = CanonicalEvent(
        event_id=event.event_id,
        source=event.source,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        data=PaymentEventData("pay_projection", "order_projection", 999, "INR"),
    )

    with pytest.raises(InconsistentFinancialIdentifierError):
        projector.project(db_session, event)


def test_unsupported_canonical_event_is_rejected(db_session: Session, projector: ProjectionService) -> None:
    source_event = persist_event(db_session, "unknown", datetime(2026, 1, 1, tzinfo=UTC))
    event = CanonicalEvent(
        event_id=source_event.id,
        source="RAZORPAY",
        event_type="UNKNOWN",  # type: ignore[arg-type]
        occurred_at=source_event.occurred_at,
        aggregate_type=CanonicalAggregateType.PAYMENT,
        aggregate_id="pay_projection",
        data=PaymentEventData("pay_projection", None, 1, "INR"),
    )

    with pytest.raises(UnsupportedProjectionError):
        projector.project(db_session, event)


def test_projection_money_remains_integer(db_session: Session, projector: ProjectionService) -> None:
    projector.project(db_session, payment_event(db_session, CanonicalEventType.PAYMENT_AUTHORIZED, datetime(2026, 1, 1, tzinfo=UTC)))
    payment = db_session.scalar(select(Payment).where(Payment.provider_payment_id == "pay_projection"))
    assert payment is not None and isinstance(payment.amount, int)
