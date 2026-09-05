"""Deterministic real-database demo scenarios for the control tower."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
load_dotenv(BACKEND / ".env")

from app.core.enums import PaymentStatus, RefundStatus, SettlementStatus  # noqa: E402
from app.database.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    ActionExecution,
    ActionProposal,
    Approval,
    AuditEvent,
    BankTransaction,
    FinancialEvent,
    Incident,
    IncidentEvidence,
    Payment,
    PaymentStateTransition,
    Policy,
    Refund,
    Settlement,
)
from app.repositories.financial_events import FinancialEventRepository  # noqa: E402
from app.services.event_normalization import EventNormalizationService  # noqa: E402
from app.services.incidents import IncidentDetectionService  # noqa: E402
from app.services.projection import ProjectionError, project_financial_event  # noqa: E402
from app.services.webhook_ingestion import WebhookIngestionService  # noqa: E402


SCENARIOS = ("clean_payment", "payment_state_conflict", "settlement_discrepancy", "refund_drift", "settlement_credit_delay")
DEMO_PREFIX = "DEMO:"
DEMO_SETTLEMENT_EXPOSURE = 2_800_000
PRIMARY_EXPECTED = 50_000_000
PRIMARY_OBSERVED = 47_200_000
PRIMARY_DIFFERENCE = PRIMARY_EXPECTED - PRIMARY_OBSERVED


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    run_id: str
    payment_id: UUID | None
    settlement_id: UUID | None
    bank_transaction_id: UUID | None
    incident_id: UUID | None


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + UUID(int=__import__("secrets").randbits(128)).hex[:8]


def _payload(event_type: str, entity_name: str, entity: dict[str, Any], occurred_at: datetime) -> dict[str, Any]:
    return {"event": event_type, "created_at": int(occurred_at.timestamp()), "payload": {entity_name: {"entity": entity}}}


def _ingest_and_project(db: Session, external_id: str, event_type: str, entity_name: str, entity: dict[str, Any], occurred_at: datetime) -> FinancialEvent:
    WebhookIngestionService().ingest_razorpay_event(db, external_event_id=external_id, payload=_payload(event_type, entity_name, entity, occurred_at))
    event = FinancialEventRepository().get_by_source_and_external_event_id(db, source="RAZORPAY", external_event_id=external_id)
    if event is None:
        raise RuntimeError(f"Could not retrieve ingested demo event {external_id}.")
    project_financial_event(db, event)
    db.commit()
    return event


def _payment(db: Session, run_id: str, at: datetime, amount: int = PRIMARY_EXPECTED) -> Payment:
    payment = Payment(provider_payment_id=f"{DEMO_PREFIX}payment:{run_id}", provider_order_id=f"{DEMO_PREFIX}order:{run_id}", amount=amount, currency="INR", status=PaymentStatus.CREATED, created_at=at, updated_at=at)
    db.add(payment)
    db.commit()
    WebhookIngestionService().ingest_razorpay_event(
        db,
        external_event_id=f"{DEMO_PREFIX}event:{run_id}:created",
        payload=_payload("payment.created", "payment", {"id": payment.provider_payment_id, "order_id": payment.provider_order_id, "amount": amount, "currency": "INR"}, at),
    )
    for index, state in enumerate(("authorized", "captured")):
        event_type = f"payment.{state}"
        event = _ingest_and_project(db, f"{DEMO_PREFIX}event:{run_id}:{state}", event_type, "payment", {"id": payment.provider_payment_id, "order_id": payment.provider_order_id, "amount": amount, "currency": "INR"}, at + timedelta(minutes=index + 1))
    db.refresh(payment)
    return payment


def _settlement(db: Session, run_id: str, at: datetime, amount: int, utr: str = "DEMO-UTR") -> Settlement:
    _ingest_and_project(db, f"{DEMO_PREFIX}event:{run_id}:settlement", "settlement.processed", "settlement", {"id": f"{DEMO_PREFIX}settlement:{run_id}", "amount": amount, "currency": "INR", "fee": 0, "tax": 0, "utr": f"{utr}-{run_id}"}, at)
    settlement = db.scalar(select(Settlement).where(Settlement.provider_settlement_id == f"{DEMO_PREFIX}settlement:{run_id}"))
    if settlement is None:
        raise RuntimeError("Could not retrieve projected demo settlement.")
    return settlement


def clean_payment(db: Session, run_id: str) -> ScenarioResult:
    payment = _payment(db, run_id, datetime(2026, 1, 1, tzinfo=UTC), amount=125_000)
    return ScenarioResult("clean_payment", run_id, payment.id, None, None, None)


def payment_state_conflict(db: Session, run_id: str) -> ScenarioResult:
    at = datetime(2026, 1, 2, tzinfo=UTC)
    payment = _payment(db, run_id, at, amount=125_000)
    conflict_event_id = f"{DEMO_PREFIX}event:{run_id}:late-authorized"
    WebhookIngestionService().ingest_razorpay_event(db, external_event_id=conflict_event_id, payload=_payload("payment.authorized", "payment", {"id": payment.provider_payment_id, "order_id": payment.provider_order_id, "amount": payment.amount, "currency": payment.currency}, at + timedelta(hours=1)))
    event = FinancialEventRepository().get_by_source_and_external_event_id(db, source="RAZORPAY", external_event_id=conflict_event_id)
    if event is None:
        raise RuntimeError("Could not retrieve conflicting demo event.")
    try:
        project_financial_event(db, event)
    except ProjectionError:
        db.rollback()
    else:
        raise RuntimeError("The conflicting event unexpectedly projected successfully.")
    db.refresh(payment)
    incident = IncidentDetectionService().detect_payment_state_conflict(db, payment, event, PaymentStatus.AUTHORIZED)
    db.commit()
    return ScenarioResult("payment_state_conflict", run_id, payment.id, None, None, incident.id if incident else None)


def settlement_discrepancy(db: Session, run_id: str) -> ScenarioResult:
    at = datetime(2026, 1, 3, tzinfo=UTC)
    payment = _payment(db, run_id, at)
    settlement = _settlement(db, run_id, at + timedelta(days=1), PRIMARY_OBSERVED, "DEMO-DISCREPANCY")
    bank = BankTransaction(external_transaction_id=f"{DEMO_PREFIX}bank:{run_id}", transaction_type="CREDIT", amount=PRIMARY_OBSERVED, currency="INR", utr=settlement.utr, transaction_at=at + timedelta(days=1, hours=2), source="DEMO_SIMULATOR")
    db.add(bank)
    seed_demo_policies(db)
    incident = IncidentDetectionService().detect_settlement_discrepancy(db, settlement, detected_at=at + timedelta(days=1, hours=3), expected_amount=PRIMARY_EXPECTED)
    db.commit()
    return ScenarioResult("settlement_discrepancy", run_id, payment.id, settlement.id, bank.id, incident.id if incident else None)


def refund_drift(db: Session, run_id: str) -> ScenarioResult:
    at = datetime(2026, 1, 4, tzinfo=UTC)
    payment = _payment(db, run_id, at, amount=10_000)
    refund_id = f"{DEMO_PREFIX}refund:{run_id}"
    _ingest_and_project(db, f"{DEMO_PREFIX}event:{run_id}:refund-created", "refund.created", "refund", {"id": refund_id, "payment_id": payment.provider_payment_id, "amount": 15_000, "currency": "INR"}, at + timedelta(minutes=3))
    _ingest_and_project(db, f"{DEMO_PREFIX}event:{run_id}:refund-processed", "refund.processed", "refund", {"id": refund_id, "payment_id": payment.provider_payment_id, "amount": 15_000, "currency": "INR"}, at + timedelta(minutes=4))
    incident = IncidentDetectionService().detect_refund_financial_drift(db, detected_at=at + timedelta(minutes=5))[0]
    db.commit()
    return ScenarioResult("refund_drift", run_id, payment.id, None, None, incident.id)


def settlement_credit_delay(db: Session, run_id: str) -> ScenarioResult:
    at = datetime(2026, 1, 5, tzinfo=UTC)
    settlement = _settlement(db, run_id, at, PRIMARY_EXPECTED, "DEMO-DELAY")
    incident = IncidentDetectionService().detect_settlement_credit_delay(db, detected_at=at + timedelta(days=2, seconds=1))[0]
    db.commit()
    return ScenarioResult("settlement_credit_delay", run_id, None, settlement.id, None, incident.id)


def reset_demo(db: Session) -> None:
    event_ids = set(db.scalars(select(FinancialEvent.id).where(FinancialEvent.external_event_id.like(f"{DEMO_PREFIX}%"))))
    payment_ids = set(db.scalars(select(Payment.id).where(Payment.provider_payment_id.like(f"{DEMO_PREFIX}%"))))
    settlement_ids = set(db.scalars(select(Settlement.id).where(Settlement.provider_settlement_id.like(f"{DEMO_PREFIX}%"))))
    refund_ids = set(db.scalars(select(Refund.id).where(Refund.provider_refund_id.like(f"{DEMO_PREFIX}%"))))
    bank_ids = set(db.scalars(select(BankTransaction.id).where(BankTransaction.external_transaction_id.like(f"{DEMO_PREFIX}%"))))
    evidence_rows = list(db.scalars(select(IncidentEvidence)))
    demo_entity_ids = {str(item) for item in event_ids | payment_ids | settlement_ids | refund_ids | bank_ids}
    incident_ids = {row.incident_id for row in evidence_rows if row.entity_id in demo_entity_ids or row.entity_id.startswith(DEMO_PREFIX)}
    incident_ids |= set(db.scalars(select(Incident.id).where(Incident.incident_code.like(f"{DEMO_PREFIX}%"))))
    proposal_ids = set(db.scalars(select(ActionProposal.id).where(ActionProposal.incident_id.in_(incident_ids)))) if incident_ids else set()
    execution_ids = set(db.scalars(select(ActionExecution.id).where(ActionExecution.action_proposal_id.in_(proposal_ids)))) if proposal_ids else set()
    audit_entity_ids = {str(item) for item in incident_ids | proposal_ids | execution_ids | event_ids | payment_ids | settlement_ids | refund_ids | bank_ids}
    if audit_entity_ids:
        db.execute(delete(AuditEvent).where(AuditEvent.entity_id.in_(audit_entity_ids)))
    if proposal_ids:
        db.execute(delete(ActionExecution).where(ActionExecution.id.in_(execution_ids)))
        db.execute(delete(Approval).where(Approval.action_proposal_id.in_(proposal_ids)))
        db.execute(delete(ActionProposal).where(ActionProposal.id.in_(proposal_ids)))
    if incident_ids:
        db.execute(delete(IncidentEvidence).where(IncidentEvidence.incident_id.in_(incident_ids)))
        db.execute(delete(Incident).where(Incident.id.in_(incident_ids)))
    if payment_ids or event_ids:
        transition_query = delete(PaymentStateTransition)
        if payment_ids:
            transition_query = transition_query.where(PaymentStateTransition.payment_id.in_(payment_ids))
        if event_ids:
            transition_query = transition_query.where(PaymentStateTransition.financial_event_id.in_(event_ids))
        db.execute(transition_query)
    if refund_ids:
        db.execute(delete(Refund).where(Refund.id.in_(refund_ids)))
    if settlement_ids:
        db.execute(delete(Settlement).where(Settlement.id.in_(settlement_ids)))
    if payment_ids:
        db.execute(delete(Payment).where(Payment.id.in_(payment_ids)))
    if bank_ids:
        db.execute(delete(BankTransaction).where(BankTransaction.id.in_(bank_ids)))
    if event_ids:
        db.execute(delete(FinancialEvent).where(FinancialEvent.id.in_(event_ids)))
    db.execute(delete(Policy).where(Policy.name.like(f"{DEMO_PREFIX}%")))
    db.commit()


def seed_demo_policies(db: Session) -> None:
    """Create demo policies that ensure RECONCILE_ADJUSTMENT evaluates to REQUIRE_APPROVAL.

    Idempotent: checks for existing active policy before inserting.
    Caller is responsible for committing.
    """
    existing = db.scalar(select(Policy).where(Policy.action_type == "RECONCILE_ADJUSTMENT", Policy.is_active == True))  # noqa: E712
    if existing is not None:
        return
    db.add(Policy(
        name=f"{DEMO_PREFIX}RECONCILE_ADJUSTMENT",
        description=f"{DEMO_PREFIX} Deterministic demo policy for settlement reconciliation proposals.",
        action_type="RECONCILE_ADJUSTMENT",
        max_amount=10_000,
        min_confidence=Decimal("0.90"),
        requires_approval=True,
        is_active=True,
    ))
    db.flush()


def summary(db: Session, result: ScenarioResult) -> None:
    ids = [result.payment_id, result.settlement_id, result.bank_transaction_id, result.incident_id]
    print(f"Scenario: {result.scenario}\nRun ID: {result.run_id}")
    print(f"\nPayments: {db.scalar(select(func.count()).select_from(Payment).where(Payment.provider_payment_id.like(f'{DEMO_PREFIX}%{result.run_id}%'))) or 0}")
    print(f"Refunds: {db.scalar(select(func.count()).select_from(Refund).where(Refund.provider_refund_id.like(f'{DEMO_PREFIX}%{result.run_id}%'))) or 0}")
    print(f"Settlements: {db.scalar(select(func.count()).select_from(Settlement).where(Settlement.provider_settlement_id.like(f'{DEMO_PREFIX}%{result.run_id}%'))) or 0}")
    print(f"Bank Transactions: {db.scalar(select(func.count()).select_from(BankTransaction).where(BankTransaction.external_transaction_id.like(f'{DEMO_PREFIX}%{result.run_id}%'))) or 0}")
    print(f"Financial Events: {db.scalar(select(func.count()).select_from(FinancialEvent).where(FinancialEvent.external_event_id.like(f'{DEMO_PREFIX}%{result.run_id}%'))) or 0}")
    print(f"Incidents: {db.scalar(select(func.count()).select_from(IncidentEvidence).where(IncidentEvidence.entity_id.in_([str(item) for item in ids if item]))) or 0}")
    incident = db.get(Incident, result.incident_id) if result.incident_id else None
    print(f"Financial Exposure: {(incident.financial_exposure if incident else 0) or 0} minor units")
    print(f"\nPayment ID: {result.payment_id or '—'}\nSettlement ID: {result.settlement_id or '—'}\nBank Transaction ID: {result.bank_transaction_id or '—'}\nIncident ID: {result.incident_id or '—'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic real-database payment operations demos.")
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS)
    parser.add_argument("--list", action="store_true", dest="list_scenarios")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(argv)
    if args.list_scenarios:
        print("\n".join(SCENARIOS))
        return 0
    with SessionLocal() as db:
        if args.reset:
            reset_demo(db)
            print("Reset complete: DEMO-prefixed simulator records removed.")
            return 0
        if not args.scenario:
            parser.error("choose a scenario or use --list/--reset")
        result = globals()[args.scenario](db, new_run_id())
        summary(db, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())