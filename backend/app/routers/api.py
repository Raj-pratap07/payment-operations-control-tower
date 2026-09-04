"""Thin REST API for the payment operations control tower."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.enums import ActionProposalStatus, IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus
from app.database.database import get_db
from app.models import ActionProposal, Incident
from app.policies.engine import PolicyEngine
from app.repositories.action_policy import ActionPolicyRepository
from app.repositories.api import APIRepository
from app.repositories.incidents import ControlStateRepository
from app.repositories.projections import PaymentRepository
from app.schemas.api import ActionResponse, ActorRequest, ApprovalResponse, DashboardSummary, EvidenceResponse, IncidentResponse, IncidentSummary, InvestigationResponse, PaymentJourneyResponse, PaymentResponse, PaymentTransitionResponse, RejectionRequest
from app.services.action_planning import ApprovalService
from app.services.investigator import InvestigationService


router = APIRouter()
api_repository = APIRepository()


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _incident_response(incident: Incident) -> IncidentResponse:
    return IncidentResponse.model_validate({**incident.__dict__, "evidence_count": len(incident.evidence_records)})


def _action_response(db: Session, action: ActionProposal) -> ActionResponse:
    decision = PolicyEngine().evaluate(
        proposal=action,
        incident=action.incident,
        evidence=list(action.incident.evidence_records),
        policy=ActionPolicyRepository().get_policy(db, action.action_type),
    )
    return ActionResponse.model_validate({**action.__dict__, "policy_decision": decision, "approvals": action.approvals, "executions": action.executions})


@router.get("/dashboard/summary", response_model=DashboardSummary)
def dashboard_summary(db: Session = Depends(get_db)) -> DashboardSummary:
    incidents = api_repository.incidents(db)
    actions = api_repository.actions(db)
    payments = ControlStateRepository().payments(db)
    open_incidents = [item for item in incidents if item.status not in {IncidentStatus.RESOLVED, IncidentStatus.DISMISSED}]
    critical = [item for item in open_incidents if item.severity is IncidentSeverity.CRITICAL]
    health = {state.value: sum(payment.status is state for payment in payments) for state in PaymentStatus}
    return DashboardSummary(
        payment_health=health,
        open_incident_count=len(open_incidents),
        critical_incident_count=len(critical),
        financial_exposure=sum(item.financial_exposure or 0 for item in open_incidents),
        unexplained_money=sum(item.financial_exposure or 0 for item in open_incidents),
        auto_approved_action_count=sum(item.status is ActionProposalStatus.APPROVED and not item.requires_approval for item in actions),
        auto_approvable_action_count=sum(item.status is ActionProposalStatus.PROPOSED and not item.requires_approval for item in actions),
        approval_required_action_count=sum(item.requires_approval and item.status is ActionProposalStatus.PROPOSED for item in actions),
        recent_critical_incidents=[IncidentSummary.model_validate(item) for item in critical[:5]],
    )


@router.get("/incidents", response_model=list[IncidentSummary])
def list_incidents(status_filter: IncidentStatus | None = Query(default=None, alias="status"), severity: IncidentSeverity | None = None, incident_type: IncidentType | None = None, db: Session = Depends(get_db)) -> list[IncidentSummary]:
    return [IncidentSummary.model_validate(item) for item in api_repository.incidents(db, status=status_filter, severity=severity, incident_type=incident_type)]


@router.get("/incidents/{incident_id}", response_model=IncidentResponse)
def get_incident(incident_id: UUID, db: Session = Depends(get_db)) -> IncidentResponse:
    incident = api_repository.incident(db, incident_id)
    if incident is None:
        raise _not_found("Incident was not found.")
    return _incident_response(incident)


@router.get("/incidents/{incident_id}/evidence", response_model=list[EvidenceResponse])
def get_incident_evidence(incident_id: UUID, db: Session = Depends(get_db)) -> list[EvidenceResponse]:
    incident = api_repository.incident(db, incident_id)
    if incident is None:
        raise _not_found("Incident was not found.")
    return [EvidenceResponse.model_validate(item) for item in incident.evidence_records]


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
def get_payment(payment_id: UUID, db: Session = Depends(get_db)) -> PaymentResponse:
    from app.repositories.projections import PaymentRepository
    payment = PaymentRepository().get_by_id(db, payment_id)
    if payment is None:
        raise _not_found("Payment was not found.")
    return PaymentResponse.model_validate(payment)


@router.get("/payments/{payment_id}/journey", response_model=PaymentJourneyResponse)
def get_payment_journey(payment_id: UUID, db: Session = Depends(get_db)) -> PaymentJourneyResponse:
    if PaymentRepository().get_by_id(db, payment_id) is None:
        raise _not_found("Payment was not found.")
    return PaymentJourneyResponse(payment_id=payment_id, transitions=[PaymentTransitionResponse.model_validate(item) for item in api_repository.payment_journey(db, payment_id)])


@router.post("/investigations/{incident_id}", response_model=InvestigationResponse)
def investigate(incident_id: UUID, db: Session = Depends(get_db)) -> InvestigationResponse:
    if api_repository.incident(db, incident_id) is None:
        raise _not_found("Incident was not found.")
    return InvestigationResponse.model_validate(InvestigationService().investigate(db, incident_id))


@router.get("/actions", response_model=list[ActionResponse])
def list_actions(status_filter: ActionProposalStatus | None = Query(default=None, alias="status"), incident_id: UUID | None = None, action_type: str | None = None, db: Session = Depends(get_db)) -> list[ActionResponse]:
    return [_action_response(db, item) for item in api_repository.actions(db, status=status_filter, incident_id=incident_id, action_type=action_type)]


@router.get("/actions/{action_id}", response_model=ActionResponse)
def get_action(action_id: UUID, db: Session = Depends(get_db)) -> ActionResponse:
    action = api_repository.action(db, action_id)
    if action is None:
        raise _not_found("Action proposal was not found.")
    return _action_response(db, action)


def _approval_action(db: Session, action_id: UUID, request: ActorRequest, *, reject: bool = False, reason: str | None = None) -> ApprovalResponse:
    action = api_repository.action(db, action_id)
    if action is None:
        raise _not_found("Action proposal was not found.")
    approval = api_repository.pending_approval(db, action_id)
    if approval is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A pending approval was not found for this action.")
    service = ApprovalService()
    try:
        result = service.reject(db, approval.id, reason=reason) if reject else service.approve(db, approval.id, approved_by=request.actor_id)
        db.commit()
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return ApprovalResponse.model_validate(result)


@router.post("/actions/{action_id}/approve", response_model=ApprovalResponse)
def approve_action(action_id: UUID, request: ActorRequest, db: Session = Depends(get_db)) -> ApprovalResponse:
    return _approval_action(db, action_id, request)


@router.post("/actions/{action_id}/reject", response_model=ApprovalResponse)
def reject_action(action_id: UUID, request: RejectionRequest, db: Session = Depends(get_db)) -> ApprovalResponse:
    return _approval_action(db, action_id, request, reject=True, reason=request.reason)


@router.get("/audit/{entity_type}/{entity_id}", response_model=list[dict[str, object]])
def get_audit(entity_type: str, entity_id: str, db: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [{"id": item.id, "actor_type": item.actor_type, "actor_id": item.actor_id, "action_type": item.action_type, "entity_type": item.entity_type, "entity_id": item.entity_id, "reason": item.reason, "evidence": item.evidence, "metadata": item.metadata_, "created_at": item.created_at} for item in api_repository.audit(db, entity_type, entity_id)]