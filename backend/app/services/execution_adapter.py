"""Provider-independent adapters for controlled internal actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.models import ActionProposal, Incident


class AdapterExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdapterResult:
    provider_reference: str | None
    after_state: dict[str, Any]


class ActionExecutionAdapter(Protocol):
    def execute(self, proposal: ActionProposal, incident: Incident) -> AdapterResult:
        """Perform only an approved controlled internal action."""


class InProcessActionAdapter:
    """Safe V1 adapter; it records internal state and calls no external API."""

    SUPPORTED_ACTIONS = frozenset({
        "RECONCILE_ADJUSTMENT", "FLAG_FOR_REVIEW", "REQUEST_EVIDENCE", "ESCALATE_INCIDENT",
    })

    def execute(self, proposal: ActionProposal, incident: Incident) -> AdapterResult:
        if proposal.action_type not in self.SUPPORTED_ACTIONS:
            raise AdapterExecutionError(f"Unsupported executable action: {proposal.action_type}")
        state = {
            "action_type": proposal.action_type,
            "incident_id": str(incident.id),
            "recorded": True,
        }
        if proposal.action_type == "RECONCILE_ADJUSTMENT":
            state["adjustment_recorded"] = True
        elif proposal.action_type == "FLAG_FOR_REVIEW":
            state["review_required"] = True
        elif proposal.action_type == "REQUEST_EVIDENCE":
            state["evidence_requested"] = True
        elif proposal.action_type == "ESCALATE_INCIDENT":
            state["escalated"] = True
        return AdapterResult(provider_reference=None, after_state=state)