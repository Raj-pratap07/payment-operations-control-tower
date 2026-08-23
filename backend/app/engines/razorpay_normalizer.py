"""Razorpay payload extraction into provider-neutral canonical events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import FinancialEvent
from app.schemas.canonical_events import (
    CanonicalAggregateType,
    CanonicalEvent,
    CanonicalEventType,
    NormalizationResult,
    PaymentEventData,
    RefundEventData,
    SettlementEventData,
    UnsupportedEvent,
)


class NormalizationError(ValueError):
    """Raised when a supported provider event lacks required canonical data."""


class RazorpayEventNormalizer:
    """Deterministically normalize only the V1-supported Razorpay event types."""

    _PAYMENT_TYPES = {
        "payment.authorized": CanonicalEventType.PAYMENT_AUTHORIZED,
        "payment.captured": CanonicalEventType.PAYMENT_CAPTURED,
        "payment.failed": CanonicalEventType.PAYMENT_FAILED,
    }
    _REFUND_TYPES = {
        "refund.created": CanonicalEventType.REFUND_CREATED,
        "refund.processed": CanonicalEventType.REFUND_PROCESSED,
        "refund.failed": CanonicalEventType.REFUND_FAILED,
    }
    _SETTLEMENT_TYPES = {"settlement.processed": CanonicalEventType.SETTLEMENT_PROCESSED}

    def normalize(self, financial_event: FinancialEvent) -> NormalizationResult:
        payload = financial_event.raw_payload
        if not isinstance(payload, dict):
            raise NormalizationError("Financial event payload must be a JSON object.")
        if financial_event.event_type in self._PAYMENT_TYPES:
            return self._payment_event(financial_event, payload)
        if financial_event.event_type in self._REFUND_TYPES:
            return self._refund_event(financial_event, payload)
        if financial_event.event_type in self._SETTLEMENT_TYPES:
            return self._settlement_event(financial_event, payload)
        return UnsupportedEvent(
            event_id=self._event_id(financial_event),
            source=financial_event.source,
            event_type=financial_event.event_type,
            occurred_at=financial_event.occurred_at,
            reason="Razorpay event type is not supported by V1 normalization.",
        )

    def _payment_event(self, event: FinancialEvent, payload: dict[str, Any]) -> CanonicalEvent:
        entity = self._entity(payload, "payment")
        payment_id = self._required_string(entity, "id", "payment ID")
        data = PaymentEventData(
            payment_id=payment_id,
            order_id=self._optional_string(entity, "order_id"),
            amount_minor=self._required_int(entity, "amount", "payment amount"),
            currency=self._required_string(entity, "currency", "payment currency"),
        )
        return self._canonical(event, self._PAYMENT_TYPES[event.event_type], CanonicalAggregateType.PAYMENT, payment_id, data, entity, payload)

    def _refund_event(self, event: FinancialEvent, payload: dict[str, Any]) -> CanonicalEvent:
        entity = self._entity(payload, "refund")
        refund_id = self._required_string(entity, "id", "refund ID")
        data = RefundEventData(
            refund_id=refund_id,
            payment_id=self._required_string(entity, "payment_id", "refund payment ID"),
            amount_minor=self._required_int(entity, "amount", "refund amount"),
            currency=self._required_string(entity, "currency", "refund currency"),
        )
        return self._canonical(event, self._REFUND_TYPES[event.event_type], CanonicalAggregateType.REFUND, refund_id, data, entity, payload)

    def _settlement_event(self, event: FinancialEvent, payload: dict[str, Any]) -> CanonicalEvent:
        entity = self._entity(payload, "settlement")
        settlement_id = self._required_string(entity, "id", "settlement ID")
        data = SettlementEventData(
            settlement_id=settlement_id,
            amount_minor=self._required_int(entity, "amount", "settlement amount"),
            currency=self._required_string(entity, "currency", "settlement currency"),
            fees_minor=self._required_int(entity, "fee", "settlement fee", alternate_key="fees"),
            tax_minor=self._required_int(entity, "tax", "settlement tax"),
            utr=self._optional_string(entity, "utr"),
        )
        return self._canonical(event, self._SETTLEMENT_TYPES[event.event_type], CanonicalAggregateType.SETTLEMENT, settlement_id, data, entity, payload)

    def _canonical(self, event: FinancialEvent, event_type: CanonicalEventType, aggregate_type: CanonicalAggregateType, aggregate_id: str, data: Any, entity: dict[str, Any], payload: dict[str, Any]) -> CanonicalEvent:
        return CanonicalEvent(
            event_id=self._event_id(event),
            source=event.source,
            event_type=event_type,
            occurred_at=self._event_time(entity, payload, fallback=event.occurred_at),
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            data=data,
        )

    @staticmethod
    def _event_id(event: FinancialEvent):
        if event.id is None:
            raise NormalizationError("Financial event must have an ID before normalization.")
        return event.id

    @staticmethod
    def _entity(payload: dict[str, Any], entity_name: str) -> dict[str, Any]:
        try:
            entity = payload["payload"][entity_name]["entity"]
        except (KeyError, TypeError) as error:
            raise NormalizationError(f"Malformed Razorpay payload: missing {entity_name} entity.") from error
        if not isinstance(entity, dict):
            raise NormalizationError(f"Malformed Razorpay payload: {entity_name} entity must be an object.")
        return entity

    @staticmethod
    def _required_string(entity: dict[str, Any], key: str, label: str) -> str:
        value = entity.get(key)
        if not isinstance(value, str) or not value:
            raise NormalizationError(f"Supported Razorpay event is missing {label}.")
        return value

    @staticmethod
    def _optional_string(entity: dict[str, Any], key: str) -> str | None:
        value = entity.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise NormalizationError(f"Razorpay {key} must be a string when supplied.")
        return value

    @staticmethod
    def _required_int(entity: dict[str, Any], key: str, label: str, alternate_key: str | None = None) -> int:
        value = entity.get(key, entity.get(alternate_key) if alternate_key else None)
        if not isinstance(value, int) or isinstance(value, bool):
            raise NormalizationError(f"Supported Razorpay event is missing integer {label}.")
        return value

    @classmethod
    def _event_time(cls, entity: dict[str, Any], payload: dict[str, Any], *, fallback: datetime) -> datetime:
        """Use entity/top-level provider timestamps; otherwise use FinancialEvent.occurred_at."""
        for candidate in (entity.get("created_at"), payload.get("created_at")):
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return datetime.fromtimestamp(candidate, UTC)
            if isinstance(candidate, str):
                try:
                    parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return fallback if fallback.tzinfo is not None else fallback.replace(tzinfo=UTC)
