"""Tests for the evidence-backed deterministic investigation fallback.

Validates that when the real provider fails or returns malformed output,
the fallback constructs a valid InvestigationOutput from tool-retrieved
evidence without inventing IDs, amounts, or root causes.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.agents.fallback import EvidenceBackedFallbackProvider
from app.agents.provider import ProviderResponse
from app.core.enums import (
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
)
from app.database.database import engine
from app.models import (
    BankTransaction,
    Incident,
    IncidentEvidence,
    Settlement,
)
from app.schemas.investigation import InvestigationOutput
from app.services.investigator import InvestigationService


# ── Fixtures ──────────────────────────────────────────────────────────────


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


def _create_settlement_incident(
    db: Session,
    *,
    expected_amount: int = 50_000_000,
    observed_amount: int = 47_200_000,
) -> tuple[Incident, IncidentEvidence, Settlement, BankTransaction]:
    # Production semantics: an incident's financial_exposure is the recorded
    # ABSOLUTE discrepancy (expected − observed), not the expected amount.
    financial_exposure = abs(expected_amount - observed_amount)
    incident = Incident(
        incident_code=f"FALLBACK_TEST_{uuid4()}",
        incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        title="Settlement discrepancy",
        description="Expected differs from observed",
        financial_exposure=financial_exposure,
        currency="INR",
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()

    settlement = Settlement(
        provider_settlement_id=f"SETTLE_{uuid4()}",
        amount=observed_amount,
        currency="INR",
        fees=0,
        tax=0,
        status="PROCESSED",
    )
    db.add(settlement)
    db.flush()

    evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="Settlement",
        entity_id=str(settlement.id),
        relationship="supports",
    )
    db.add(evidence)
    db.flush()

    bank = BankTransaction(
        external_transaction_id=f"BANK_{uuid4()}",
        transaction_type="CREDIT",
        amount=observed_amount,
        currency="INR",
        transaction_at="2026-01-01T00:00:00Z",
        source="DEMO",
    )
    db.add(bank)
    db.flush()

    bank_evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="BankTransaction",
        entity_id=str(bank.id),
        relationship="supports",
    )
    db.add(bank_evidence)
    db.flush()

    return incident, evidence, settlement, bank


def _create_unsupported_incident(db: Session) -> tuple[Incident, IncidentEvidence]:
    incident = Incident(
        incident_code=f"FALLBACK_UNSUPPORTED_{uuid4()}",
        incident_type=IncidentType.PAYMENT_STATE_CONFLICT,
        severity=IncidentSeverity.LOW,
        status=IncidentStatus.OPEN,
        title="Payment state conflict",
        description="Test unsupported type",
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()

    evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="state",
        entity_type="Payment",
        entity_id=str(uuid4()),
        relationship="conflicts",
    )
    db.add(evidence)
    db.flush()

    return incident, evidence


def _incident_result(incident: Incident) -> dict[str, Any]:
    return {
        "id": str(incident.id),
        "incident_code": incident.incident_code,
        "incident_type": incident.incident_type.value,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "title": incident.title,
        "financial_exposure": incident.financial_exposure,
        "currency": incident.currency,
    }


def _evidence_result(evidence: IncidentEvidence, extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    items = [{"entity_type": evidence.entity_type, "entity_id": evidence.entity_id, "relationship": evidence.relationship}]
    if extra:
        items.extend(extra)
    return items


class _MalformedProvider:
    """Provider that always returns malformed (non-dict) content."""

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        return ProviderResponse(content="this is not valid JSON output")


class _HTTPErrorProvider:
    """Provider that simulates a 503 error."""

    def __init__(self, status_code: int = 503) -> None:
        self._status_code = status_code

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        import requests

        response = MagicMock()
        response.status_code = self._status_code
        error = requests.HTTPError(response=response)
        error.response = response
        raise error


class _TimeoutProvider:
    """Provider that simulates a timeout."""

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        import requests

        raise requests.Timeout("Connection timed out")


class _ValidProvider:
    """Provider that returns valid InvestigationOutput content."""

    def __init__(self, content: dict[str, Any]) -> None:
        self._content = content
        self.called = False

    def complete(self, *, system_prompt: str, messages: list[dict[str, Any]], tools: list[str]) -> ProviderResponse:
        self.called = True
        return ProviderResponse(content=self._content)


# ── Tests ────────────────────────────────────────────────────────────────


def test_malformed_output_triggers_fallback(db_session: Session) -> None:
    """1. Malformed (non-dict) provider output triggers the fallback."""
    incident, evidence, _, _ = _create_settlement_incident(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    assert result.incident_id == incident.id
    assert any("[fallback]" in u for u in result.uncertainties)


def test_503_error_triggers_fallback(db_session: Session) -> None:
    """2. HTTP 503 from provider triggers the fallback."""
    incident, evidence, _, _ = _create_settlement_incident(db_session)

    service = InvestigationService(provider=_HTTPErrorProvider(503), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    assert result.incident_id == incident.id
    assert any("[fallback]" in u for u in result.uncertainties)


def test_timeout_triggers_fallback(db_session: Session) -> None:
    """3. Provider timeout triggers the fallback."""
    incident, evidence, _, _ = _create_settlement_incident(db_session)

    service = InvestigationService(provider=_TimeoutProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    assert result.incident_id == incident.id
    assert any("[fallback]" in u for u in result.uncertainties)


def test_valid_provider_result_preserved(db_session: Session) -> None:
    """4. Valid provider output is NOT replaced by fallback."""
    incident, evidence, _, _ = _create_settlement_incident(db_session)

    valid_content = {
        "incident_id": str(incident.id),
        "root_cause": "Provider found the root cause.",
        "summary": "Provider summary.",
        "observed_facts": ["Provider observed fact."],
        "derived_findings": ["Provider finding."],
        "evidence": [{"entity_type": "Settlement", "entity_id": evidence.entity_id, "relationship": "supports"}],
        "financial_impact_minor": 2_800_000,
        "unresolved_amount_minor": 2_800_000,
        "recommended_action": "Provider recommendation.",
        "confidence": 0.92,
        "uncertainties": [],
    }
    provider = _ValidProvider(valid_content)

    service = InvestigationService(provider=provider, use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert provider.called is True
    assert result.root_cause == "Provider found the root cause."
    assert result.confidence == 0.92
    assert not any("[fallback]" in u for u in result.uncertainties)


def test_settlement_discrepancy_fallback(db_session: Session) -> None:
    """5. Settlement discrepancy fallback constructs correct output."""
    incident, evidence, settlement, bank = _create_settlement_incident(db_session)

    provider = _MalformedProvider()
    service = InvestigationService(provider=provider, use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    assert "discrepancy" in result.root_cause.lower()
    assert len(result.observed_facts) > 0
    assert len(result.evidence) > 0
    assert result.recommended_action == "Review and reconcile the settlement discrepancy."


def test_fallback_financial_impact_is_2800000(db_session: Session) -> None:
    """6. Fallback financial_impact_minor is the deterministic discrepancy."""
    incident, evidence, _, _ = _create_settlement_incident(
        db_session, expected_amount=50_000_000, observed_amount=47_200_000
    )

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert result.financial_impact_minor == 2_800_000


def test_fallback_unresolved_amount_is_2800000(db_session: Session) -> None:
    """7. Fallback unresolved_amount_minor matches financial impact."""
    incident, evidence, _, _ = _create_settlement_incident(
        db_session, expected_amount=50_000_000, observed_amount=47_200_000
    )

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert result.unresolved_amount_minor == 2_800_000
    assert result.unresolved_amount_minor == result.financial_impact_minor


def test_fallback_evidence_ids_are_real(db_session: Session) -> None:
    """8. Fallback evidence contains only real database entity IDs."""
    incident, evidence, settlement, bank = _create_settlement_incident(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    real_ids = {str(settlement.id), str(bank.id)}
    fallback_ids = {item.entity_id for item in result.evidence}
    assert fallback_ids.issubset(real_ids)


def test_fallback_does_not_invent_identifiers(db_session: Session) -> None:
    """9. Fallback does not invent any IDs, amounts, currencies, or root causes."""
    incident, evidence, _, _ = _create_settlement_incident(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    assert all(item.entity_id for item in result.evidence)
    assert result.financial_impact_minor >= 0
    assert result.unresolved_amount_minor >= 0
    assert result.confidence >= 0


def test_unsupported_incident_type_conservative(db_session: Session) -> None:
    """10. Unsupported incident type returns conservative incomplete result."""
    incident, evidence = _create_unsupported_incident(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    assert result.root_cause == "Undetermined"
    assert result.financial_impact_minor == 0
    assert result.unresolved_amount_minor == 0
    assert result.confidence == 0.0
    assert len(result.evidence) > 0
    assert any("no deterministic analysis" in u.lower() for u in result.uncertainties)


def test_no_financial_state_mutation(db_session: Session) -> None:
    """11. Fallback does not mutate any financial state."""
    from app.models import (
        FinancialEvent,
        Payment,
        Refund,
    )

    incident, evidence, _, _ = _create_settlement_incident(db_session)
    payment_count = db_session.scalar(
        __import__("sqlalchemy", fromlist=["func"]).func.count()
    ) or 0

    # Count before
    from sqlalchemy import func, select

    counts_before = {
        "payments": db_session.scalar(select(func.count()).select_from(Payment)),
        "refunds": db_session.scalar(select(func.count()).select_from(Refund)),
        "settlements": db_session.scalar(select(func.count()).select_from(Settlement)),
        "events": db_session.scalar(select(func.count()).select_from(FinancialEvent)),
        "bank": db_session.scalar(select(func.count()).select_from(BankTransaction)),
    }

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    service.investigate(db_session, incident.id)
    db_session.expire_all()

    counts_after = {
        "payments": db_session.scalar(select(func.count()).select_from(Payment)),
        "refunds": db_session.scalar(select(func.count()).select_from(Refund)),
        "settlements": db_session.scalar(select(func.count()).select_from(Settlement)),
        "events": db_session.scalar(select(func.count()).select_from(FinancialEvent)),
        "bank": db_session.scalar(select(func.count()).select_from(BankTransaction)),
    }

    assert counts_before == counts_after


def test_fallback_evidence_tuples_match_incident_evidence(db_session: Session) -> None:
    """Fallback evidence tuples exactly match the incident's IncidentEvidence rows."""
    from sqlalchemy import select

    incident, evidence, settlement, bank = _create_settlement_incident(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    rows = {
        (item.entity_type, item.entity_id, item.relationship)
        for item in db_session.scalars(select(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id))
    }
    assert rows
    requested = {(item.entity_type, item.entity_id, item.relationship) for item in result.evidence}
    assert requested
    assert requested.issubset(rows)


def test_action_proposal_created_from_fallback_investigation(db_session: Session) -> None:
    """A proposal can be created from a fallback investigation and requires approval."""
    from app.core.enums import ActionProposalStatus, ApprovalStatus
    from app.models import Approval, Policy
    from app.policies.engine import PolicyOutcome
    from app.services.investigation_action_planning import InvestigationActionService

    incident, evidence, settlement, bank = _create_settlement_incident(db_session)
    db_session.add(Policy(
        name=f"DEMO_RECONCILE_{uuid4()}",
        description="Deterministic demo policy for settlement reconciliation proposals.",
        action_type="RECONCILE_ADJUSTMENT",
        max_amount=10_000,
        min_confidence=Decimal("0.90"),
        requires_approval=True,
        is_active=True,
    ))
    db_session.flush()

    investigation = InvestigationService(provider=_MalformedProvider(), use_fallback=True).investigate(
        db_session, incident.id
    )
    assert investigation.financial_impact_minor == 2_800_000

    result = InvestigationActionService().create_proposal_from_investigation(db_session, investigation)

    assert result.proposal.action_type == "RECONCILE_ADJUSTMENT"
    assert result.proposal.amount == 2_800_000
    assert result.proposal.status == ActionProposalStatus.PROPOSED
    assert result.policy_decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert result.proposal.requires_approval is True
    assert result.approval_id is not None
    approval = db_session.get(Approval, result.approval_id)
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING


def test_non_incident_evidence_db_id_is_rejected(db_session: Session) -> None:
    """A real entity ID that is NOT an IncidentEvidence row is still rejected."""
    from app.core.enums import PaymentStatus
    from app.models import Payment
    from app.services.investigator import InvalidInvestigationOutputError

    incident, evidence, settlement, bank = _create_settlement_incident(db_session)
    # A real payment exists in the database but is NOT referenced by any
    # IncidentEvidence row for this incident.
    payment = Payment(
        provider_payment_id=f"PAY_{uuid4()}",
        amount=50_000_000,
        currency="INR",
        status=PaymentStatus.CAPTURED,
    )
    db_session.add(payment)
    db_session.flush()

    provider = _ValidProvider({
        "incident_id": str(incident.id),
        "root_cause": "Root cause.",
        "summary": "Summary.",
        "observed_facts": ["Fact."],
        "derived_findings": ["Finding."],
        "evidence": [{"entity_type": "Payment", "entity_id": str(payment.id), "relationship": "supports"}],
        "financial_impact_minor": 2_800_000,
        "unresolved_amount_minor": 2_800_000,
        "recommended_action": "Reconcile the settlement adjustment.",
        "confidence": 0.9,
        "uncertainties": [],
    })
    service = InvestigationService(provider=provider, use_fallback=False)
    with pytest.raises(InvalidInvestigationOutputError, match="unsupported evidence"):
        service.investigate(db_session, incident.id)


def _create_settlement_incident_missing_bank(
    db: Session,
    *,
    expected_amount: int = 50_000_000,
    observed_amount: int = 47_200_000,
) -> tuple[Incident, IncidentEvidence, Settlement]:
    """A settlement-discrepancy incident whose Settlement exists but whose
    referenced BankTransaction record does NOT exist.

    This mirrors the live failure: the IncidentEvidence row references a
    BankTransaction UUID with no corresponding row, so retrieval raises
    ToolError.  The investigation must NOT fail; it must record an
    uncertainty and derive financials from the authoritative Settlement.
    """
    financial_exposure = abs(expected_amount - observed_amount)
    incident = Incident(
        incident_code=f"FALLBACK_MISSING_BANK_{uuid4()}",
        incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        title="Settlement discrepancy",
        description="Expected differs from observed",
        financial_exposure=financial_exposure,
        currency="INR",
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()

    settlement = Settlement(
        provider_settlement_id=f"SETTLE_{uuid4()}",
        amount=observed_amount,
        currency="INR",
        fees=0,
        tax=0,
        status="PROCESSED",
    )
    db.add(settlement)
    db.flush()

    evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="Settlement",
        entity_id=str(settlement.id),
        relationship="supports",
    )
    db.add(evidence)
    db.flush()

    # BankTransaction evidence references a UUID with no corresponding row.
    bank_evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="BankTransaction",
        entity_id=str(uuid4()),
        relationship="supports",
    )
    db.add(bank_evidence)
    db.flush()

    return incident, evidence, settlement


