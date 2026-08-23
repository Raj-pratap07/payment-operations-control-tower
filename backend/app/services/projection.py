"""Deterministic projection of canonical events into domain state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.enums import PaymentStatus, RefundStatus, SettlementStatus
from app.models import FinancialEvent, Payment, PaymentStateTransition, Refund, Settlement
from app.repositories.projections import (
    PaymentRepository,
    PaymentStateTransitionRepository,
    RefundRepository,
    SettlementRepository,
)
from app.schemas.canonical_events import (
    CanonicalEvent,
    CanonicalEventType,
    PaymentEventData,
    RefundEventData,
    SettlementEventData,
)


class ProjectionError(ValueError):
    """Base class for deterministic projection failures."""


class MissingPaymentError(ProjectionError):
    pass


class MissingRefundError(ProjectionError):
    pass


class InvalidStateTransitionError(ProjectionError):
    pass


class DuplicateTransitionError(ProjectionError):
    pass


class InconsistentFinancialIdentifierError(ProjectionError):
    pass


class UnsupportedProjectionError(ProjectionError):
    pass


@dataclass(frozen=True)
class ProjectionResult:
    event_id: UUID
    aggregate_type: str
    aggregate_id: str


class PaymentProjectionService:
    def __init__(
        self,
        payments: PaymentRepository | None = None,
        transitions: PaymentStateTransitionRepository | None = None,
    ) -> None:
        self._payments = payments or PaymentRepository()
        self._transitions = transitions or PaymentStateTransitionRepository()

    def project(self, db: Session, event: CanonicalEvent) -> Payment:
        if not isinstance(event.data, PaymentEventData):
            raise UnsupportedProjectionError("Payment projection requires payment event data.")

        existing_transition = self._transitions.get_by_event_id(db, event.event_id)
        if existing_transition is not None:
            payment = self._payments.get_by_id(db, existing_transition.payment_id)
            if payment is None:
                raise DuplicateTransitionError(
                    f"Financial event {event.event_id} references a missing payment projection."
                )
            return payment

        data = event.data
        payment = self._payments.get_by_provider_id(db, data.payment_id)
        if payment is None:
            payment = Payment(
                provider_payment_id=data.payment_id,
                provider_order_id=data.order_id,
                amount=data.amount_minor,
                currency=data.currency,
                status=self._target_status(event.event_type),
            )
            db.add(payment)
            db.flush()
            from_state = None
        else:
            self._validate_identifiers(payment, data)
            from_state = payment.status
            self._validate_transition(payment.status, event.event_type)
            payment.status = self._target_status(event.event_type)

        self._set_state_timestamp(payment, event.event_type, event.occurred_at)
        self._transitions.add(
            db,
            PaymentStateTransition(
                payment_id=payment.id,
                financial_event_id=event.event_id,
                from_state=from_state,
                to_state=payment.status,
                occurred_at=event.occurred_at,
            ),
        )
        return payment

    @staticmethod
    def _target_status(event_type: CanonicalEventType) -> PaymentStatus:
        return {
            CanonicalEventType.PAYMENT_AUTHORIZED: PaymentStatus.AUTHORIZED,
            CanonicalEventType.PAYMENT_CAPTURED: PaymentStatus.CAPTURED,
            CanonicalEventType.PAYMENT_FAILED: PaymentStatus.FAILED,
        }[event_type]

    @staticmethod
    def _validate_identifiers(payment: Payment, data: PaymentEventData) -> None:
        if payment.amount != data.amount_minor or payment.currency != data.currency:
            raise InconsistentFinancialIdentifierError(
                f"Payment {data.payment_id} has inconsistent amount or currency."
            )
        if payment.provider_order_id and data.order_id and payment.provider_order_id != data.order_id:
            raise InconsistentFinancialIdentifierError(f"Payment {data.payment_id} has inconsistent order ID.")
        if payment.provider_order_id is None and data.order_id:
            payment.provider_order_id = data.order_id

    @staticmethod
    def _validate_transition(current: PaymentStatus, event_type: CanonicalEventType) -> None:
        valid_targets = {
            PaymentStatus.CREATED: {CanonicalEventType.PAYMENT_AUTHORIZED, CanonicalEventType.PAYMENT_FAILED},
            PaymentStatus.AUTHORIZED: {CanonicalEventType.PAYMENT_CAPTURED, CanonicalEventType.PAYMENT_FAILED},
            PaymentStatus.CAPTURED: set(),
            PaymentStatus.FAILED: set(),
            PaymentStatus.REFUND_PENDING: set(),
            PaymentStatus.REFUNDED: set(),
        }
        if event_type not in valid_targets[current]:
            raise InvalidStateTransitionError(f"Cannot apply {event_type.value} to payment in {current.value} state.")

    @staticmethod
    def _set_state_timestamp(payment: Payment, event_type: CanonicalEventType, occurred_at: datetime) -> None:
        if event_type is CanonicalEventType.PAYMENT_AUTHORIZED:
            payment.authorized_at = occurred_at
        elif event_type is CanonicalEventType.PAYMENT_CAPTURED:
            payment.captured_at = occurred_at
        elif event_type is CanonicalEventType.PAYMENT_FAILED:
            payment.failed_at = occurred_at


class RefundProjectionService:
    def __init__(self, refunds: RefundRepository | None = None, payments: PaymentRepository | None = None) -> None:
        self._refunds = refunds or RefundRepository()
        self._payments = payments or PaymentRepository()

    def project(self, db: Session, event: CanonicalEvent) -> Refund:
        if not isinstance(event.data, RefundEventData):
            raise UnsupportedProjectionError("Refund projection requires refund event data.")
        data = event.data
        existing_from_event = self._refunds.get_by_source_event_id(db, event.event_id)
        if existing_from_event is not None:
            return existing_from_event
        payment = self._payments.get_by_provider_id(db, data.payment_id)
        if payment is None:
            raise MissingPaymentError(f"Payment {data.payment_id} does not exist for refund {data.refund_id}.")
        refund = self._refunds.get_by_provider_id(db, data.refund_id)
        if refund is None:
            if event.event_type is CanonicalEventType.REFUND_CREATED:
                status = RefundStatus.CREATED
            else:
                raise MissingRefundError(f"Refund {data.refund_id} does not exist.")
            refund = Refund(
                provider_refund_id=data.refund_id,
                payment_id=payment.id,
                amount=data.amount_minor,
                currency=data.currency,
                status=status,
                source_financial_event_id=event.event_id,
            )
            self._refunds.add(db, refund)
            return refund
        self._validate_identifiers(refund, data, payment.id)
        if event.event_type is CanonicalEventType.REFUND_PROCESSED:
            if refund.status is RefundStatus.PROCESSED:
                return refund
            if refund.status not in {RefundStatus.CREATED, RefundStatus.PENDING}:
                raise InvalidStateTransitionError(f"Cannot process refund in {refund.status.value} state.")
            refund.status = RefundStatus.PROCESSED
            refund.processed_at = event.occurred_at
        elif event.event_type is CanonicalEventType.REFUND_FAILED:
            if refund.status is RefundStatus.FAILED:
                return refund
            if refund.status not in {RefundStatus.CREATED, RefundStatus.PENDING}:
                raise InvalidStateTransitionError(f"Cannot fail refund in {refund.status.value} state.")
            refund.status = RefundStatus.FAILED
            refund.failed_at = event.occurred_at
        else:
            return refund
        return refund

    @staticmethod
    def _validate_identifiers(refund: Refund, data: RefundEventData, payment_id: object) -> None:
        if refund.payment_id != payment_id or refund.amount != data.amount_minor or refund.currency != data.currency:
            raise InconsistentFinancialIdentifierError(
                f"Refund {data.refund_id} has inconsistent payment, amount, or currency."
            )


class SettlementProjectionService:
    def __init__(self, settlements: SettlementRepository | None = None) -> None:
        self._settlements = settlements or SettlementRepository()

    def project(self, db: Session, event: CanonicalEvent) -> Settlement:
        if not isinstance(event.data, SettlementEventData):
            raise UnsupportedProjectionError("Settlement projection requires settlement event data.")
        data = event.data
        existing_from_event = self._settlements.get_by_source_event_id(db, event.event_id)
        if existing_from_event is not None:
            return existing_from_event
        settlement = self._settlements.get_by_provider_id(db, data.settlement_id)
        if settlement is not None:
            raise InconsistentFinancialIdentifierError(f"Settlement {data.settlement_id} already exists.")
        settlement = Settlement(
            provider_settlement_id=data.settlement_id,
            amount=data.amount_minor,
            currency=data.currency,
            fees=data.fees_minor,
            tax=data.tax_minor,
            utr=data.utr,
            status=SettlementStatus.PROCESSED,
            processed_at=event.occurred_at,
            source_financial_event_id=event.event_id,
        )
        self._settlements.add(db, settlement)
        return settlement


class ProjectionService:
    def __init__(
        self,
        payments: PaymentProjectionService | None = None,
        refunds: RefundProjectionService | None = None,
        settlements: SettlementProjectionService | None = None,
    ) -> None:
        self._payments = payments or PaymentProjectionService()
        self._refunds = refunds or RefundProjectionService()
        self._settlements = settlements or SettlementProjectionService()

    def project(self, db: Session, event: CanonicalEvent) -> ProjectionResult:
        if not isinstance(event, CanonicalEvent):
            raise UnsupportedProjectionError("Only canonical events can be projected.")
        transaction = db.begin_nested() if db.in_transaction() else db.begin()
        with transaction:
            if event.event_type in {
                CanonicalEventType.PAYMENT_AUTHORIZED,
                CanonicalEventType.PAYMENT_CAPTURED,
                CanonicalEventType.PAYMENT_FAILED,
            }:
                aggregate = self._payments.project(db, event)
            elif event.event_type in {
                CanonicalEventType.REFUND_CREATED,
                CanonicalEventType.REFUND_PROCESSED,
                CanonicalEventType.REFUND_FAILED,
            }:
                aggregate = self._refunds.project(db, event)
            elif event.event_type is CanonicalEventType.SETTLEMENT_PROCESSED:
                aggregate = self._settlements.project(db, event)
            else:
                raise UnsupportedProjectionError(f"Canonical event {event.event_type} is not projectable.")
        return ProjectionResult(event.event_id, event.aggregate_type.value, event.aggregate_id)


def project_financial_event(db: Session, event: FinancialEvent) -> ProjectionResult:
    """Normalize and project a persisted financial event in one projection transaction."""
    from app.services.event_normalization import EventNormalizationService

    normalized = EventNormalizationService().normalize(event)
    if not isinstance(normalized, CanonicalEvent):
        raise UnsupportedProjectionError(normalized.reason)
    return ProjectionService().project(db, normalized)