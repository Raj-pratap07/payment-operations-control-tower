"""Deterministic policy evaluation for action proposals."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.core.enums import IncidentStatus
from app.models import ActionProposal, Incident, IncidentEvidence, Policy


class PolicyOutcome:
    ALLOW_AUTO = "ALLOW_AUTO"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    REJECT = "REJECT"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str
    reason: str
    policy_id: UUID | None
    evaluated_conditions: dict[str, object]


class PolicyEngine:
    def evaluate(
        self,
        *,
        proposal: ActionProposal,
        incident: Incident,
        evidence: list[IncidentEvidence],
        policy: Policy | None,
    ) -> PolicyDecision:
        conditions: dict[str, object] = {
            "action_type_supported": proposal.action_type in {
                "RECONCILE_ADJUSTMENT", "FLAG_FOR_REVIEW", "REQUEST_EVIDENCE", "ESCALATE_INCIDENT"
            },
            "incident_actionable": incident.status not in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED},
            "evidence_available": bool(evidence),
            "policy_active": bool(policy and policy.is_active),
            "amount": proposal.amount,
            "maximum_amount": policy.max_amount if policy else None,
            "confidence": proposal.confidence,
            "minimum_confidence": policy.min_confidence if policy else None,
            "policy_requires_approval": policy.requires_approval if policy else None,
        }
        if not conditions["action_type_supported"]:
            return self._reject("Unsupported action type.", policy, conditions)
        if not conditions["incident_actionable"]:
            return self._reject("Incident is resolved or dismissed.", policy, conditions)
        if not conditions["evidence_available"]:
            return self._reject("At least one evidence record is required.", policy, conditions)
        if policy is None:
            return self._reject("No policy is configured for this action type.", None, conditions)
        if not policy.is_active:
            return self._reject("The policy is inactive.", policy, conditions)
        if proposal.amount is not None and policy.max_amount is not None and proposal.amount > policy.max_amount:
            return PolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, "Amount exceeds the policy auto-approval maximum.", policy.id, conditions)
        if proposal.confidence is not None and policy.min_confidence is not None and proposal.confidence < policy.min_confidence:
            return PolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, "Confidence is below the policy auto-approval minimum.", policy.id, conditions)
        if policy.requires_approval:
            return PolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, "The policy requires human approval.", policy.id, conditions)
        return PolicyDecision(PolicyOutcome.ALLOW_AUTO, "All policy conditions passed.", policy.id, conditions)

    @staticmethod
    def _reject(reason: str, policy: Policy | None, conditions: dict[str, object]) -> PolicyDecision:
        return PolicyDecision(PolicyOutcome.REJECT, reason, policy.id if policy else None, conditions)