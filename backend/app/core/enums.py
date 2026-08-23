"""Enumerations used by the V1 payment operations domain."""

from enum import Enum


class StringEnum(str, Enum):
    """String-backed enum suitable for SQLAlchemy and API serialization."""


class FinancialEventProcessingStatus(StringEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class PaymentStatus(StringEnum):
    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    FAILED = "FAILED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


class RefundStatus(StringEnum):
    CREATED = "CREATED"
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class SettlementStatus(StringEnum):
    CREATED = "CREATED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class IncidentType(StringEnum):
    PAYMENT_STATE_CONFLICT = "PAYMENT_STATE_CONFLICT"
    PAYMENT_SIGNAL_OVERDUE = "PAYMENT_SIGNAL_OVERDUE"
    SETTLEMENT_DISCREPANCY = "SETTLEMENT_DISCREPANCY"
    REFUND_FINANCIAL_DRIFT = "REFUND_FINANCIAL_DRIFT"
    EVENT_INTEGRITY = "EVENT_INTEGRITY"
    SETTLEMENT_CREDIT_DELAY = "SETTLEMENT_CREDIT_DELAY"
    FINANCIAL_EXPOSURE = "FINANCIAL_EXPOSURE"
    PAYMENT_PATH_DEGRADATION = "PAYMENT_PATH_DEGRADATION"


class IncidentSeverity(StringEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(StringEnum):
    OPEN = "OPEN"
    INVESTIGATING = "INVESTIGATING"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    RESOLVING = "RESOLVING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ActionProposalStatus(StringEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StringEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
