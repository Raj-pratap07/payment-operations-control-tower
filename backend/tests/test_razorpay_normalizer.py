from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.engines.razorpay_normalizer import NormalizationError, RazorpayEventNormalizer
from app.models import FinancialEvent
from app.schemas.canonical_events import (
    CanonicalAggregateType,
    CanonicalEvent,
    CanonicalEventType,
    PaymentEventData,
    RefundEventData,
    SettlementEventData,
    UnsupportedEvent,
)


NORMALIZER = RazorpayEventNormalizer()
EVENT_TIME = datetime(2024, 1, 1, tzinfo=UTC)


def financial_event(event_type: str, payload: dict[str, object]) -> FinancialEvent:
    return FinancialEvent(
        id=uuid4(),
        source="RAZORPAY",
        external_event_id="evt_test",
        event_type=event_type,
        occurred_at=EVENT_TIME,
        received_at=EVENT_TIME,
        signature_valid=True,
        raw_payload=payload,
    )


def payment_payload() -> dict[str, object]:
    return {"payload": {"payment": {"entity": {"id": "pay_123", "order_id": "order_123", "amount": 1250, "currency": "INR", "created_at": 1_704_067_200}}}}


def refund_payload() -> dict[str, object]:
    return {"payload": {"refund": {"entity": {"id": "rfnd_123", "payment_id": "pay_123", "amount": 500, "currency": "INR", "created_at": 1_704_067_200}}}}


def settlement_payload() -> dict[str, object]:
    return {"payload": {"settlement": {"entity": {"id": "setl_123", "amount": 1000, "currency": "INR", "fee": 20, "tax": 4, "utr": "UTR123", "created_at": 1_704_067_200}}}}


@pytest.mark.parametrize(
    ("provider_event_type", "payload", "expected_type", "expected_aggregate", "expected_data_type", "expected_id"),
    [
        ("payment.authorized", payment_payload(), CanonicalEventType.PAYMENT_AUTHORIZED, CanonicalAggregateType.PAYMENT, PaymentEventData, "pay_123"),
        ("payment.captured", payment_payload(), CanonicalEventType.PAYMENT_CAPTURED, CanonicalAggregateType.PAYMENT, PaymentEventData, "pay_123"),
        ("payment.failed", payment_payload(), CanonicalEventType.PAYMENT_FAILED, CanonicalAggregateType.PAYMENT, PaymentEventData, "pay_123"),
        ("refund.created", refund_payload(), CanonicalEventType.REFUND_CREATED, CanonicalAggregateType.REFUND, RefundEventData, "rfnd_123"),
        ("refund.processed", refund_payload(), CanonicalEventType.REFUND_PROCESSED, CanonicalAggregateType.REFUND, RefundEventData, "rfnd_123"),
        ("refund.failed", refund_payload(), CanonicalEventType.REFUND_FAILED, CanonicalAggregateType.REFUND, RefundEventData, "rfnd_123"),
        ("settlement.processed", settlement_payload(), CanonicalEventType.SETTLEMENT_PROCESSED, CanonicalAggregateType.SETTLEMENT, SettlementEventData, "setl_123"),
    ],
)
def test_supported_razorpay_events_normalize(provider_event_type: str, payload: dict[str, object], expected_type: CanonicalEventType, expected_aggregate: CanonicalAggregateType, expected_data_type: type[object], expected_id: str) -> None:
    result = NORMALIZER.normalize(financial_event(provider_event_type, payload))
    assert isinstance(result, CanonicalEvent)
    assert result.event_type is expected_type
    assert result.aggregate_type is expected_aggregate
    assert result.aggregate_id == expected_id
    assert isinstance(result.data, expected_data_type)
    assert result.occurred_at.tzinfo is not None


def test_payment_amount_is_an_integer_and_currency_is_preserved() -> None:
    result = NORMALIZER.normalize(financial_event("payment.captured", payment_payload()))
    assert isinstance(result, CanonicalEvent)
    assert isinstance(result.data, PaymentEventData)
    assert isinstance(result.data.amount_minor, int)
    assert result.data.currency == "INR"


def test_settlement_fee_tax_and_utr_are_preserved() -> None:
    result = NORMALIZER.normalize(financial_event("settlement.processed", settlement_payload()))
    assert isinstance(result, CanonicalEvent)
    assert isinstance(result.data, SettlementEventData)
    assert (result.data.fees_minor, result.data.tax_minor, result.data.utr) == (20, 4, "UTR123")


def test_unknown_event_is_explicitly_unsupported() -> None:
    result = NORMALIZER.normalize(financial_event("payment.disputed", {"payload": {}}))
    assert isinstance(result, UnsupportedEvent)
    assert result.event_type == "payment.disputed"


@pytest.mark.parametrize(
    ("event_type", "payload", "message"),
    [
        ("payment.captured", {"payload": {"payment": {"entity": {"amount": 1, "currency": "INR"}}}}, "payment ID"),
        ("refund.processed", {"payload": {"refund": {"entity": {"payment_id": "pay_123", "amount": 1, "currency": "INR"}}}}, "refund ID"),
        ("settlement.processed", {"payload": {"settlement": {"entity": {"amount": 1, "currency": "INR", "fee": 0, "tax": 0}}}}, "settlement ID"),
        ("payment.captured", {"payload": {"payment": {}}}, "payment entity"),
    ],
)
def test_incomplete_or_malformed_supported_events_fail_clearly(event_type: str, payload: dict[str, object], message: str) -> None:
    with pytest.raises(NormalizationError, match=message):
        NORMALIZER.normalize(financial_event(event_type, payload))


def test_normalization_uses_financial_event_time_when_provider_time_is_missing() -> None:
    payload = payment_payload()
    entity = payload["payload"]["payment"]["entity"]
    assert isinstance(entity, dict)
    entity.pop("created_at")
    result = NORMALIZER.normalize(financial_event("payment.captured", payload))
    assert isinstance(result, CanonicalEvent)
    assert result.occurred_at == EVENT_TIME


def test_normalization_is_deterministic() -> None:
    event = financial_event("refund.processed", refund_payload())
    assert NORMALIZER.normalize(event) == NORMALIZER.normalize(event)
