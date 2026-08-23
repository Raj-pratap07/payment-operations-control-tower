"""Provider-neutral entry point for deterministic event normalization."""

from app.engines.razorpay_normalizer import RazorpayEventNormalizer
from app.models import FinancialEvent
from app.schemas.canonical_events import NormalizationResult, UnsupportedEvent


RAZORPAY_SOURCE = "RAZORPAY"


class EventNormalizationService:
    def __init__(self, razorpay_normalizer: RazorpayEventNormalizer | None = None) -> None:
        self._razorpay_normalizer = razorpay_normalizer or RazorpayEventNormalizer()

    def normalize(self, financial_event: FinancialEvent) -> NormalizationResult:
        if financial_event.source == RAZORPAY_SOURCE:
            return self._razorpay_normalizer.normalize(financial_event)
        return UnsupportedEvent(
            event_id=financial_event.id,
            source=financial_event.source,
            event_type=financial_event.event_type,
            occurred_at=financial_event.occurred_at,
            reason="No normalizer is registered for this event source.",
        )
