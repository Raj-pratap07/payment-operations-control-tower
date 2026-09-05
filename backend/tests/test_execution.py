from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.provider import NoProvider
from app.core.enums import ActionProposalStatus, ApprovalStatus, IncidentSeverity, IncidentStatus, IncidentType
from app.database.database import engine
from app.models import ActionExecution, ActionProposal, Approval, AuditEvent, Incident, IncidentEvidence, Payment, Policy, Settlement
from app.policies.engine import PolicyOutcome
from app.services.action_planning import ApprovalService
from app.services.execution_adapter import AdapterExecutionError, AdapterResult, InProcessActionAdapter
from app.services.executor import ActionExecutor
from app.services.verification import VerificationResult, VerificationService


@pytest.fixture
def db_session() -> Session:
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def setup_proposal(db: Session, *, requires_approval: bool = False, status: IncidentStatus = IncidentStatus.OPEN) -> tuple[Incident, ActionProposal]:
    incident = Incident(
        incident_code=f"EXEC-INC-{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM, status=status, title="Execution test", financial_exposure=1000,
        currency="INR", detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()
    db.add(IncidentEvidence(
        incident_id=incident.id, evidence_type="financial", entity_type="Settlement",
        entity_id="SETTLEMENT_PLACEHOLDER", relationship="supports",
    ))
    settlement = Settlement(
        provider_settlement_id="SETTLEMENT_PLACEHOLDER", amount=1000, currency="INR", fees=0,
        tax=0, status="PROCESSED",
    )
    payment = Payment(
        provider_payment_id=f"payment-{uuid4()}", amount=1000, currency="INR", status="CAPTURED",
    )
    policy = Policy(
        name=f"Execution policy {uuid4()}", action_type="FLAG_FOR_REVIEW", max_amount=10000,
        min_confidence=Decimal("0.9000"), requires_approval=requires_approval, is_active=True,
    )
    proposal = ActionProposal(
        incident_id=incident.id, action_type="FLAG_FOR_REVIEW", description="Flag for review",
        amount=1000, currency="INR", confidence=Decimal("0.9500"), requires_approval=False,
        status=ActionProposalStatus.PROPOSED, created_by="test",
    )
    db.add_all((policy, proposal, settlement, payment))
    db.flush()
    db_session_evidence = db.scalar(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id))
    assert db_session_evidence is not None
    db_session_evidence.entity_id = str(settlement.id)
    db.flush()
    return incident, proposal


def audit_actions(db: Session, proposal: ActionProposal) -> set[str]:
    return set(db.scalars(select(AuditEvent.action_type).where(
        (AuditEvent.entity_id == str(proposal.id))
        | (AuditEvent.metadata_["action_proposal_id"].astext == str(proposal.id))
    )))


def test_authorized_execution_verifies_and_resolves_incident(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)

    result = ActionExecutor().execute(db_session, proposal.id)

    assert result.success is True
    assert result.execution is not None
    assert result.execution.status == "EXECUTED"
    assert result.execution.provider_reference is None
    assert incident.status is IncidentStatus.RESOLVED
    assert result.execution.verified_at is not None
    assert {"POLICY_AUTHORIZED", "EXECUTION_STARTED", "EXECUTION_COMPLETED", "VERIFICATION_PASSED", "INCIDENT_RESOLVED"}.issubset(audit_actions(db_session, proposal))


def test_unauthorized_policy_rejection_does_not_execute(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)
    policy = db_session.scalar(select(Policy).where(Policy.action_type == proposal.action_type))
    assert policy is not None
    policy.is_active = False

    result = ActionExecutor().execute(db_session, proposal.id)

    assert result.success is False
    assert result.execution is None
    assert db_session.scalar(select(func.count()).select_from(ActionExecution)) == 0
    assert incident.status is IncidentStatus.OPEN


def test_approval_required_is_blocked_until_approved(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session, requires_approval=True)

    blocked = ActionExecutor().execute(db_session, proposal.id)
    assert blocked.success is False
    assert "approval" in blocked.reason.lower()
    assert db_session.scalar(select(func.count()).select_from(ActionExecution)) == 0

    approval = ApprovalService().request(db_session, proposal.id, requested_by="operator")
    ApprovalService().approve(db_session, approval.id, approved_by="operator")
    allowed = ActionExecutor().execute(db_session, proposal.id)

    assert allowed.success is True
    assert incident.status is IncidentStatus.RESOLVED
    assert "APPROVAL_GRANTED" in audit_actions(db_session, proposal)


def test_duplicate_execution_returns_existing_result(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)
    executor = ActionExecutor()

    first = executor.execute(db_session, proposal.id)
    second = executor.execute(db_session, proposal.id)

    assert first.success is True and second.success is True
    assert first.execution is not None and second.execution is not None
    assert first.execution.id == second.execution.id
    assert db_session.scalar(select(func.count()).select_from(ActionExecution)) == 1


