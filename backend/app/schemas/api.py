"""HTTP request and response contracts for the control tower API."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ActionProposalStatus, ApprovalStatus, IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus
from app.schemas.investigation import InvestigationEvidence


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EvidenceResponse(APIModel):
    id: UUID
    evidence_type: str
    entity_type: str
    entity_id: str
    relationship: str
    created_at: datetime


class IncidentSummary(APIModel):
    id: UUID
    incident_code: str
    incident_type: IncidentType
    severity: IncidentSeverity
    status: IncidentStatus
    title: str
    financial_exposure: int | None
    currency: str | None
    detected_at: datetime


class IncidentResponse(IncidentSummary):
    description: str | None
    resolved_at: datetime | None
    evidence_count: int


class PaymentResponse(APIModel):
    id: UUID
    provider_payment_id: str
    provider_order_id: str | None
    amount: int
    currency: str
    status: PaymentStatus
    created_at: datetime
    authorized_at: datetime | None
    captured_at: datetime | None
    failed_at: datetime | None
    updated_at: datetime


class PaymentTransitionResponse(APIModel):
    id: UUID
    from_state: PaymentStatus | None
    to_state: PaymentStatus
    financial_event_id: UUID
    occurred_at: datetime
    created_at: datetime


class PaymentJourneyResponse(APIModel):
    payment_id: UUID
    transitions: list[PaymentTransitionResponse]


class PolicyDecisionResponse(APIModel):
    outcome: str
    reason: str
    policy_id: UUID | None
    evaluated_conditions: dict[str, object]


class ApprovalResponse(APIModel):
    id: UUID
    requested_by: str
    approved_by: str | None
    status: ApprovalStatus
    reason: str | None
    requested_at: datetime
    approved_at: datetime | None


class ExecutionResponse(APIModel):
    id: UUID
    execution_id: str
    status: str
    provider_reference: str | None
    error: str | None
    executed_at: datetime | None
    verified_at: datetime | None


class ActionResponse(APIModel):
    id: UUID
    incident_id: UUID
    action_type: str
    description: str
    amount: int | None
    currency: str | None
    confidence: float | None
    requires_approval: bool
    status: ActionProposalStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    policy_decision: PolicyDecisionResponse | None = None
    approvals: list[ApprovalResponse] = Field(default_factory=list)
    executions: list[ExecutionResponse] = Field(default_factory=list)


class VerificationResultResponse(APIModel):
    outcome: str
    passed: bool
    reason: str


class ExecutionActionResult(APIModel):
    success: bool
    reason: str
    execution: ExecutionResponse | None = None
    verification: VerificationResultResponse | None = None
    action: ActionResponse | None = None


class ActorRequest(BaseModel):
    actor_id: str = Field(min_length=1, max_length=255)


class RejectionRequest(ActorRequest):
    reason: str | None = Field(default=None, max_length=2000)


class DashboardSummary(APIModel):
    payment_health: dict[str, int]
    open_incident_count: int
    critical_incident_count: int
    financial_exposure: int
    unexplained_money: int
    auto_approved_action_count: int
    auto_approvable_action_count: int
    approval_required_action_count: int
    recent_critical_incidents: list[IncidentSummary]


class InvestigationResponse(APIModel):
    incident_id: UUID
    root_cause: str
    summary: str
    observed_facts: list[str]
    derived_findings: list[str]
    evidence: list[InvestigationEvidence]
    financial_impact_minor: int
    unresolved_amount_minor: int
    recommended_action: str
    confidence: float
    uncertainties: list[str]


class CreateActionProposalRequest(BaseModel):
    action_type: str | None = Field(default=None, min_length=1, max_length=100, description="Optional action type override. If omitted, inferred from investigation.")


class ActionProposalResponse(APIModel):
    id: UUID
    incident_id: UUID
    action_type: str
    description: str
    amount: int | None
    currency: str | None
    confidence: float | None
    requires_approval: bool
    status: ActionProposalStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    policy_decision: PolicyDecisionResponse
    approval_id: UUID | None = None