"""Explicit read-only tools exposed to the investigator."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Incident
from app.repositories.investigation import InvestigationRepository


class ToolError(ValueError):
    """Raised for invalid tool names or arguments."""


def calculate_financial_difference(expected_amount: int, observed_amount: int) -> int:
    """Return the signed difference using integer minor units only."""
    if not isinstance(expected_amount, int) or isinstance(expected_amount, bool):
        raise ToolError("expected_amount must be an integer.")
    if not isinstance(observed_amount, int) or isinstance(observed_amount, bool):
        raise ToolError("observed_amount must be an integer.")
    return expected_amount - observed_amount


class InvestigationToolLayer:
    """Allow-listed, read-only database tools for one investigation."""

    TOOL_NAMES = frozenset({
        "get_incident", "get_incident_evidence", "get_financial_event", "get_payment",
        "get_payment_history", "get_refund", "get_settlement", "get_bank_transaction",
        "find_related_transactions", "compare_financial_records", "calculate_financial_difference",
    })

    def __init__(self, db: Session, repository: InvestigationRepository | None = None) -> None:
        self._db = db
        self._repository = repository or InvestigationRepository()

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.TOOL_NAMES:
            raise ToolError(f"Unknown investigation tool: {name}")
        handler = getattr(self, name)
        result = handler(**arguments)
        return {"tool": name, "result": _serialize(result)}

    def get_incident(self, incident_id: UUID) -> dict[str, Any]:
        incident = self._repository.get_incident(self._db, incident_id)
        if incident is None:
            raise ToolError(f"Incident {incident_id} was not found.")
        return _serialize(incident)

    def get_incident_evidence(self, incident_id: UUID) -> list[dict[str, Any]]:
        return _serialize(self._repository.get_incident_evidence(self._db, incident_id))

    def get_financial_event(self, event_id: UUID) -> dict[str, Any]:
        event = self._repository.get_financial_event(self._db, event_id)
        if event is None:
            raise ToolError(f"Financial event {event_id} was not found.")
        return _serialize(event)

    def get_payment(self, payment_id: UUID | None = None, provider_payment_id: str | None = None) -> dict[str, Any]:
        payment = self._repository.get_payment(self._db, payment_id, provider_payment_id)
        if payment is None:
            raise ToolError("Payment was not found.")
        return _serialize(payment)

    def get_payment_history(self, payment_id: UUID) -> list[dict[str, Any]]:
        return _serialize(self._repository.get_payment_history(self._db, payment_id))

    def get_refund(self, refund_id: UUID | None = None, provider_refund_id: str | None = None) -> dict[str, Any]:
        refund = self._repository.get_refund(self._db, refund_id, provider_refund_id)
        if refund is None:
            raise ToolError("Refund was not found.")
        return _serialize(refund)

    def get_settlement(self, settlement_id: UUID | None = None, provider_settlement_id: str | None = None) -> dict[str, Any]:
        settlement = self._repository.get_settlement(self._db, settlement_id, provider_settlement_id)
        if settlement is None:
            raise ToolError("Settlement was not found.")
        return _serialize(settlement)

    def get_bank_transaction(self, transaction_id: UUID | None = None, external_transaction_id: str | None = None) -> dict[str, Any]:
        transaction = self._repository.get_bank_transaction(self._db, transaction_id, external_transaction_id)
        if transaction is None:
            raise ToolError("Bank transaction was not found.")
        return _serialize(transaction)

    def find_related_transactions(self, amount: int, currency: str, utr: str | None = None) -> list[dict[str, Any]]:
        return _serialize(self._repository.find_related_transactions(self._db, amount=amount, currency=currency, utr=utr))

    def compare_financial_records(self, expected_amount: int, observed_amount: int, currency: str) -> dict[str, Any]:
        difference = calculate_financial_difference(expected_amount, observed_amount)
        return {
            "expected_amount": expected_amount,
            "observed_amount": observed_amount,
            "difference": difference,
            "absolute_difference": abs(difference),
            "currency": currency,
        }

    def calculate_financial_difference(self, expected_amount: int, observed_amount: int) -> dict[str, int]:
        return {"difference": calculate_financial_difference(expected_amount, observed_amount)}


def _serialize(value: Any) -> Any:
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items() if key not in {"metadata_"}}
    if isinstance(value, (UUID, datetime, date)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__table__"):
        return {
            column.name: _serialize(getattr(value, column.name))
            for column in value.__table__.columns
            if column.name not in {"metadata", "metadata_"}
        }
    return value