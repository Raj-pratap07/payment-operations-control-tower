"""Pure deterministic control rules for projected financial state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.enums import IncidentType, PaymentStatus, RefundStatus
from app.models import BankTransaction, FinancialEvent, Payment, PaymentStateTransition, Refund, Settlement


@dataclass(frozen=True)
class ControlConfig:
    payment_capture_timeout: timedelta = timedelta(hours=24)
    settlement_credit_timeout: timedelta = timedelta(days=2)
    enabled_controls: frozenset[IncidentType] = frozenset(IncidentType)

    def is_enabled(self, control: IncidentType) -> bool:
        return control in self.enabled_controls


@dataclass(frozen=True)
class EvidenceReference:
    entity_type: str
    entity_id: str
    evidence_type: str
    relationship: str


@dataclass(frozen=True)
class ControlFinding:
    incident_type: IncidentType
    incident_code: str
    title: str
    description: str
    financial_exposure: int | None
    currency: str | None
    detected_at: datetime
    evidence: tuple[EvidenceReference, ...]
    exposure_key: str


@dataclass(frozen=True)
class PaymentConflictInput:
    payment: Payment
    financial_event: FinancialEvent
    attempted_status: PaymentStatus


class ControlEngine:
    def __init__(self, config: ControlConfig | None = None) -> None:
        self.config = config or ControlConfig()

    def payment_state_conflict(self, conflict: PaymentConflictInput) -> ControlFinding | None:
        if not self.config.is_enabled(IncidentType.PAYMENT_STATE_CONFLICT):
            return None
        payment = conflict.payment
        event = conflict.financial_event
        return ControlFinding(
            incident_type=IncidentType.PAYMENT_STATE_CONFLICT,
            incident_code=f"PAYMENT_STATE_CONFLICT:{payment.id}:{event.id}",
            title=f"Payment state conflict for {payment.provider_payment_id}",
            description=(
                f"Event {event.external_event_id} attempted to move payment from "
                f"{payment.status.value} to {conflict.attempted_status.value}; the current state was preserved."
            ),
            financial_exposure=payment.amount,
            currency=payment.currency,
            detected_at=_utc(event.received_at),
            evidence=(
                EvidenceReference("Payment", str(payment.id), "payment", "conflicts_with"),
                EvidenceReference("FinancialEvent", str(event.id), "financial_event", "caused_by"),
            ),
            exposure_key=f"payment:{payment.id}:event:{event.id}",
        )

    def payment_signal_overdue(self, payment: Payment, *, detected_at: datetime) -> ControlFinding | None:
        if not self.config.is_enabled(IncidentType.PAYMENT_SIGNAL_OVERDUE):
            return None
        if payment.status is not PaymentStatus.AUTHORIZED or payment.authorized_at is None:
            return None
        detected_at = _utc(detected_at)
        if detected_at < _utc(payment.authorized_at) + self.config.payment_capture_timeout:
            return None
        expected_event = "PAYMENT_CAPTURED"
        return ControlFinding(
            incident_type=IncidentType.PAYMENT_SIGNAL_OVERDUE,
            incident_code=f"PAYMENT_SIGNAL_OVERDUE:{payment.id}:{expected_event}",
            title=f"Payment capture signal overdue for {payment.provider_payment_id}",
            description=(
                f"Payment has remained AUTHORIZED since {_utc(payment.authorized_at).isoformat()} "
                f"without {expected_event} for {self.config.payment_capture_timeout}."
            ),
            financial_exposure=payment.amount,
            currency=payment.currency,
            detected_at=detected_at,
            evidence=(EvidenceReference("Payment", str(payment.id), "payment", "has_overdue_signal"),),
            exposure_key=f"payment:{payment.id}:expected:{expected_event}",
        )

    def settlement_discrepancy(
        self,
        settlement: Settlement,
        *,
        expected_amount: int,
        detected_at: datetime,
        expected_evidence: tuple[EvidenceReference, ...] = (),
    ) -> ControlFinding | None:
        if not self.config.is_enabled(IncidentType.SETTLEMENT_DISCREPANCY):
            return None
        difference = expected_amount - settlement.amount
        if difference == 0:
            return None
        evidence = (
            EvidenceReference("Settlement", str(settlement.id), "settlement", "observed_amount"),
            *expected_evidence,
        )
        return ControlFinding(
            incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
            incident_code=f"SETTLEMENT_DISCREPANCY:{settlement.id}",
            title=f"Settlement discrepancy for {settlement.provider_settlement_id}",
            description=(
                f"Expected {expected_amount} minor units but observed {settlement.amount}; "
                f"difference is {difference} minor units."
            ),
            financial_exposure=abs(difference),
            currency=settlement.currency,
            detected_at=_utc(detected_at),
            evidence=evidence,
            exposure_key=f"settlement:{settlement.id}",
        )

    def refund_financial_drift(self, refund: Refund, payment: Payment, *, detected_at: datetime) -> ControlFinding | None:
        if not self.config.is_enabled(IncidentType.REFUND_FINANCIAL_DRIFT):
            return None
        reason: str | None = None
        if refund.amount > payment.amount:
            reason = f"Refund amount {refund.amount} exceeds payment amount {payment.amount}."
        elif refund.status is RefundStatus.PROCESSED and payment.status not in {PaymentStatus.CAPTURED, PaymentStatus.REFUNDED}:
            reason = f"Refund is PROCESSED while payment is {payment.status.value}."
        if reason is None:
            return None
        return ControlFinding(
            incident_type=IncidentType.REFUND_FINANCIAL_DRIFT,
            incident_code=f"REFUND_FINANCIAL_DRIFT:{refund.id}",
            title=f"Refund financial drift for {refund.provider_refund_id}",
            description=reason,
            financial_exposure=refund.amount,
            currency=refund.currency,
            detected_at=_utc(detected_at),
            evidence=(
                EvidenceReference("Refund", str(refund.id), "refund", "causes_drift"),
                EvidenceReference("Payment", str(payment.id), "payment", "compared_with"),
            ),
            exposure_key=f"refund:{refund.id}",
        )

    def event_integrity(self, transition: PaymentStateTransition, payment: Payment) -> ControlFinding | None:
        if not self.config.is_enabled(IncidentType.EVENT_INTEGRITY):
            return None
        if transition.payment_id != payment.id:
            return None
        if transition.to_state is payment.status:
            return None
        return ControlFinding(
            incident_type=IncidentType.EVENT_INTEGRITY,
            incident_code=f"EVENT_INTEGRITY:{payment.id}:{transition.id}",
            title=f"Event integrity problem for {payment.provider_payment_id}",
            description="A persisted payment transition disagrees with the current payment projection.",
            financial_exposure=payment.amount,
            currency=payment.currency,
            detected_at=_utc(transition.occurred_at),
            evidence=(
                EvidenceReference("Payment", str(payment.id), "payment", "has_integrity_issue"),
                EvidenceReference("PaymentStateTransition", str(transition.id), "transition", "contradicts"),
            ),
            exposure_key=f"payment:{payment.id}:transition:{transition.id}",
        )

    def settlement_credit_delay(
        self,
        settlement: Settlement,
        bank_transactions: list[BankTransaction],
        *,
        detected_at: datetime,
    ) -> ControlFinding | None:
        if not self.config.is_enabled(IncidentType.SETTLEMENT_CREDIT_DELAY):
            return None
        if settlement.processed_at is None:
            return None
        processed_at = _utc(settlement.processed_at)
        detected_at = _utc(detected_at)
        if detected_at < processed_at + self.config.settlement_credit_timeout:
            return None
        matching_credit = any(
            transaction.transaction_type.upper() == "CREDIT"
            and transaction.currency == settlement.currency
            and transaction.amount == settlement.amount
            and _utc(transaction.transaction_at) >= processed_at
            and (not settlement.utr or transaction.utr == settlement.utr)
            for transaction in bank_transactions
        )
        if matching_credit:
            return None
        return ControlFinding(
            incident_type=IncidentType.SETTLEMENT_CREDIT_DELAY,
            incident_code=f"SETTLEMENT_CREDIT_DELAY:{settlement.id}",
            title=f"Bank credit delayed for {settlement.provider_settlement_id}",
            description=(
                f"No matching bank credit for {settlement.amount} {settlement.currency} was found "
                f"after the {self.config.settlement_credit_timeout} window."
            ),
            financial_exposure=settlement.amount,
            currency=settlement.currency,
            detected_at=detected_at,
            evidence=(EvidenceReference("Settlement", str(settlement.id), "settlement", "awaits_credit"),),
            exposure_key=f"settlement:{settlement.id}",
        )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)