def _create_settlement_incident_missing_settlement(
    db: Session,
    *,
    expected_amount: int = 50_000_000,
    observed_amount: int = 47_200_000,
) -> tuple[Incident, IncidentEvidence, BankTransaction]:
    """A settlement-discrepancy incident whose referenced Settlement record
    does NOT exist (a BankTransaction exists and is referenced).

    Without the authoritative Settlement the fallback must NOT invent an
    amount; it returns a conservative incomplete investigation.
    """
    financial_exposure = abs(expected_amount - observed_amount)
    incident = Incident(
        incident_code=f"FALLBACK_MISSING_SETTLE_{uuid4()}",
        incident_type=IncidentType.SETTLEMENT_DISCREPANCY,
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        title="Settlement discrepancy",
        description="Expected differs from observed",
        financial_exposure=financial_exposure,
        currency="INR",
        detected_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(incident)
    db.flush()

    # Settlement evidence references a UUID with no corresponding row.
    evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="Settlement",
        entity_id=str(uuid4()),
        relationship="supports",
    )
    db.add(evidence)
    db.flush()

    bank = BankTransaction(
        external_transaction_id=f"BANK_{uuid4()}",
        transaction_type="CREDIT",
        amount=observed_amount,
        currency="INR",
        transaction_at="2026-01-01T00:00:00Z",
        source="DEMO",
    )
    db.add(bank)
    db.flush()

    bank_evidence = IncidentEvidence(
        incident_id=incident.id,
        evidence_type="financial",
        entity_type="BankTransaction",
        entity_id=str(bank.id),
        relationship="supports",
    )
    db.add(bank_evidence)
    db.flush()

    return incident, evidence, bank


