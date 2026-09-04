from datetime import UTC, datetime
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from app.core.enums import IncidentType, PaymentStatus
from app.database.database import SessionLocal
from app.models import Incident, Payment
from simulator import run


@pytest.fixture(autouse=True)
def clean_demo_records():
    run.main(["--reset"])
    yield
    run.main(["--reset"])


def test_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert run.main(["--list"]) == 0
    assert capsys.readouterr().out.splitlines() == list(run.SCENARIOS)


def test_clean_payment_has_no_incident() -> None:
    with SessionLocal() as db:
        result = run.clean_payment(db, run.new_run_id())
        db.commit()
        assert result.incident_id is None
        assert db.get(Payment, result.payment_id).status is PaymentStatus.CAPTURED


@pytest.mark.parametrize(
    ("scenario", "incident_type"),
    [
        (run.payment_state_conflict, IncidentType.PAYMENT_STATE_CONFLICT),
        (run.settlement_discrepancy, IncidentType.SETTLEMENT_DISCREPANCY),
        (run.refund_drift, IncidentType.REFUND_FINANCIAL_DRIFT),
        (run.settlement_credit_delay, IncidentType.SETTLEMENT_CREDIT_DELAY),
    ],
)
def test_incident_scenarios_use_existing_detection(scenario, incident_type: IncidentType) -> None:
    with SessionLocal() as db:
        result = scenario(db, run.new_run_id())
        incident = db.get(Incident, result.incident_id)
        assert incident is not None
        assert incident.incident_type is incident_type


def test_settlement_discrepancy_uses_deterministic_inr_amounts() -> None:
    with SessionLocal() as db:
        result = run.settlement_discrepancy(db, run.new_run_id())
        incident = db.get(Incident, result.incident_id)
        assert incident is not None
        assert incident.financial_exposure == run.PRIMARY_DIFFERENCE == 2_800_000
        assert incident.currency == "INR"


def test_run_ids_are_unique() -> None:
    assert run.new_run_id() != run.new_run_id()


def test_reset_preserves_unrelated_data() -> None:
    real_id = f"REAL:{uuid4()}"
    with SessionLocal() as db:
        real_payment = Payment(provider_payment_id=real_id, amount=100, currency="INR", status=PaymentStatus.CREATED, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        db.add(real_payment)
        db.commit()
        run.clean_payment(db, run.new_run_id())
        run.reset_demo(db)
        assert db.scalar(select(Payment).where(Payment.provider_payment_id == real_id)) is not None


def test_settlement_scenario_delegates_incident_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int | None] = []
    original = run.IncidentDetectionService.detect_settlement_discrepancy

    def spy(self, db, settlement, *, detected_at, expected_amount=None):
        calls.append(expected_amount)
        return original(self, db, settlement, detected_at=detected_at, expected_amount=expected_amount)

    monkeypatch.setattr(run.IncidentDetectionService, "detect_settlement_discrepancy", spy)
    with SessionLocal() as db:
        run.settlement_discrepancy(db, run.new_run_id())
    assert calls == [run.PRIMARY_EXPECTED]