import hashlib
import hmac
import json
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import FinancialEventProcessingStatus
from app.database.database import SessionLocal
from app.main import app
from app.models import FinancialEvent


WEBHOOK_SECRET = "test-webhook-secret"


def _body(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _signature(raw_body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _headers(raw_body: bytes, event_id: str) -> dict[str, str]:
    return {
        "X-Razorpay-Signature": _signature(raw_body),
        "x-razorpay-event-id": event_id,
        "content-type": "application/json",
    }


@pytest.fixture(autouse=True)
def webhook_test_environment(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", WEBHOOK_SECRET)
    with SessionLocal() as db:
        db.execute(delete(FinancialEvent).where(FinancialEvent.source == "RAZORPAY", FinancialEvent.external_event_id.like("webhook-test-%")))
        db.commit()
    yield
    with SessionLocal() as db:
        db.execute(delete(FinancialEvent).where(FinancialEvent.source == "RAZORPAY", FinancialEvent.external_event_id.like("webhook-test-%")))
        db.commit()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


def _stored_event(event_id: str) -> FinancialEvent | None:
    with SessionLocal() as db:
        return db.scalar(select(FinancialEvent).where(FinancialEvent.source == "RAZORPAY", FinancialEvent.external_event_id == event_id))


def _event_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(FinancialEvent).where(FinancialEvent.source == "RAZORPAY", FinancialEvent.external_event_id.like("webhook-test-%"))) or 0


def test_valid_signature_persists_a_new_financial_event(client: TestClient) -> None:
    payload: dict[str, object] = {"event": "payment.captured", "created_at": 1_700_000_000, "payload": {"payment": {"entity": {"id": "pay_123"}}}}
    raw_body = _body(payload)
    response = client.post("/webhooks/razorpay", content=raw_body, headers=_headers(raw_body, "webhook-test-valid"))

    assert response.status_code == 200
    assert response.json() == {"status": "accepted", "duplicate": False}
    stored = _stored_event("webhook-test-valid")
    assert stored is not None
    assert stored.raw_payload == payload
    assert stored.signature_valid is True
    assert stored.processing_status is FinancialEventProcessingStatus.RECEIVED
    assert stored.occurred_at.tzinfo is not None


def test_invalid_signature_is_rejected_without_persisting(client: TestClient) -> None:
    raw_body = _body({"event": "payment.captured"})
    response = client.post("/webhooks/razorpay", content=raw_body, headers={"X-Razorpay-Signature": "invalid", "x-razorpay-event-id": "webhook-test-invalid"})
    assert response.status_code == 401
    assert _stored_event("webhook-test-invalid") is None


def test_missing_signature_is_rejected(client: TestClient) -> None:
    raw_body = _body({"event": "payment.captured"})
    response = client.post("/webhooks/razorpay", content=raw_body, headers={"x-razorpay-event-id": "webhook-test-missing-signature"})
    assert response.status_code == 400


def test_missing_event_id_is_rejected(client: TestClient) -> None:
    raw_body = _body({"event": "payment.captured"})
    response = client.post("/webhooks/razorpay", content=raw_body, headers={"X-Razorpay-Signature": _signature(raw_body)})
    assert response.status_code == 400


def test_unconfigured_secret_rejects_the_webhook(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    raw_body = _body({"event": "payment.captured"})
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    response = client.post("/webhooks/razorpay", content=raw_body, headers=_headers(raw_body, "webhook-test-no-secret"))
    assert response.status_code == 503
    assert _stored_event("webhook-test-no-secret") is None


def test_duplicate_event_is_acknowledged_once(client: TestClient) -> None:
    raw_body = _body({"event": "payment.captured"})
    headers = _headers(raw_body, "webhook-test-duplicate")
    assert client.post("/webhooks/razorpay", content=raw_body, headers=headers).status_code == 200
    duplicate = client.post("/webhooks/razorpay", content=raw_body, headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert _event_count() == 1


def test_valid_signature_with_malformed_json_is_rejected(client: TestClient) -> None:
    raw_body = b'{"event":'
    response = client.post("/webhooks/razorpay", content=raw_body, headers=_headers(raw_body, "webhook-test-malformed"))
    assert response.status_code == 400
    assert _stored_event("webhook-test-malformed") is None


def test_different_event_ids_create_separate_financial_events(client: TestClient) -> None:
    for event_id in ("webhook-test-first", "webhook-test-second"):
        raw_body = _body({"event": "refund.processed", "payload": {"marker": event_id}})
        response = client.post("/webhooks/razorpay", content=raw_body, headers=_headers(raw_body, event_id))
        assert response.status_code == 200
    assert _event_count() == 2
