"""Provider-neutral canonical events for deterministic downstream processing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TypeAlias
from uuid import UUID


class CanonicalEventType(str, Enum):
    PAYMENT_AUTHORIZED = "PAYMENT_AUTHORIZED"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    REFUND_CREATED = "REFUND_CREATED"
    REFUND_PROCESSED = "REFUND_PROCESSED"
    REFUND_FAILED = "REFUND_FAILED"
    SETTLEMENT_PROCESSED = "SETTLEMENT_PROCESSED"


class CanonicalAggregateType(str, Enum):
    PAYMENT = "PAYMENT"
    REFUND = "REFUND"
    SETTLEMENT = "SETTLEMENT"


@dataclass(frozen=True)
class PaymentEventData:
    payment_id: str
    order_id: str | None
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class RefundEventData:
    refund_id: str
    payment_id: str
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class SettlementEventData:
    settlement_id: str
    amount_minor: int
    currency: str
    fees_minor: int
    tax_minor: int
    utr: str | None


CanonicalEventData: TypeAlias = PaymentEventData | RefundEventData | SettlementEventData


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: UUID
    source: str
    event_type: CanonicalEventType
    occurred_at: datetime
    aggregate_type: CanonicalAggregateType
    aggregate_id: str
    data: CanonicalEventData


@dataclass(frozen=True)
class UnsupportedEvent:
    """A persisted provider event with no V1 canonical mapping."""

    event_id: UUID
    source: str
    event_type: str
    occurred_at: datetime
    reason: str


NormalizationResult: TypeAlias = CanonicalEvent | UnsupportedEvent
