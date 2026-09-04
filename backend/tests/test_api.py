from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.database.database import SessionLocal
from app.main import app
from app.models import ActionProposal, Approval, AuditEvent, FinancialEvent, Incident, IncidentEvidence, Payment, PaymentStateTransition, Policy
from app.core.enums import ActionProposalStatus, ApprovalStatus, IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus


@pytest.fixture
def api_records() -> dict[str, object]:
    incident = Incident(
        incident_code=f"API:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.CRITICAL, status=IncidentStatus.OPEN, title="API settlement issue",
        description="API test incident", financial_exposure=2500, currency="INR", detected_at=datetime.now(UTC),
    )
    payment = Payment(provider_payment_id=f"pay_api_{uuid4()}", provider_order_id="order_api", amount=2500, currency="INR", status=PaymentStatus.CAPTURED)
    event = FinancialEvent(source="API_TEST", external_event_id=f"event_api_{uuid4()}", event_type="payment.captured", occurred_at=datetime.now(UTC), received_at=datetime.now(UTC), signature_valid=True, raw_payload={})
    db = SessionLocal()
    db.add_all((incident, payment, event))
    db.flush()
    evidence = IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Payment", entity_id=str(payment.id), relationship="supports")
    transition = PaymentStateTransition(payment_id=payment.id, financial_event_id=event.id, from_state=PaymentStatus.AUTHORIZED, to_state=PaymentStatus.CAPTURED, occurred_at=datetime.now(UTC))
    policy = Policy(name=f"API policy {uuid4()}", action_type="FLAG_FOR_REVIEW", max_amount=10000, min_confidence=Decimal("0.90"), requires_approval=True, is_active=True)
    action = ActionProposal(incident_id=incident.id, action_type="FLAG_FOR_REVIEW", description="Review incident", amount=2500, currency="INR", confidence=Decimal("0.95"), requires_approval=True, status=ActionProposalStatus.PROPOSED, created_by="test")
    db.add_all((evidence, transition, policy, action))
    db.flush()
    approval = Approval(action_proposal_id=action.id, requested_by="ops", status=ApprovalStatus.PENDING, requested_at=datetime.now(UTC))
    audit = AuditEvent(actor_type="SYSTEM", actor_id="test", action_type="TEST", entity_type="Incident", entity_id=str(incident.id), created_at=datetime(2026, 1, 1, tzinfo=UTC))
    db.add_all((approval, audit))
    db.commit()
    values = {"incident": incident, "payment": payment, "action": action, "approval": approval, "event": event}
    try:
        yield values
    finally:
        db.execute(delete(AuditEvent).where(AuditEvent.entity_id == str(incident.id)))
        db.execute(delete(Approval).where(Approval.action_proposal_id == action.id))
        db.execute(delete(ActionProposal).where(ActionProposal.id == action.id))
        db.execute(delete(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id))
        db.execute(delete(PaymentStateTransition).where(PaymentStateTransition.payment_id == payment.id))
        db.execute(delete(FinancialEvent).where(FinancialEvent.id == event.id))
        db.execute(delete(Payment).where(Payment.id == payment.id))
        db.execute(delete(Policy).where(Policy.id == policy.id))
        db.execute(delete(Incident).where(Incident.id == incident.id))
        db.commit()
        db.close()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_dashboard_and_incident_endpoints(client: TestClient, api_records: dict[str, object]) -> None:
    incident = api_records["incident"]
    response = client.get("/dashboard/summary")
    assert response.status_code == 200
    assert response.json()["critical_incident_count"] >= 1
    assert client.get("/incidents", params={"severity": "CRITICAL"}).status_code == 200
    detail = client.get(f"/incidents/{incident.id}")
    assert detail.status_code == 200
    assert detail.json()["financial_exposure"] == 2500
    evidence = client.get(f"/incidents/{incident.id}/evidence")
    assert evidence.status_code == 200
    assert evidence.json()[0]["entity_id"] == str(api_records["payment"].id)


def test_dashboard_cors_preflight_and_get(client: TestClient, api_records: dict[str, object]) -> None:
    preflight = client.options(
        "/dashboard/summary",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "GET" in preflight.headers["access-control-allow-methods"]
    assert client.get("/dashboard/summary").status_code == 200


def test_payment_and_journey_endpoints(client: TestClient, api_records: dict[str, object]) -> None:
    payment = api_records["payment"]
    assert client.get(f"/payments/{payment.id}").json()["provider_payment_id"] == payment.provider_payment_id
    journey = client.get(f"/payments/{payment.id}/journey")
    assert journey.status_code == 200
    assert journey.json()["transitions"][0]["financial_event_id"] == str(api_records["event"].id)


def test_investigation_returns_provider_independent_structured_result(client: TestClient, api_records: dict[str, object]) -> None:
    response = client.post(f"/investigations/{api_records['incident'].id}")
    assert response.status_code == 200
    assert response.json()["confidence"] == 0.0
    assert response.json()["root_cause"] == "Undetermined"


def test_action_endpoints_and_approval_lifecycle(client: TestClient, api_records: dict[str, object]) -> None:
    action = api_records["action"]
    assert client.get("/actions", params={"incident_id": str(api_records["incident"].id)}).status_code == 200
    detail = client.get(f"/actions/{action.id}")
    assert detail.status_code == 200
    assert detail.json()["policy_decision"]["outcome"] == "REQUIRE_APPROVAL"
    approved = client.post(f"/actions/{action.id}/approve", json={"actor_id": "operator-1"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"


def test_reject_action_preserves_service_lifecycle(client: TestClient, api_records: dict[str, object]) -> None:
    action = api_records["action"]
    db = SessionLocal()
    replacement = Approval(action_proposal_id=action.id, requested_by="ops", status=ApprovalStatus.PENDING, requested_at=datetime.now(UTC))
    action.status = ActionProposalStatus.PROPOSED
    db.merge(action)
    db.add(replacement)
    db.commit()
    response = client.post(f"/actions/{action.id}/reject", json={"actor_id": "operator-2", "reason": "Insufficient evidence"})
    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    db.close()


def test_audit_and_404_and_invalid_request(client: TestClient, api_records: dict[str, object]) -> None:
    audit = client.get(f"/audit/Incident/{api_records['incident'].id}")
    assert audit.status_code == 200
    assert audit.json()[0]["action_type"] == "TEST"
    assert client.get(f"/incidents/{uuid4()}").status_code == 404
    assert client.get("/incidents/not-a-uuid").status_code == 422
    assert client.post(f"/actions/{api_records['action'].id}/approve", json={}).status_code == 422


def test_read_api_does_not_mutate_payment_projection(client: TestClient, api_records: dict[str, object]) -> None:
    payment = api_records["payment"]
    before = payment.status
    client.get(f"/payments/{payment.id}")
    client.get(f"/payments/{payment.id}/journey")
    with SessionLocal() as db:
        assert db.get(Payment, payment.id).status is before