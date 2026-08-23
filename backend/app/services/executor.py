"""Controlled execution of already-authorized internal actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus, IncidentStatus
from app.models import ActionExecution, ActionProposal, AuditEvent
from app.policies.engine import PolicyOutcome
from app.repositories.execution import ExecutionRepository
from app.services.action_planning import PolicyEvaluationService, SUPPORTED_ACTIONS
from app.services.audit import AuditService
from app.services.execution_adapter import ActionExecutionAdapter, AdapterExecutionError, InProcessActionAdapter
from app.services.verification import VerificationResult, VerificationService


class ExecutionError(ValueError):
    pass


class InconsistentExecutionStateError(ExecutionError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    execution: ActionExecution | None
    verification: VerificationResult | None
    reason: str


class ActionExecutor:
    def __init__(
        self,
        repository: ExecutionRepository | None = None,
        policy_service: PolicyEvaluationService | None = None,
        adapter: ActionExecutionAdapter | None = None,
        verifier: VerificationService | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._repository = repository or ExecutionRepository()
        self._policy = policy_service or PolicyEvaluationService()
        self._adapter = adapter or InProcessActionAdapter()
        self._verifier = verifier or VerificationService()
        self._audit = audit or AuditService(self._repository)

    def execute(self, db: Session, proposal_id: UUID) -> ExecutionResult:
        execution_id = f"action-proposal:{proposal_id}"
        existing = self._repository.get_execution(db, execution_id)
        if existing is not None:
            verification = self._verify_existing(db, existing)
            return ExecutionResult(existing.status == "EXECUTED" and verification.passed, existing, verification, "Existing execution returned.")

        proposal = self._repository.get_proposal(db, proposal_id)
        if proposal is None:
            return ExecutionResult(False, None, None, f"Action proposal {proposal_id} was not found.")
        if proposal.action_type not in SUPPORTED_ACTIONS:
            return ExecutionResult(False, None, None, f"Unsupported action type: {proposal.action_type}")
        if proposal.status is ActionProposalStatus.EXECUTED:
            return ExecutionResult(False, None, None, "Proposal is EXECUTED but its execution record is missing; manual reconciliation is required.")
        if proposal.status in {
            ActionProposalStatus.REJECTED,
            ActionProposalStatus.FAILED,
            ActionProposalStatus.CANCELLED,
        }:
            return ExecutionResult(False, None, None, f"Proposal in {proposal.status.value} state cannot be executed.")
        incident = self._repository.get_incident(db, proposal.incident_id)
        if incident is None:
            return ExecutionResult(False, None, None, "The proposal incident was not found.")
        if incident.status in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}:
            return ExecutionResult(False, None, None, "The incident is not actionable.")

        decision = self._policy.evaluate(db, proposal.id)
        if decision.outcome == PolicyOutcome.REJECT:
            return ExecutionResult(False, None, None, decision.reason)
        if proposal.status is ActionProposalStatus.PROPOSED and decision.outcome == PolicyOutcome.ALLOW_AUTO:
            proposal.status = ActionProposalStatus.APPROVED
            self._persist_audit(db, "POLICY_AUTHORIZED", proposal, reason=decision.reason)
        elif proposal.status is ActionProposalStatus.PROPOSED and decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            return ExecutionResult(False, None, None, "Approval with approved human authorization is required.")
        elif proposal.status is ActionProposalStatus.APPROVED and decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            approval = self._repository.get_approval(db, proposal.id)
            if approval is None:
                return ExecutionResult(False, None, None, "Approval with approved human authorization is required.")
            self._persist_audit(db, "APPROVAL_GRANTED", proposal, reason="Approved human authorization found.")
        else:
            return ExecutionResult(False, None, None, "Proposal lifecycle state is not authorized for this policy decision.")

        transaction = db.begin_nested() if db.in_transaction() else db.begin()
        with transaction:
            proposal.status = ActionProposalStatus.EXECUTING
            self._persist_audit(db, "EXECUTION_STARTED", proposal, reason="Controlled internal execution started.")
            before_state = {"incident_status": incident.status.value}
            try:
                adapter_result = self._adapter.execute(proposal, incident)
            except Exception as error:
                proposal.status = ActionProposalStatus.FAILED
                execution = ActionExecution(
                    action_proposal_id=proposal.id, execution_id=execution_id, status="FAILED", error=str(error),
                )
                self._repository.add_execution(db, execution)
                db.flush()
                self._persist_audit(db, "EXECUTION_FAILED", proposal, reason=str(error))
                return ExecutionResult(False, execution, None, str(error))
            if proposal.action_type == "FLAG_FOR_REVIEW":
                incident.status = IncidentStatus.ACTION_REQUIRED
            execution = ActionExecution(
                action_proposal_id=proposal.id, execution_id=execution_id, status="EXECUTED",
                provider_reference=adapter_result.provider_reference, before_state=before_state,
                after_state=adapter_result.after_state, executed_at=datetime.now(UTC),
            )
            self._repository.add_execution(db, execution)
            proposal.status = ActionProposalStatus.EXECUTED
            db.flush()
            self._persist_audit(db, "EXECUTION_COMPLETED", proposal, reason="Controlled internal execution completed.")
            verification = self._verifier.verify(execution, incident)
            execution.verified_at = datetime.now(UTC)
            if verification.passed:
                incident.status = IncidentStatus.RESOLVED
                incident.resolved_at = datetime.now(UTC)
                self._persist_audit(db, "VERIFICATION_PASSED", proposal, reason=verification.reason)
                self._persist_audit(db, "INCIDENT_RESOLVED", proposal, reason="Execution was verified successfully.")
            else:
                incident.status = IncidentStatus.ACTION_REQUIRED
                self._persist_audit(db, "VERIFICATION_FAILED", proposal, reason=verification.reason)
                self._persist_audit(db, "INCIDENT_REOPENED", proposal, reason="Verified execution did not produce expected state.")
                return ExecutionResult(False, execution, verification, verification.reason)
        return ExecutionResult(True, execution, verification, verification.reason)

    def _verify_existing(self, db: Session, execution: ActionExecution) -> VerificationResult:
        proposal = self._repository.get_proposal(db, execution.action_proposal_id)
        if proposal is None:
            return VerificationResult(False, "Execution references a missing proposal.")
        incident = self._repository.get_incident(db, proposal.incident_id)
        if incident is None:
            return VerificationResult(False, "Execution references a missing incident.")
        return self._verifier.verify(execution, incident)

    def _persist_audit(self, db: Session, action_type: str, proposal: ActionProposal, *, reason: str) -> AuditEvent:
        return self._audit.record(
            db, action_type=action_type, entity_type="ActionProposal", entity_id=proposal.id,
            actor_type="SYSTEM", actor_id="action-executor", reason=reason,
            metadata={"incident_id": str(proposal.incident_id), "action_type": proposal.action_type},
        )