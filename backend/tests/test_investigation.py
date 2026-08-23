from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.provider import ProviderResponse, ToolCall
from app.core.enums import IncidentSeverity, IncidentStatus, IncidentType, PaymentStatus, RefundStatus, SettlementStatus
from app.database.database import engine
from app.models import BankTransaction, FinancialEvent, Incident, IncidentEvidence, Payment, PaymentStateTransition, Refund, Settlement
from app.schemas.investigation import InvestigationOutput
from app.services.investigation_tools import InvestigationToolLayer, ToolError, calculate_financial_difference
from app.services.investigator import InvalidInvestigationOutputError, InvestigationService


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


def timestamp() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def records(db: Session) -> tuple[Incident, Payment, FinancialEvent, PaymentStateTransition, Refund, Settlement, BankTransaction]:
    payment = Payment(provider_payment_id=f"pay_investigation_{uuid4()}", amount=1000, currency="INR", status=PaymentStatus.CAPTURED)
    event = FinancialEvent(
        source="RAZORPAY", external_event_id=f"evt_investigation_{uuid4()}", event_type="payment.captured",
        occurred_at=timestamp(), received_at=timestamp(), signature_valid=True, raw_payload={"safe": True},
    )
    db.add_all((payment, event))
    db.flush()
    transition = PaymentStateTransition(
        payment_id=payment.id, financial_event_id=event.id, from_state=PaymentStatus.AUTHORIZED,
        to_state=PaymentStatus.CAPTURED, occurred_at=timestamp(),
    )
    refund = Refund(
        provider_refund_id=f"rfnd_investigation_{uuid4()}", payment_id=payment.id, amount=200,
        currency="INR", status=RefundStatus.PROCESSED,
    )
    settlement = Settlement(
        provider_settlement_id=f"setl_investigation_{uuid4()}", amount=800, currency="INR",
        fees=0, tax=0, status=SettlementStatus.PROCESSED,
    )
    bank_transaction = BankTransaction(
        external_transaction_id=f"bank_investigation_{uuid4()}", transaction_type="CREDIT", amount=800,
        currency="INR", transaction_at=timestamp(), source="synthetic",
    )
    incident = Incident(
        incident_code=f"SETTLEMENT_DISCREPANCY:{uuid4()}", incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.MEDIUM, status=IncidentStatus.OPEN, title="Settlement mismatch",
        description="Observed settlement differs from expected amount.", financial_exposure=200,
        currency="INR", detected_at=timestamp(),
    )
    db.add_all((transition, refund, settlement, bank_transaction, incident))
    db.flush()
    evidence = IncidentEvidence(
        incident_id=incident.id, evidence_type="financial", entity_type="Payment",
        entity_id=str(payment.id), relationship="supports",
    )
    db.add(evidence)
    db.flush()
    return incident, payment, event, transition, refund, settlement, bank_transaction


def test_tools_retrieve_incident_and_evidence(db_session: Session) -> None:
    incident, payment, *_ = records(db_session)
    tools = InvestigationToolLayer(db_session)

    incident_result = tools.execute("get_incident", {"incident_id": incident.id})
    evidence_result = tools.execute("get_incident_evidence", {"incident_id": incident.id})

    assert incident_result["result"]["id"] == str(incident.id)
    assert evidence_result["result"][0]["entity_id"] == str(payment.id)


def test_tools_retrieve_financial_records_and_history(db_session: Session) -> None:
    incident, payment, event, transition, refund, settlement, bank_transaction = records(db_session)
    tools = InvestigationToolLayer(db_session)

    assert tools.execute("get_financial_event", {"event_id": event.id})["result"]["id"] == str(event.id)
    assert tools.execute("get_payment", {"payment_id": payment.id})["result"]["id"] == str(payment.id)
    assert tools.execute("get_payment_history", {"payment_id": payment.id})["result"][0]["id"] == str(transition.id)
    assert tools.execute("get_refund", {"refund_id": refund.id})["result"]["id"] == str(refund.id)
    assert tools.execute("get_settlement", {"settlement_id": settlement.id})["result"]["id"] == str(settlement.id)
    assert tools.execute("get_bank_transaction", {"transaction_id": bank_transaction.id})["result"]["id"] == str(bank_transaction.id)
    assert tools.execute("find_related_transactions", {"amount": 800, "currency": "INR"})["result"][0]["id"] == str(bank_transaction.id)


def test_deterministic_financial_calculation_is_integer_only() -> None:
    assert calculate_financial_difference(500000, 472000) == 28000
    with pytest.raises(ToolError):
        calculate_financial_difference(1.5, 1)  # type: ignore[arg-type]


def test_compare_records_returns_deterministic_difference(db_session: Session) -> None:
    result = InvestigationToolLayer(db_session).execute(
        "compare_financial_records", {"expected_amount": 1000, "observed_amount": 900, "currency": "INR"}
    )
    assert result["result"] == {
        "expected_amount": 1000, "observed_amount": 900, "difference": 100,
        "absolute_difference": 100, "currency": "INR",
    }


