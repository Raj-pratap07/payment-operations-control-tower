"""Orchestration for idempotent provider webhook ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.enums import FinancialEventProcessingStatus
from app.models import FinancialEvent
from app.repositories.financial_events import FinancialEventRepository


RAZORPAY_SOURCE = "RAZORPAY"


@dataclass(frozen=True)
class IngestionResult:
    duplicate: bool


class WebhookIngestionService:
    def __init__(self, repository: FinancialEventRepository | None = None) -> None:
        self._repository = repository or FinancialEventRepository()

    def ingest_razorpay_event(self, db: Session, *, external_event_id: str, payload: dict[str, Any]) -> IngestionResult:
        if self._repository.get_by_source_and_external_event_id(db, source=RAZORPAY_SOURCE, external_event_id=external_event_id):
            return IngestionResult(duplicate=True)

        received_at = datetime.now(UTC)
        event = FinancialEvent(
            source=RAZORPAY_SOURCE,
            external_event_id=external_event_id,
            event_type=self._event_type(payload),
            occurred_at=self._occurred_at(payload, fallback=received_at),
            received_at=received_at,
            signature_valid=True,
            raw_payload=payload,
            processing_status=FinancialEventProcessingStatus.RECEIVED,
            processing_error=None,
        )
        self._repository.add(db, event)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if self._repository.get_by_source_and_external_event_id(db, source=RAZORPAY_SOURCE, external_event_id=external_event_id):
                return IngestionResult(duplicate=True)
            raise
        return IngestionResult(duplicate=False)

    @staticmethod
    def _event_type(payload: dict[str, Any]) -> str:
        event_type = payload.get("event")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Webhook payload is missing an event type.")
        return event_type

    @staticmethod
    def _occurred_at(payload: dict[str, Any], *, fallback: datetime) -> datetime:
        created_at = payload.get("created_at")
        if isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
            return datetime.fromtimestamp(created_at, UTC)
        if isinstance(created_at, str):
            try:
                parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                return fallback
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return fallback