class FailingAdapter:
    def execute(self, db: Session, proposal: ActionProposal, incident: Incident) -> AdapterResult:
        raise AdapterExecutionError("controlled adapter failed")


def test_execution_failure_is_recorded_without_resolution(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)

    result = ActionExecutor(adapter=FailingAdapter()).execute(db_session, proposal.id)

    assert result.success is False
    assert result.execution is not None and result.execution.status == "FAILED"
    assert proposal.status is ActionProposalStatus.FAILED
    assert incident.status is IncidentStatus.OPEN
    assert "EXECUTION_FAILED" in audit_actions(db_session, proposal)


class MissingStateAdapter:
    def execute(self, db: Session, proposal: ActionProposal, incident: Incident) -> AdapterResult:
        return AdapterResult(provider_reference=None, after_state={"action_type": proposal.action_type})


def test_verification_failure_does_not_resolve_and_reopens(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)

    result = ActionExecutor(adapter=MissingStateAdapter()).execute(db_session, proposal.id)

    assert result.success is False
    assert result.verification is not None and result.verification.passed is False
    assert incident.status is IncidentStatus.ACTION_REQUIRED
    assert incident.resolved_at is None
    assert {"VERIFICATION_FAILED", "INCIDENT_REOPENED"}.issubset(audit_actions(db_session, proposal))


def test_unsupported_action_is_rejected(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)
    proposal.action_type = "ISSUE_REFUND"
    db_session.flush()

    result = ActionExecutor().execute(db_session, proposal.id)

    assert result.success is False
    assert "unsupported" in result.reason.lower()
    assert db_session.scalar(select(func.count()).select_from(ActionExecution)) == 0


@pytest.mark.parametrize("terminal_status", [
    ActionProposalStatus.REJECTED,
    ActionProposalStatus.FAILED,
    ActionProposalStatus.CANCELLED,
])
def test_terminal_proposal_states_cannot_execute(db_session: Session, terminal_status: ActionProposalStatus) -> None:
    incident, proposal = setup_proposal(db_session)
    proposal.status = terminal_status
    db_session.flush()

    result = ActionExecutor().execute(db_session, proposal.id)

    assert result.success is False
    assert terminal_status.value in result.reason
    assert db_session.scalar(select(func.count()).select_from(ActionExecution)) == 0
    assert incident.status is IncidentStatus.OPEN


def test_executed_proposal_without_execution_record_fails_closed(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)
    proposal.status = ActionProposalStatus.EXECUTED
    db_session.flush()

    result = ActionExecutor().execute(db_session, proposal.id)

    assert result.success is False
    assert "manual reconciliation" in result.reason
    assert db_session.scalar(select(func.count()).select_from(ActionExecution)) == 0
    assert incident.status is IncidentStatus.OPEN


def test_invalid_lifecycle_guard_does_not_mutate_financial_state(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)
    proposal.status = ActionProposalStatus.REJECTED
    before = (incident.status, incident.financial_exposure, proposal.status)
    db_session.flush()

    ActionExecutor().execute(db_session, proposal.id)

    db_session.expire_all()
    assert (incident.status, incident.financial_exposure, proposal.status) == before


def test_in_process_adapter_has_no_live_provider_capability(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)

    result = InProcessActionAdapter().execute(db_session, proposal, incident)

    assert result.provider_reference is None
    assert result.after_state["review_required"] is True


def test_audit_records_are_append_only(db_session: Session) -> None:
    incident, proposal = setup_proposal(db_session)
    ActionExecutor().execute(db_session, proposal.id)
    audit = db_session.scalar(select(AuditEvent).where(AuditEvent.entity_id == str(proposal.id)))
    assert audit is not None
    audit.reason = "tampered"
    with pytest.raises(Exception):
        db_session.flush()


def test_verification_service_requires_expected_state() -> None:
    incident = Incident(
        incident_code=f"VERIFY-{uuid4()}", incident_type=IncidentType.EVENT_INTEGRITY,
        severity=IncidentSeverity.LOW, status=IncidentStatus.OPEN, title="Verify", detected_at=datetime.now(UTC),
    )
    result = VerificationService().verify(
        ActionExecution(status="EXECUTED", after_state={"action_type": "FLAG_FOR_REVIEW"}), incident
    )
    assert result.passed is False


def test_no_ai_provider_or_execution_methods_are_used() -> None:
    assert isinstance(NoProvider(), NoProvider)
    assert not hasattr(NoProvider(), "execute")
    assert not hasattr(InProcessActionAdapter(), "refund")
    assert not hasattr(InProcessActionAdapter(), "payout")
