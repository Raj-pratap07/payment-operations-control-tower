"""Persistence operations for immutable financial events."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinancialEvent


class FinancialEventRepository:
    def get_by_source_and_external_event_id(self, db: Session, *, source: str, external_event_id: str) -> FinancialEvent | None:
        return db.scalar(select(FinancialEvent).where(FinancialEvent.source == source, FinancialEvent.external_event_id == external_event_id))

    def add(self, db: Session, event: FinancialEvent) -> None:
        db.add(event)
