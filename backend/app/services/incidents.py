"""Incident detection orchestration for deterministic control findings."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.enums import IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus, RefundStatus
from app.engines.controls import ControlEngine, ControlFinding, EvidenceReference, PaymentConflictInput
from app.models import BankTransaction, FinancialEvent, Incident, IncidentEvidence, Payment, PaymentStateTransition, Refund, Settlement
from app.repositories.incidents import ControlStateRepository, IncidentEvidenceRepository, IncidentRepository


class IncidentDetectionError(ValueError):
    """Raised when incident detection cannot safely evaluate a condition."""


class IncidentDetectionService:
    def __init__(
        self,
        controls: ControlEngine | None = None,
        incidents: IncidentRepository | None = None,
        evidence: IncidentEvidenceRepository | None = None,
        state: ControlStateRepository | None = None,
    ) -> None:
        self._controls = controls or ControlEngine()
        self._incidents = incidents or IncidentRepository()
        self._evidence = evidence or IncidentEvidenceRepository()
        self._state = state or ControlStateRepository()

    def detect_payment_state_conflict(
        self, db: Session, payment: Payment, financial_event: FinancialEvent, attempted_status: PaymentStatus
    ) -> Incident | None:
        return self._persist(db, self._controls.payment_state_conflict(PaymentConflictInput(payment, financial_event, attempted_status)))

    def detect_payment_signal_overdue(self, db: Session, *, detected_at: datetime) -> list[Incident]:
        findings = [
            self._controls.payment_signal_overdue(payment, detected_at=detected_at)
            for payment in self._state.payments(db)
        ]
        return self._persist_many(db, [finding for finding in findings if finding is not None])

    def detect_settlement_discrepancy(
        self, db: Session, settlement: Settlement, *, detected_at: datetime, expected_amount: int | None = None
    ) -> Incident | None:
        expected, evidence = self._expected_settlement(db, settlement) if expected_amount is None else (expected_amount, ())
        finding = self._controls.settlement_discrepancy(
            settlement, expected_amount=expected, detected_at=detected_at, expected_evidence=evidence
        )
        return self._persist(db, finding)

    def detect_refund_financial_drift(self, db: Session, *, detected_at: datetime) -> list[Incident]:
        findings: list[ControlFinding] = []
        payments = {payment.id: payment for payment in self._state.payments(db)}
        for refund in self._state.refunds(db):
            payment = payments.get(refund.payment_id)
            if payment is None:
                raise IncidentDetectionError(f"Refund {refund.provider_refund_id} references a missing payment.")
            finding = self._controls.refund_financial_drift(refund, payment, detected_at=detected_at)
            if finding is not None:
                findings.append(finding)
        return self._persist_many(db, findings)

    def detect_event_integrity(self, db: Session, payment: Payment) -> list[Incident]:
        findings = [
            self._controls.event_integrity(transition, payment)
            for transition in self._state.payment_transitions(db, payment.id)
        ]
        return self._persist_many(db, [finding for finding in findings if finding is not None])

    def detect_settlement_credit_delay(self, db: Session, *, detected_at: datetime) -> list[Incident]:
        transactions = self._state.bank_transactions(db)
        findings = [
            self._controls.settlement_credit_delay(settlement, transactions, detected_at=detected_at)
            for settlement in self._state.settlements(db)
        ]
        return self._persist_many(db, [finding for finding in findings if finding is not None])

    def detect_financial_exposure(self, db: Session, *, detected_at: datetime) -> Incident | None:
        unresolved = [
            incident
            for incident in self._state.incidents(db)
            if incident.status not in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}
            and incident.incident_type is not IncidentType.FINANCIAL_EXPOSURE
            and incident.financial_exposure is not None
        ]
        by_key: dict[str, Incident] = {}
        for incident in unresolved:
            key = self._incident_exposure_key(db, incident)
            current = by_key.get(key)
            if current is None or (current.financial_exposure or 0) < (incident.financial_exposure or 0):
                by_key[key] = incident
        if not by_key:
            return None
        currencies = {incident.currency for incident in by_key.values() if incident.currency}
        currency = currencies.pop() if len(currencies) == 1 else None
        exposure = sum(incident.financial_exposure or 0 for incident in by_key.values())
        finding = ControlFinding(
            incident_type=IncidentType.FINANCIAL_EXPOSURE,
            incident_code="FINANCIAL_EXPOSURE:UNRESOLVED",
            title="Aggregate unresolved financial exposure",
            description=f"Unresolved incidents represent {exposure} minor units of aggregate exposure.",
            financial_exposure=exposure,
            currency=currency,
            detected_at=_utc(detected_at),
            evidence=tuple(
                EvidenceReference("Incident", str(incident.id), "incident", "contributes_to")
                for incident in sorted(by_key.values(), key=lambda item: str(item.id))
            ),
            exposure_key="financial_exposure:unresolved",
        )
        return self._persist(db, finding)

    def detect_all(self, db: Session, *, detected_at: datetime) -> list[Incident]:
        detected: list[Incident] = []
        detected.extend(self.detect_payment_signal_overdue(db, detected_at=detected_at))
        detected.extend(self.detect_refund_financial_drift(db, detected_at=detected_at))
        detected.extend(self.detect_settlement_credit_delay(db, detected_at=detected_at))
        exposure = self.detect_financial_exposure(db, detected_at=detected_at)
        if exposure is not None:
            detected.append(exposure)
        return detected

    def _persist(self, db: Session, finding: ControlFinding | None) -> Incident | None:
        if finding is None:
            return None
        transaction = db.begin_nested() if db.in_transaction() else db.begin()
        with transaction:
            incident = self._incidents.get_by_code(db, finding.incident_code)
            if incident is None:
                incident = Incident(
                    incident_code=finding.incident_code,
                    incident_type=finding.incident_type,
                    severity=self._severity(finding.financial_exposure),
                    status=IncidentStatus.OPEN,
                    title=finding.title,
                    description=finding.description,
                    financial_exposure=finding.financial_exposure,
                    currency=finding.currency,
                    detected_at=finding.detected_at,
                )
                self._incidents.add(db, incident)
                db.flush()
            else:
                incident.title = finding.title
                incident.description = finding.description
                incident.financial_exposure = finding.financial_exposure
                incident.currency = finding.currency
                incident.severity = self._severity(finding.financial_exposure)
                incident.detected_at = finding.detected_at
                if incident.status in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}:
                    incident.status = IncidentStatus.OPEN
                    incident.resolved_at = None
            for reference in finding.evidence:
                if self._evidence.get_by_reference(db, incident.id, reference.entity_type, reference.entity_id) is None:
                    self._evidence.add(
                        db,
                        IncidentEvidence(
                            incident_id=incident.id,
                            evidence_type=reference.evidence_type,
                            entity_type=reference.entity_type,
                            entity_id=reference.entity_id,
                            relationship=reference.relationship,
                        ),
                    )
            db.flush()
        return incident

    def _persist_many(self, db: Session, findings: list[ControlFinding]) -> list[Incident]:
        return [incident for finding in findings if (incident := self._persist(db, finding)) is not None]

    def _expected_settlement(self, db: Session, settlement: Settlement) -> tuple[int, tuple[EvidenceReference, ...]]:
        payments = [payment for payment in self._state.payments(db) if payment.currency == settlement.currency]
        refunds = [refund for refund in self._state.refunds(db) if refund.currency == settlement.currency and refund.status is RefundStatus.PROCESSED]
        expected = sum(payment.amount for payment in payments if payment.status in {PaymentStatus.CAPTURED, PaymentStatus.REFUNDED})
        expected -= sum(refund.amount for refund in refunds)
        expected -= settlement.fees + settlement.tax
        evidence = tuple(EvidenceReference("Payment", str(payment.id), "payment", "contributes_to_expected") for payment in payments)
        evidence += tuple(EvidenceReference("Refund", str(refund.id), "refund", "reduces_expected") for refund in refunds)
        return expected, evidence

    def _incident_exposure_key(self, db: Session, incident: Incident) -> str:
        references = self._state.incident_evidence(db, incident.id)
        preferred = next(
            (reference for reference in references if reference.entity_type in {"Payment", "Refund", "Settlement"}),
            None,
        )
        return f"{preferred.entity_type}:{preferred.entity_id}" if preferred else f"Incident:{incident.id}"

    @staticmethod
    def _severity(exposure: int | None) -> IncidentSeverity:
        if exposure is None or exposure <= 0:
            return IncidentSeverity.LOW
        if exposure >= 100_000:
            return IncidentSeverity.CRITICAL
        if exposure >= 10_000:
            return IncidentSeverity.HIGH
        return IncidentSeverity.MEDIUM


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)