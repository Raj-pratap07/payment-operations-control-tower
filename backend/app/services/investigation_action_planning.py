"""Integration service connecting validated investigation to action planning and policy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus
from app.models import ActionProposal
from app.policies.engine import PolicyDecision, PolicyOutcome
from app.schemas.investigation import InvestigationOutput
from app.services.action_planning import (
    ActionPlanner,
    ActionPlanningError,
    ApprovalService,
    PolicyEvaluationService,
    UnsupportedActionError,
)


class InvestigationActionError(RuntimeError):
    """Base error for investigation-to-action integration failures."""


class MissingInvestigationError(InvestigationActionError):
    """Raised when investigation output is required but missing."""


class InvalidActionTypeError(InvestigationActionError):
    """Raised when the recommended action type is not supported."""


class PolicyRejectionError(InvestigationActionError):
    """Raised when policy rejects the action proposal."""


@dataclass(frozen=True)
class InvestigationActionResult:
    """Result of connecting investigation to action planning."""

    proposal: ActionProposal
    policy_decision: PolicyDecision
    approval_id: UUID | None


class InvestigationActionService:
    """Orchestrates: InvestigationOutput → ActionProposal → Policy → Approval."""

    def __init__(
        self,
        planner: ActionPlanner | None = None,
        policy_service: PolicyEvaluationService | None = None,
        approval_service: ApprovalService | None = None,
    ) -> None:
        self._planner = planner or ActionPlanner()
        self._policy_service = policy_service or PolicyEvaluationService()
        self._approval_service = approval_service or ApprovalService()

    def create_proposal_from_investigation(
        self,
        db: Session,
        investigation: InvestigationOutput,
        *,
        action_type: str | None = None,
        created_by: str = "ai_investigator",
    ) -> InvestigationActionResult:
        """Create an action proposal from a validated investigation and evaluate policy.

        This method:
        1. Validates the investigation has a supported recommended action
        2. Creates an ActionProposal via the existing ActionPlanner
        3. Evaluates policy via the existing PolicyEvaluationService
        4. Creates approval if required via the existing ApprovalService

        Args:
            db: Database session
            investigation: Validated investigation output
            action_type: Optional action type override; if None, inferred from investigation
            created_by: Identifier for the proposal creator

        Returns:
            InvestigationActionResult with proposal, policy decision, and optional approval

        Raises:
            MissingInvestigationError: If investigation is None
            InvalidActionTypeError: If action type is not supported
            PolicyRejectionError: If policy rejects the proposal
            ActionPlanningError: For other planning failures
        """
        if investigation is None:
            raise MissingInvestigationError("Investigation output is required.")

        resolved_action_type = action_type or self._infer_action_type(investigation)
        if resolved_action_type is None:
            raise InvalidActionTypeError(
                "Could not determine a supported action type from investigation. "
                f"Supported types: {sorted(ActionPlanner(None).SUPPORTED_ACTIONS if hasattr(ActionPlanner, 'SUPPORTED_ACTIONS') else [])}"
            )

        try:
            proposal = self._planner.create_proposal(
                db,
                investigation,
                action_type=resolved_action_type,
                created_by=created_by,
            )
        except UnsupportedActionError as error:
            raise InvalidActionTypeError(str(error)) from error
        except ActionPlanningError:
            raise

        decision = self._policy_service.evaluate(db, proposal.id)

        approval_id: UUID | None = None
        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            approval = self._approval_service.request(
                db,
                proposal.id,
                requested_by=created_by,
            )
            approval_id = approval.id
        elif decision.outcome == PolicyOutcome.REJECT:
            pass

        db.flush()

        return InvestigationActionResult(
            proposal=proposal,
            policy_decision=decision,
            approval_id=approval_id,
        )

    def _infer_action_type(self, investigation: InvestigationOutput) -> str | None:
        """Infer action type from investigation recommended_action field.

        This is a simple heuristic mapping. The backend remains authoritative
        for action type validation via ActionPlanner.SUPPORTED_ACTIONS.
        """
        from app.services.action_planning import SUPPORTED_ACTIONS

        recommended = investigation.recommended_action.upper()

        for action_type in SUPPORTED_ACTIONS:
            if action_type in recommended:
                return action_type

        if any(
            keyword in recommended
            for keyword in ["RECONCILE", "ADJUSTMENT", "DISCREPANCY"]
        ):
            return "RECONCILE_ADJUSTMENT"
        if any(keyword in recommended for keyword in ["FLAG", "REVIEW"]):
            return "FLAG_FOR_REVIEW"
        if any(keyword in recommended for keyword in ["EVIDENCE", "REQUEST"]):
            return "REQUEST_EVIDENCE"
        if any(keyword in recommended for keyword in ["ESCALATE", "URGENT"]):
            return "ESCALATE_INCIDENT"

        return None