def test_missing_bank_transaction_yields_valid_fallback(db_session: Session) -> None:
    """12. A missing optional BankTransaction does NOT fail the investigation."""
    incident, _, _ = _create_settlement_incident_missing_bank(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert isinstance(result, InvestigationOutput)
    assert result.incident_id == incident.id
    assert result.root_cause != "Undetermined"
    assert result.confidence > 0
    assert len(result.evidence) > 0
    assert result.financial_impact_minor == 2_800_000
    assert result.unresolved_amount_minor == 2_800_000


def test_missing_secondary_evidence_recorded_as_uncertainty(db_session: Session) -> None:
    """13. A missing secondary BankTransaction appears as an uncertainty."""
    incident, _, _ = _create_settlement_incident_missing_bank(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert any(
        "bank transaction evidence could not be retrieved" in item.lower()
        for item in result.uncertainties
    )
    # The settlement-derived discrepancy is still reported.
    assert result.financial_impact_minor == 2_800_000


def test_missing_settlement_yields_conservative_incomplete(db_session: Session) -> None:
    """14. A missing authoritative Settlement yields a conservative result."""
    from sqlalchemy import select

    incident, evidence, _ = _create_settlement_incident_missing_settlement(db_session)

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)

    assert result.incident_id == incident.id
    assert result.root_cause == "Undetermined"
    assert result.financial_impact_minor == 0
    assert result.unresolved_amount_minor == 0
    assert result.confidence == 0.0
    # No invented evidence: all fallback evidence are real IncidentEvidence rows.
    from sqlalchemy import select as _select
    rows = {
        (item.entity_type, item.entity_id, item.relationship)
        for item in db_session.scalars(_select(IncidentEvidence).where(IncidentEvidence.incident_id == incident.id))
    }
    assert rows
    requested = {(item.entity_type, item.entity_id, item.relationship) for item in result.evidence}
    assert requested
    assert requested.issubset(rows)


def test_existing_action_proposal_remains_untouched(db_session: Session) -> None:
    """15. An existing persisted ActionProposal is untouched by investigation."""
    from app.core.enums import ActionProposalStatus
    from app.models import ActionProposal

    incident, _, _ = _create_settlement_incident_missing_bank(db_session)
    proposal = ActionProposal(
        incident_id=incident.id,
        action_type="RECONCILE_ADJUSTMENT",
        description="Existing persisted proposal, untouched by investigation.",
        amount=2_800_000,
        currency="INR",
        confidence=Decimal("0.95"),
        requires_approval=True,
        status=ActionProposalStatus.PROPOSED,
        created_by="demo",
    )
    db_session.add(proposal)
    db_session.flush()

    service = InvestigationService(provider=_MalformedProvider(), use_fallback=True)
    result = service.investigate(db_session, incident.id)
    db_session.expire_all()

    reloaded = db_session.get(ActionProposal, proposal.id)
    assert reloaded is not None
    assert reloaded.action_type == "RECONCILE_ADJUSTMENT"
    assert reloaded.amount == 2_800_000
    assert reloaded.status == ActionProposalStatus.PROPOSED
    assert reloaded.requires_approval is True
    assert result.incident_id == incident.id
