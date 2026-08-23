"""Database access for deterministic domain projections."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Payment, PaymentStateTransition, Refund, Settlement


class PaymentRepository:
    def get_by_provider_id(self, db: Session, provider_payment_id: str) -> Payment | None:
        return db.scalar(select(Payment).where(Payment.provider_payment_id == provider_payment_id))

    def get_by_id(self, db: Session, payment_id: UUID) -> Payment | None:
        return db.get(Payment, payment_id)


class PaymentStateTransitionRepository:
    def get_by_event_id(self, db: Session, financial_event_id: UUID) -> PaymentStateTransition | None:
        return db.scalar(
            select(PaymentStateTransition).where(PaymentStateTransition.financial_event_id == financial_event_id)
        )

    def add(self, db: Session, transition: PaymentStateTransition) -> None:
        db.add(transition)


class RefundRepository:
    def get_by_provider_id(self, db: Session, provider_refund_id: str) -> Refund | None:
        return db.scalar(select(Refund).where(Refund.provider_refund_id == provider_refund_id))

    def get_by_source_event_id(self, db: Session, financial_event_id: UUID) -> Refund | None:
        return db.scalar(select(Refund).where(Refund.source_financial_event_id == financial_event_id))

    def add(self, db: Session, refund: Refund) -> None:
        db.add(refund)


class SettlementRepository:
    def get_by_provider_id(self, db: Session, provider_settlement_id: str) -> Settlement | None:
        return db.scalar(select(Settlement).where(Settlement.provider_settlement_id == provider_settlement_id))

    def get_by_source_event_id(self, db: Session, financial_event_id: UUID) -> Settlement | None:
        return db.scalar(select(Settlement).where(Settlement.source_financial_event_id == financial_event_id))

    def add(self, db: Session, settlement: Settlement) -> None:
        db.add(settlement)