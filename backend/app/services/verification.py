"""Deterministic post-execution control verification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.enums import IncidentStatus, IncidentType, PaymentStatus, RefundStatus
from app.engines.controls import ControlEngine
from app.models import ActionExecution, Incident, Payment, PaymentStateTransition, Refund, Settlement
from app.repositories.execution import ExecutionRepository
from app.services.audit import AuditService


class VerificationOutcome:
    VERIFIED_RESOLVED = "VERIFIED_RESOLVED"
    VERIFIED_STILL_ACTIVE = "VERIFIED_STILL_ACTIVE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


@dataclass(frozen=True)
class VerificationResult:
    outcome: str
    passed: bool
    reason: str


class VerificationService:
    def __init__(self, repository: ExecutionRepository | None = None, controls: ControlEngine | None = None, audit: AuditService | None = None) -> None:
        self._repository = repository or ExecutionRepository()
        self._controls = controls or ControlEngine()
        self._audit = audit or AuditService(self._repository)

    def verify(self, execution: ActionExecution, incident: Incident) -> VerificationResult:
        state = execution.after_state or {}
        expected = {"RECONCILE_ADJUSTMENT": "adjustment_recorded", "FLAG_FOR_REVIEW": "review_required", "REQUEST_EVIDENCE": "evidence_requested", "ESCALATE_INCIDENT": "escalated"}.get(state.get("action_type"))
        if expected is None or state.get(expected) is not True or state.get("incident_id") != str(incident.id):
            return VerificationResult(VerificationOutcome.VERIFICATION_FAILED, False, "Expected controlled internal state was not recorded.")
        return VerificationResult(VerificationOutcome.VERIFIED_RESOLVED, True, f"Verified internal state {expected}.")

    def verify_execution(self, db: Session, execution_id: str) -> VerificationResult:
        execution = self._repository.get_execution(db, execution_id)
        if execution is None:
            return VerificationResult(VerificationOutcome.VERIFICATION_FAILED, False, "Action execution was not found.")
        proposal = self._repository.get_proposal(db, execution.action_proposal_id)
        incident = proposal and self._repository.get_incident(db, proposal.incident_id)
        if proposal is None or incident is None:
            return VerificationResult(VerificationOutcome.VERIFICATION_FAILED, False, "Linked action proposal or incident was not found.")
        state = execution.after_state or {}
        stored = state.get("verification_outcome")
        if execution.verified_at is not None and stored in {VerificationOutcome.VERIFIED_RESOLVED, VerificationOutcome.VERIFIED_STILL_ACTIVE, VerificationOutcome.VERIFICATION_FAILED}:
            return VerificationResult(stored, stored == VerificationOutcome.VERIFIED_RESOLVED, state.get("verification_reason", "Existing verification returned."))
        transaction = db.begin_nested() if db.in_transaction() else db.begin()
        with transaction:
            result = self._recheck_control(db, execution, incident)
            execution.after_state = {**state, "verification_outcome": result.outcome, "verification_reason": result.reason}
            execution.verified_at = datetime.now(UTC)
            if result.outcome == VerificationOutcome.VERIFIED_RESOLVED:
                incident.status = IncidentStatus.RESOLVED
                incident.resolved_at = datetime.now(UTC)
                self._record_once(db, "VERIFICATION_PASSED", execution, proposal, result.reason)
                self._record_once(db, "INCIDENT_RESOLVED", execution, proposal, "Control no longer triggers.")
            else:
                incident.status = IncidentStatus.ACTION_REQUIRED
                incident.resolved_at = None
                self._record_once(db, "VERIFICATION_FAILED", execution, proposal, result.reason)
                self._record_once(db, "INCIDENT_REOPENED", execution, proposal, "Control remains active or verification failed.")
            db.flush()
        return result

    def _recheck_control(self, db: Session, execution: ActionExecution, incident: Incident) -> VerificationResult:
        evidence = self._repository.get_incident_evidence(db, incident.id)
        state = execution.after_state or {}
        expected = {
            "RECONCILE_ADJUSTMENT": "adjustment_recorded",
            "FLAG_FOR_REVIEW": "review_required",
            "REQUEST_EVIDENCE": "evidence_requested",
            "ESCALATE_INCIDENT": "escalated",
        }.get(state.get("action_type"))
        if expected is None or state.get(expected) is not True or state.get("incident_id") != str(incident.id):
            return self._failed("Expected controlled internal state was not recorded.")
        if incident.incident_type is IncidentType.SETTLEMENT_DISCREPANCY:
            settlement = self._entity(evidence, "Settlement", Settlement, db)
            if settlement is None:
                return self._failed("Required settlement evidence is unavailable.")
            expected = self._expected_settlement(db, settlement)
            active = self._controls.settlement_discrepancy(settlement, expected_amount=expected, detected_at=datetime.now(UTC)) is not None
        elif incident.incident_type is IncidentType.REFUND_FINANCIAL_DRIFT:
            refund = self._entity(evidence, "Refund", Refund, db)
            payment = self._entity(evidence, "Payment", Payment, db)
            if refund is None or payment is None:
                return self._failed("Required refund or payment evidence is unavailable.")
            active = self._controls.refund_financial_drift(refund, payment, detected_at=datetime.now(UTC)) is not None
        elif incident.incident_type is IncidentType.SETTLEMENT_CREDIT_DELAY:
            settlement = self._entity(evidence, "Settlement", Settlement, db)
            if settlement is None:
                return self._failed("Required settlement evidence is unavailable.")
            active = self._controls.settlement_credit_delay(settlement, self._repository.get_bank_transactions(db), detected_at=datetime.now(UTC)) is not None
        elif incident.incident_type is IncidentType.PAYMENT_STATE_CONFLICT:
            transition = self._entity(evidence, "PaymentStateTransition", PaymentStateTransition, db)
            payment = self._entity(evidence, "Payment", Payment, db)
            if transition is None or payment is None:
                return self._failed("Required payment transition evidence is unavailable.")
            active = self._controls.event_integrity(transition, payment) is not None
        else:
            state = execution.after_state or {}
            expected = {"RECONCILE_ADJUSTMENT": "adjustment_recorded", "FLAG_FOR_REVIEW": "review_required", "REQUEST_EVIDENCE": "evidence_requested", "ESCALATE_INCIDENT": "escalated"}.get(state.get("action_type"))
            if expected is None or state.get(expected) is not True:
                return self._failed("Expected controlled internal state was not recorded.")
            return VerificationResult(VerificationOutcome.VERIFIED_RESOLVED, True, f"Verified internal state {expected}.")
        if active:
            return VerificationResult(VerificationOutcome.VERIFIED_STILL_ACTIVE, False, "The originating deterministic control still triggers.")
        return VerificationResult(VerificationOutcome.VERIFIED_RESOLVED, True, "The originating deterministic control no longer triggers.")

    @staticmethod
    def _entity(evidence: list[object], entity_type: str, model: object, db: Session) -> object | None:
        reference = next((item for item in evidence if item.entity_type == entity_type), None)
        if reference is None:
            return None
        try:
            return db.get(model, UUID(reference.entity_id))
        except ValueError:
            return None

    def _expected_settlement(self, db: Session, settlement: Settlement) -> int:
        payments = [item for item in self._repository.get_payments(db) if item.currency == settlement.currency]
        refunds = [item for item in self._repository.get_refunds(db) if item.currency == settlement.currency and item.status is RefundStatus.PROCESSED]
        return sum(item.amount for item in payments if item.status in {PaymentStatus.CAPTURED, PaymentStatus.REFUNDED}) - sum(item.amount for item in refunds) - settlement.fees - settlement.tax

    @staticmethod
    def _failed(reason: str) -> VerificationResult:
        return VerificationResult(VerificationOutcome.VERIFICATION_FAILED, False, reason)

    def _record_once(self, db: Session, action_type: str, execution: ActionExecution, proposal: object, reason: str) -> None:
        self._audit.record_once(db, action_type=action_type, entity_type="ActionExecution", entity_id=execution.id, actor_type="SYSTEM", actor_id="verification-service", reason=reason, metadata={"incident_id": str(proposal.incident_id), "action_proposal_id": str(proposal.id), "action_execution_id": str(execution.id)})