"""Verification of controlled internal action results."""

from __future__ import annotations

from dataclasses import dataclass

from app.models import ActionExecution, Incident


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    reason: str


class VerificationService:
    def verify(self, execution: ActionExecution, incident: Incident) -> VerificationResult:
        state = execution.after_state or {}
        action_type = execution.action_proposal.action_type if execution.action_proposal else state.get("action_type")
        expected = {
            "RECONCILE_ADJUSTMENT": "adjustment_recorded",
            "FLAG_FOR_REVIEW": "review_required",
            "REQUEST_EVIDENCE": "evidence_requested",
            "ESCALATE_INCIDENT": "escalated",
        }.get(action_type)
        if expected is None:
            return VerificationResult(False, f"Unsupported action type for verification: {action_type}")
        if state.get(expected) is not True or state.get("incident_id") != str(incident.id):
            return VerificationResult(False, f"Expected internal state {expected} was not recorded.")
        if action_type == "FLAG_FOR_REVIEW" and incident.status.value not in {"ACTION_REQUIRED", "RESOLVED"}:
            return VerificationResult(False, "Incident does not reflect the review requirement.")
        return VerificationResult(True, f"Verified internal state {expected}.")