def test_tools_are_read_only(db_session: Session) -> None:
    incident, payment, *_ = records(db_session)
    before = (incident.title, incident.financial_exposure, payment.status, payment.amount)
    InvestigationToolLayer(db_session).execute("get_incident", {"incident_id": incident.id})
    InvestigationToolLayer(db_session).execute("get_payment", {"payment_id": payment.id})
    db_session.expire_all()
    assert (incident.title, incident.financial_exposure, payment.status, payment.amount) == before


def valid_output(incident_id, evidence_id: str) -> dict[str, object]:
    return {
        "incident_id": str(incident_id), "root_cause": "Settlement amount differs from expected records.",
        "summary": "The available records show a settlement discrepancy.",
        "observed_facts": ["The incident and payment were retrieved."],
        "derived_findings": ["The deterministic difference is 100 minor units."],
        "evidence": [{"entity_type": "Payment", "entity_id": evidence_id, "relationship": "supports"}],
        "financial_impact_minor": 100, "unresolved_amount_minor": 100,
        "recommended_action": "Review the settlement reconciliation.", "confidence": 0.75,
        "uncertainties": [],
    }


class FinalProvider:
    def __init__(self, content: object) -> None:
        self.content = content
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs) -> ProviderResponse:
        self.calls.append(kwargs)
        return ProviderResponse(content=self.content)


class ToolThenFinalProvider:
    def __init__(self, incident_id, payment_id: str) -> None:
        self.incident_id = incident_id
        self.payment_id = payment_id
        self.calls = 0

    def complete(self, **kwargs) -> ProviderResponse:
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(tool_calls=(ToolCall("get_payment", {"payment_id": self.payment_id}),))
        return ProviderResponse(content=valid_output(self.incident_id, self.payment_id))


def test_investigation_validates_mocked_structured_response(db_session: Session) -> None:
    incident, payment, *_ = records(db_session)
    provider = FinalProvider(valid_output(incident.id, str(payment.id)))

    result = InvestigationService(provider).investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    assert result.incident_id == incident.id
    assert result.evidence[0].entity_id == str(payment.id)
    assert "read-only" in provider.calls[0]["system_prompt"]


def test_investigation_orchestrates_bounded_tool_calls(db_session: Session) -> None:
    incident, payment, *_ = records(db_session)
    provider = ToolThenFinalProvider(incident.id, str(payment.id))

    result = InvestigationService(provider, max_tool_calls=1).investigate(db_session, incident.id)

    assert result.root_cause.startswith("Settlement")
    assert provider.calls == 2


def test_missing_incident_returns_structured_incomplete_investigation(db_session: Session) -> None:
    result = InvestigationService().investigate(db_session, uuid4())

    assert result.root_cause == "Undetermined"
    assert result.confidence == 0
    assert result.uncertainties


def test_no_provider_mode_does_not_break_deterministic_investigation(db_session: Session) -> None:
    incident, *_ = records(db_session)

    result = InvestigationService().investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    assert result.confidence == 0
    assert result.evidence


def test_invalid_ai_output_is_rejected(db_session: Session) -> None:
    incident, payment, *_ = records(db_session)
    provider = FinalProvider({"incident_id": str(incident.id)})

    with pytest.raises(InvalidInvestigationOutputError):
        InvestigationService(provider).investigate(db_session, incident.id)


def test_untrusted_evidence_id_is_rejected(db_session: Session) -> None:
    incident, *_ = records(db_session)
    provider = FinalProvider(valid_output(incident.id, str(uuid4())))

    with pytest.raises(InvalidInvestigationOutputError, match="unsupported evidence"):
        InvestigationService(provider).investigate(db_session, incident.id)


def test_tool_call_limit_returns_structured_uncertainty(db_session: Session) -> None:
    incident, payment, *_ = records(db_session)
    provider = ToolThenFinalProvider(incident.id, str(payment.id))

    result = InvestigationService(provider, max_tool_calls=0).investigate(db_session, incident.id)

    assert result.root_cause == "Undetermined"
    assert any("tool-call limit" in item for item in result.uncertainties)


def test_tool_failure_returns_structured_uncertainty(db_session: Session) -> None:
    incident, *_ = records(db_session)
    class FailingToolProvider:
        def complete(self, **kwargs) -> ProviderResponse:
            return ProviderResponse(tool_calls=(ToolCall("get_payment", {"payment_id": uuid4()}),))

    provider = FailingToolProvider()
    result = InvestigationService(provider).investigate(db_session, incident.id)
    assert result.root_cause == "Undetermined"
    assert any("tool failed" in item for item in result.uncertainties)


def test_investigation_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        InvestigationOutput.model_validate({"unexpected": True})


def test_ai_surface_has_no_mutation_or_execution_tools() -> None:
    names = InvestigationToolLayer.TOOL_NAMES
    assert not any("update" in name or "delete" in name or "execute" in name or "refund" == name for name in names)
