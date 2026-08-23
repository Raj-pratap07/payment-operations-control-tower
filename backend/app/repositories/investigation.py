"""Read-only repository queries used by the AI investigation tools."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BankTransaction, FinancialEvent, Incident, IncidentEvidence, Payment, PaymentStateTransition, Refund, Settlement


class InvestigationRepository:
    def get_incident(self, db: Session, incident_id: UUID) -> Incident | None:
        return db.get(Incident, incident_id)

    def get_incident_evidence(self, db: Session, incident_id: UUID) -> list[IncidentEvidence]:
        return list(db.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident_id)))

    def get_financial_event(self, db: Session, event_id: UUID) -> FinancialEvent | None:
        return db.get(FinancialEvent, event_id)

    def get_payment(self, db: Session, payment_id: UUID | None = None, provider_payment_id: str | None = None) -> Payment | None:
        if payment_id is not None:
            return db.get(Payment, payment_id)
        if provider_payment_id is None:
            return None
        return db.scalar(select(Payment).where(Payment.provider_payment_id == provider_payment_id))

    def get_payment_history(self, db: Session, payment_id: UUID) -> list[PaymentStateTransition]:
        return list(db.scalars(select(PaymentStateTransition).where(PaymentStateTransition.payment_id == payment_id).order_by(PaymentStateTransition.occurred_at, PaymentStateTransition.created_at)))

    def get_refund(self, db: Session, refund_id: UUID | None = None, provider_refund_id: str | None = None) -> Refund | None:
        if refund_id is not None:
            return db.get(Refund, refund_id)
        if provider_refund_id is None:
            return None
        return db.scalar(select(Refund).where(Refund.provider_refund_id == provider_refund_id))

    def get_settlement(self, db: Session, settlement_id: UUID | None = None, provider_settlement_id: str | None = None) -> Settlement | None:
        if settlement_id is not None:
            return db.get(Settlement, settlement_id)
        if provider_settlement_id is None:
            return None
        return db.scalar(select(Settlement).where(Settlement.provider_settlement_id == provider_settlement_id))

    def get_bank_transaction(self, db: Session, transaction_id: UUID | None = None, external_transaction_id: str | None = None) -> BankTransaction | None:
        if transaction_id is not None:
            return db.get(BankTransaction, transaction_id)
        if external_transaction_id is None:
            return None
        return db.scalar(select(BankTransaction).where(BankTransaction.external_transaction_id == external_transaction_id))

    def find_related_transactions(self, db: Session, *, amount: int, currency: str, utr: str | None = None) -> list[BankTransaction]:
        query = select(BankTransaction).where(BankTransaction.amount == amount, BankTransaction.currency == currency)
        if utr:
            query = query.where(BankTransaction.utr == utr)
        return list(db.scalars(query))
