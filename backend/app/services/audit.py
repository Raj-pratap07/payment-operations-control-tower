"""Append-only audit event recording."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.repositories.execution import ExecutionRepository


class AuditService:
    def __init__(self, repository: ExecutionRepository | None = None) -> None:
        self._repository = repository or ExecutionRepository()

    def record(
        self,
        db: Session,
        *,
        action_type: str,
        entity_type: str,
        entity_id: UUID | str | None,
        actor_type: str,
        actor_id: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> AuditEvent:
        audit = AuditEvent(
            actor_type=actor_type,
            actor_id=actor_id,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            reason=reason,
            evidence=evidence,
            metadata_=metadata,
            created_at=_utc(created_at or datetime.now(UTC)),
        )
        self._repository.add_audit(db, audit)
        db.flush()
        return audit

    def record_once(
        self,
        db: Session,
        *,
        action_type: str,
        entity_type: str,
        entity_id: UUID | str,
        actor_type: str,
        actor_id: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> AuditEvent:
        existing = self._repository.get_audit(
            db, action_type=action_type, entity_type=entity_type, entity_id=str(entity_id)
        )
        if existing is not None:
            return existing
        return self.record(
            db,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            evidence=evidence,
            metadata=metadata,
            created_at=created_at,
        )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)