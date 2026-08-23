from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import BIGINT, DateTime, inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus, RefundStatus, SettlementStatus
from app.database.base import Base
from app.database.database import engine
from app.models import AppendOnlyRecordError, AuditEvent, BankTransaction, FinancialEvent, Incident, Payment, PaymentStateTransition, Refund, Settlement


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
    return datetime.now(UTC)


def event(key: str) -> FinancialEvent:
    return FinancialEvent(source="razorpay", external_event_id=f"evt_{key}", event_type="payment.captured", occurred_at=now(), received_at=now(), signature_valid=True, raw_payload={})


def payment(key: str) -> Payment:
    return Payment(provider_payment_id=f"pay_{key}", amount=100, currency="INR", status=PaymentStatus.CAPTURED)


def incident(key: str) -> Incident:
    return Incident(incident_code=f"INC-{key}", incident_type=IncidentType.EVENT_INTEGRITY, severity=IncidentSeverity.LOW, status=IncidentStatus.OPEN, title="Integrity issue", detected_at=now())


def _config() -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    return config


def test_alembic_upgrade_downgrade_upgrade_from_empty_database() -> None:
    config = _config()
    command.downgrade(config, "base")
    assert "financial_events" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert "financial_events" in inspect(engine).get_table_names()


def test_all_v1_models_are_registered() -> None:
    assert set(Base.metadata.tables) == {"financial_events", "payments", "payment_state_transitions", "refunds", "settlements", "bank_transactions", "incidents", "incident_evidence", "policies", "action_proposals", "approvals", "action_executions", "audit_events"}


def test_required_unique_constraints_reject_duplicates(db_session: Session) -> None:
    first_event = event("duplicate")
    first_payment = payment("duplicate")
    db_session.add_all((first_event, first_payment))
    db_session.flush()
    records = (
        (event("duplicate"),),
        (payment("duplicate"),),
        (Refund(provider_refund_id="rfnd_duplicate", payment_id=first_payment.id, amount=100, currency="INR", status=RefundStatus.PROCESSED),),
        (Settlement(provider_settlement_id="setl_duplicate", amount=100, currency="INR", fees=0, tax=0, status=SettlementStatus.PROCESSED),),
        (BankTransaction(external_transaction_id="bank_duplicate", transaction_type="CREDIT", amount=100, currency="INR", transaction_at=now(), source="synthetic"),),
        (incident("duplicate"),),
    )
    for (first,) in records[2:]:
        db_session.add(first)
    db_session.flush()
    duplicates = (
        event("duplicate"),
        payment("duplicate"),
        Refund(provider_refund_id="rfnd_duplicate", payment_id=first_payment.id, amount=100, currency="INR", status=RefundStatus.PROCESSED),
        Settlement(provider_settlement_id="setl_duplicate", amount=100, currency="INR", fees=0, tax=0, status=SettlementStatus.PROCESSED),
        BankTransaction(external_transaction_id="bank_duplicate", transaction_type="CREDIT", amount=100, currency="INR", transaction_at=now(), source="synthetic"),
        incident("duplicate"),
    )
    for duplicate in duplicates:
        with pytest.raises(IntegrityError), db_session.begin_nested():
            db_session.add(duplicate)
            db_session.flush()


def test_foreign_keys_and_source_event_links_flush_to_postgresql(db_session: Session) -> None:
    source_event = event("source")
    source_payment = payment("source")
    db_session.add_all((source_event, source_payment))
    db_session.flush()
    refund = Refund(provider_refund_id="rfnd_source", payment=source_payment, source_financial_event=source_event, amount=100, currency="INR", status=RefundStatus.PROCESSED)
    settlement = Settlement(provider_settlement_id="setl_source", source_financial_event=source_event, amount=100, currency="INR", fees=0, tax=0, status=SettlementStatus.PROCESSED)
    transition = PaymentStateTransition(payment=source_payment, financial_event=source_event, to_state=PaymentStatus.CAPTURED, occurred_at=now())
    db_session.add_all((refund, settlement, transition))
    db_session.flush()
    assert refund.source_financial_event is source_event
    assert settlement.source_financial_event is source_event
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(Refund(provider_refund_id="rfnd_invalid_fk", payment_id=uuid4(), source_financial_event_id=uuid4(), amount=100, currency="INR", status=RefundStatus.PROCESSED))
        db_session.flush()


