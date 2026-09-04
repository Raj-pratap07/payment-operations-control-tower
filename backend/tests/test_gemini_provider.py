import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.agents.provider import GeminiProvider, NoProvider, configured_provider
from app.core.config import settings
from app.core.enums import IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus
from app.database.database import engine
from app.models import Incident, IncidentEvidence, Payment
from app.schemas.investigation import InvestigationOutput
from app.services.investigator import InvestigationService, InvalidInvestigationOutputError


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


def records(db: Session) -> tuple[Incident, Payment]:
    payment = Payment(provider_payment_id=f"gemini-pay-{uuid4()}", amount=1000, currency="INR", status=PaymentStatus.CAPTURED)
    incident = Incident(
        incident_code=f"GEMINI:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM, status=IncidentStatus.OPEN, title="Gemini test incident",
        description="Test evidence", financial_exposure=100, currency="INR", detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add_all((payment, incident))
    db.flush()
    db.add(IncidentEvidence(incident_id=incident.id, evidence_type="financial", entity_type="Payment", entity_id=str(payment.id), relationship="supports"))
    db.flush()
    return incident, payment


def output(incident_id, payment_id: str) -> dict[str, object]:
    return {
        "incident_id": str(incident_id), "root_cause": "Settlement mismatch", "summary": "Evidence shows a mismatch.",
        "observed_facts": ["Payment was retrieved."], "derived_findings": ["Difference is deterministic."],
        "evidence": [{"entity_type": "Payment", "entity_id": payment_id, "relationship": "supports"}],
        "financial_impact_minor": 100, "unresolved_amount_minor": 100,
        "recommended_action": "Review the settlement.", "confidence": 0.8, "uncertainties": [],
    }


def interaction(*steps: object, output_text: str | None = None, identifier: str = "interaction-1") -> SimpleNamespace:
    return SimpleNamespace(id=identifier, steps=list(steps), output_text=output_text)


class FakeInteractions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return next(self.responses)


class FakeGeminiClient:
    def __init__(self, responses: list[object]) -> None:
        self.interactions = FakeInteractions(responses)


def final_response(value: object, identifier: str = "interaction-1") -> SimpleNamespace:
    return interaction(SimpleNamespace(type="model_output", content=[SimpleNamespace(type="text", text=json.dumps(value))]), identifier=identifier)


def test_successful_structured_investigation_uses_interactions_and_model(db_session: Session) -> None:
    incident, payment = records(db_session)
    client = FakeGeminiClient([final_response(output(incident.id, str(payment.id)))])

    result = InvestigationService(GeminiProvider("test-key", "gemini-3.6-flash", client)).investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    assert client.interactions.calls[0]["model"] == "gemini-3.6-flash"
    assert "response_format" in client.interactions.calls[0]
    assert client.interactions.calls[0]["tools"][0]["type"] == "function"


def test_function_call_executes_locally_and_result_returns_to_gemini(db_session: Session) -> None:
    incident, payment = records(db_session)
    call = SimpleNamespace(type="function_call", id="call-123", name="get_payment", arguments={"payment_id": str(payment.id)})
    client = FakeGeminiClient([interaction(call), final_response(output(incident.id, str(payment.id)), identifier="interaction-2")])

    result = InvestigationService(GeminiProvider("test-key", "gemini-3.6-flash", client), max_tool_calls=1).investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    second_call = client.interactions.calls[1]
    assert second_call["previous_interaction_id"] == "interaction-1"
    assert second_call["input"][0]["type"] == "function_result"
    assert second_call["input"][0]["call_id"] == "call-123"
    result_payload = json.loads(second_call["input"][0]["result"][0]["text"])
    assert result_payload["result"]["id"] == str(payment.id)


def test_interactions_function_tools_are_allow_listed() -> None:
    declarations = GeminiProvider._declarations(["get_payment", "execute_refund"])
    assert [item["name"] for item in declarations] == ["get_payment"]
    assert declarations[0]["parameters"]["type"] == "object"


def test_hallucinated_evidence_id_is_rejected(db_session: Session) -> None:
    incident, _ = records(db_session)
    client = FakeGeminiClient([final_response(output(incident.id, str(uuid4())))])
    with pytest.raises(InvalidInvestigationOutputError, match="unsupported evidence"):
        InvestigationService(GeminiProvider("test-key", "gemini-3.6-flash", client)).investigate(db_session, incident.id)


def test_malformed_output_becomes_structured_incomplete_result(db_session: Session) -> None:
    incident, _ = records(db_session)
    client = FakeGeminiClient([final_response("not-json")])
    result = InvestigationService(GeminiProvider("test-key", "gemini-3.6-flash", client)).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert any("malformed" in item for item in result.uncertainties)


@pytest.mark.parametrize("error", [TimeoutError("timed out"), RuntimeError("service unavailable"), RuntimeError("429 rate limit"), RuntimeError("503 unavailable")])
def test_provider_failures_become_structured_uncertainty(db_session: Session, error: Exception) -> None:
    incident, _ = records(db_session)
    client = FakeGeminiClient([])

    def fail(**kwargs: object) -> object:
        raise error

    client.interactions.create = fail
    result = InvestigationService(GeminiProvider("test-key", "gemini-3.6-flash", client)).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert result.confidence == 0
    assert result.uncertainties


def test_no_api_key_keeps_no_provider_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    assert isinstance(configured_provider(), NoProvider)


def test_tool_call_limit_and_financial_state_are_safe(db_session: Session) -> None:
    incident, payment = records(db_session)
    before = payment.status
    call = SimpleNamespace(type="function_call", id="call-limit", name="get_payment", arguments={"payment_id": str(payment.id)})
    client = FakeGeminiClient([interaction(call)])
    result = InvestigationService(GeminiProvider("test-key", "gemini-3.6-flash", client), max_tool_calls=0).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert any("tool-call limit" in item for item in result.uncertainties)
    db_session.expire_all()
    assert db_session.get(Payment, payment.id).status is before
