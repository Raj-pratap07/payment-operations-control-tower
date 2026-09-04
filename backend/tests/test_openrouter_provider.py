import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.agents.provider import NoProvider, OpenRouterProvider, configured_provider
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
    payment = Payment(provider_payment_id=f"openrouter-pay-{uuid4()}", amount=1000, currency="INR", status=PaymentStatus.CAPTURED)
    incident = Incident(
        incident_code=f"OPENROUTER:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM, status=IncidentStatus.OPEN, title="OpenRouter test incident",
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


class FakeResponse:
    def __init__(self, body: dict[str, object], status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = __import__("requests").HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self) -> dict[str, object]:
        return self.body


class FakeHTTP:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        kwargs["url"] = url
        self.calls.append(kwargs)
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def final_response(value: object) -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"role": "assistant", "content": json.dumps(value)}}]})


def test_successful_openrouter_response_and_structured_output(db_session: Session) -> None:
    incident, payment = records(db_session)
    http = FakeHTTP([final_response(output(incident.id, str(payment.id)))])
    provider = OpenRouterProvider("secret-key", "openai/gpt-oss-120b:free", http)

    result = InvestigationService(provider).investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    request = http.calls[0]
    assert request["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert request["json"]["model"] == "openai/gpt-oss-120b:free"
    assert request["json"]["response_format"]["type"] == "json_schema"
    assert request["headers"]["Authorization"] == "Bearer secret-key"


def test_openrouter_tool_call_executes_locally_and_returns_result(db_session: Session) -> None:
    incident, payment = records(db_session)
    tool_call = {"id": "call-123", "type": "function", "function": {"name": "get_payment", "arguments": json.dumps({"payment_id": str(payment.id)})}}
    first = FakeResponse({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tool_call]}}]})
    http = FakeHTTP([first, final_response(output(incident.id, str(payment.id)))])

    result = InvestigationService(OpenRouterProvider("secret-key", "openai/gpt-oss-120b:free", http), max_tool_calls=1).investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    second_payload = http.calls[1]["json"]
    tool_message = second_payload["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-123"
    assert json.loads(tool_message["content"])["result"]["id"] == str(payment.id)


def test_openrouter_tools_are_allow_listed() -> None:
    declarations = OpenRouterProvider._declarations(["get_payment", "execute_refund"])
    assert [item["function"]["name"] for item in declarations] == ["get_payment"]
    assert declarations[0]["type"] == "function"


def test_hallucinated_evidence_id_is_rejected(db_session: Session) -> None:
    incident, _ = records(db_session)
    http = FakeHTTP([final_response(output(incident.id, str(uuid4())))])
    with pytest.raises(InvalidInvestigationOutputError, match="unsupported evidence"):
        InvestigationService(OpenRouterProvider("secret-key", "model", http)).investigate(db_session, incident.id)


def test_malformed_output_becomes_incomplete(db_session: Session) -> None:
    incident, _ = records(db_session)
    http = FakeHTTP([final_response("not an object")])
    result = InvestigationService(OpenRouterProvider("secret-key", "model", http)).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert any("malformed" in item for item in result.uncertainties)


@pytest.mark.parametrize("status_code", [401, 429, 503])
def test_http_provider_failures_become_incomplete(db_session: Session, status_code: int) -> None:
    incident, _ = records(db_session)
    http = FakeHTTP([FakeResponse({}, status_code)])
    result = InvestigationService(OpenRouterProvider("secret-key", "model", http)).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert result.confidence == 0
    assert result.uncertainties


def test_timeout_becomes_incomplete_and_key_is_not_exposed(db_session: Session) -> None:
    incident, _ = records(db_session)
    http = FakeHTTP([__import__("requests").Timeout("secret-key timeout")])
    result = InvestigationService(OpenRouterProvider("secret-key", "model", http)).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert all("secret-key" not in item for item in result.uncertainties)


def test_missing_selected_key_returns_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "openrouter")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    assert isinstance(configured_provider(), NoProvider)


def test_provider_selection_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "openrouter")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(settings, "OPENROUTER_MODEL", "openrouter/model")
    provider = configured_provider()
    assert isinstance(provider, OpenRouterProvider)
    assert provider._model == "openrouter/model"


def test_tool_limit_and_financial_state_are_safe(db_session: Session) -> None:
    incident, payment = records(db_session)
    before = payment.status
    call = {"id": "call-limit", "type": "function", "function": {"name": "get_payment", "arguments": json.dumps({"payment_id": str(payment.id)})}}
    http = FakeHTTP([FakeResponse({"choices": [{"message": {"tool_calls": [call], "content": None}}]})])
    result = InvestigationService(OpenRouterProvider("secret-key", "model", http), max_tool_calls=0).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert any("tool-call limit" in item for item in result.uncertainties)
    db_session.expire_all()
    assert db_session.get(Payment, payment.id).status is before