def test_types_and_timezone_requirements() -> None:
    for table_name, column_name in (("payments", "amount"), ("refunds", "amount"), ("settlements", "amount"), ("settlements", "fees"), ("settlements", "tax"), ("bank_transactions", "amount"), ("incidents", "financial_exposure")):
        assert isinstance(Base.metadata.tables[table_name].c[column_name].type, BIGINT)
    for table_name, column_name in (("financial_events", "raw_payload"), ("action_executions", "before_state"), ("action_executions", "after_state"), ("audit_events", "evidence"), ("audit_events", "metadata")):
        assert isinstance(Base.metadata.tables[table_name].c[column_name].type, JSONB)
    for table in Base.metadata.tables.values():
        assert isinstance(table.c.id.type, PG_UUID)
    for table_name, column_name in (("financial_events", "occurred_at"), ("financial_events", "received_at"), ("financial_events", "created_at"), ("payment_state_transitions", "occurred_at"), ("payment_state_transitions", "created_at"), ("audit_events", "created_at")):
        column_type = Base.metadata.tables[table_name].c[column_name].type
        assert isinstance(column_type, DateTime) and column_type.timezone is True


def test_financial_event_is_append_only(db_session: Session) -> None:
    record = event("immutable_update")
    db_session.add(record)
    db_session.flush()
    record.event_type = "payment.failed"
    with pytest.raises(AppendOnlyRecordError, match="FinancialEvent records are append-only and cannot be updated"):
        db_session.flush()


def test_financial_event_cannot_be_deleted(db_session: Session) -> None:
    record = event("immutable_delete")
    db_session.add(record)
    db_session.flush()
    db_session.delete(record)
    with pytest.raises(AppendOnlyRecordError, match="FinancialEvent records are append-only and cannot be deleted"):
        db_session.flush()


def test_payment_state_transition_is_append_only(db_session: Session) -> None:
    source_event, source_payment = event("transition"), payment("transition")
    record = PaymentStateTransition(payment=source_payment, financial_event=source_event, to_state=PaymentStatus.CAPTURED, occurred_at=now())
    db_session.add_all((source_event, source_payment, record))
    db_session.flush()
    record.to_state = PaymentStatus.REFUNDED
    with pytest.raises(AppendOnlyRecordError, match="PaymentStateTransition records are append-only and cannot be updated"):
        db_session.flush()


def test_payment_state_transition_cannot_be_deleted(db_session: Session) -> None:
    source_event, source_payment = event("transition_delete"), payment("transition_delete")
    record = PaymentStateTransition(payment=source_payment, financial_event=source_event, to_state=PaymentStatus.CAPTURED, occurred_at=now())
    db_session.add_all((source_event, source_payment, record))
    db_session.flush()
    db_session.delete(record)
    with pytest.raises(AppendOnlyRecordError, match="PaymentStateTransition records are append-only and cannot be deleted"):
        db_session.flush()


def test_audit_event_is_append_only(db_session: Session) -> None:
    record = AuditEvent(actor_type="system", action_type="created", entity_type="payment")
    db_session.add(record)
    db_session.flush()
    record.action_type = "updated"
    with pytest.raises(AppendOnlyRecordError, match="AuditEvent records are append-only and cannot be updated"):
        db_session.flush()


def test_audit_event_cannot_be_deleted(db_session: Session) -> None:
    record = AuditEvent(actor_type="system", action_type="created", entity_type="payment")
    db_session.add(record)
    db_session.flush()
    db_session.delete(record)
    with pytest.raises(AppendOnlyRecordError, match="AuditEvent records are append-only and cannot be deleted"):
        db_session.flush()
