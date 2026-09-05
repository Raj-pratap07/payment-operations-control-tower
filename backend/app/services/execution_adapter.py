"""Provider-independent adapters for controlled internal actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ActionProposal, Incident, IncidentEvidence, Settlement
from app.services.audit import AuditService


class AdapterExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdapterResult:
    provider_reference: str | None
    after_state: dict[str, Any]


class ActionExecutionAdapter(Protocol):
    def execute(self, db: Session, proposal: ActionProposal, incident: Incident) -> AdapterResult:
        """Perform only an approved controlled internal action.

        The adapter receives the active database session so a controlled
        internal correction can have a real, verifiable effect on domain
        state (never on immutable raw financial events).
        """


class InProcessActionAdapter:
    """Safe V1 adapter; performs controlled internal corrections and calls no external API."""

    SUPPORTED_ACTIONS = frozenset({
        "RECONCILE_ADJUSTMENT", "FLAG_FOR_REVIEW", "REQUEST_EVIDENCE", "ESCALATE_INCIDENT",
    })

    def __init__(self, audit: AuditService | None = None) -> None:
        self._audit = audit or AuditService()

    def execute(self, db: Session, proposal: ActionProposal, incident: Incident) -> AdapterResult:
        if proposal.action_type not in self.SUPPORTED_ACTIONS:
            raise AdapterExecutionError(f"Unsupported executable action: {proposal.action_type}")
        state = {
            "action_type": proposal.action_type,
            "incident_id": str(incident.id),
            "recorded": True,
        }
        if proposal.action_type == "RECONCILE_ADJUSTMENT":
            state.update(self._apply_reconcile_adjustment(db, proposal, incident))
        elif proposal.action_type == "FLAG_FOR_REVIEW":
            state["review_required"] = True
        elif proposal.action_type == "REQUEST_EVIDENCE":
            state["evidence_requested"] = True
        elif proposal.action_type == "ESCALATE_INCIDENT":
            state["escalated"] = True
        return AdapterResult(provider_reference=None, after_state=state)

    def _apply_reconcile_adjustment(
        self,
        db: Session,
        proposal: ActionProposal,
        incident: Incident,
    ) -> dict[str, Any]:
        """Apply the approved reconciliation adjustment to the settlement projection.

        RECONCILE_ADJUSTMENT is a controlled internal correction to the
        settlement projection that was derived from an immutable financial
        event.  Correcting the projection is the authoritative effect the
        settlement-discrepancy control observes; the raw FinancialEvent
        payload is never mutated and no external payment-provider API is
        called.
        """
        reference = db.scalar(
            select(IncidentEvidence).where(
                IncidentEvidence.incident_id == incident.id,
                IncidentEvidence.entity_type == "Settlement",
            )
        )
        if reference is None:
            raise AdapterExecutionError("RECONCILE_ADJUSTMENT requires settlement evidence for the incident.")
        settlement = db.get(Settlement, UUID(reference.entity_id))
        if settlement is None:
            raise AdapterExecutionError("RECONCILE_ADJUSTMENT settlement record was not found.")
        if proposal.amount is None or proposal.amount <= 0:
            raise AdapterExecutionError("RECONCILE_ADJUSTMENT requires a positive adjustment amount.")
        if proposal.currency and settlement.currency != proposal.currency:
            raise AdapterExecutionError("RECONCILE_ADJUSTMENT currency does not match the settlement.")

        prior_amount = settlement.amount
        adjustment_amount = proposal.amount
        settlement.amount = prior_amount + adjustment_amount
        settlement.updated_at = datetime.now(UTC)

        self._audit.record(
            db,
            action_type="RECONCILE_ADJUSTMENT_APPLIED",
            entity_type="Settlement",
            entity_id=settlement.id,
            actor_type="SYSTEM",
            actor_id="action-executor",
            reason=f"Applied approved reconciliation adjustment of {adjustment_amount} to the settlement projection.",
            evidence={
                "prior_amount": prior_amount,
                "adjusted_amount": settlement.amount,
                "adjustment_amount": adjustment_amount,
                "currency": settlement.currency,
                "action_proposal_id": str(proposal.id),
            },
            metadata={"incident_id": str(incident.id), "action_type": proposal.action_type},
        )
        return {
            "adjustment_recorded": True,
            "settlement_id": str(settlement.id),
            "prior_settlement_amount": prior_amount,
            "adjusted_settlement_amount": settlement.amount,
            "adjustment_amount": adjustment_amount,
        }