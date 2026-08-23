"""Provider webhook HTTP endpoints."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.razorpay_security import verify_webhook_signature
from app.database.database import get_db
from app.services.webhook_ingestion import WebhookIngestionService


logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])
ingestion_service = WebhookIngestionService()


@router.post("/webhooks/razorpay", status_code=status.HTTP_200_OK)
async def ingest_razorpay_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    external_event_id = request.headers.get("x-razorpay-event-id")
    if not signature:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Razorpay signature.")
    if not external_event_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Razorpay event ID.")
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        logger.error("Razorpay webhook secret is not configured")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook verification is unavailable.")
    if not verify_webhook_signature(raw_body, signature, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Razorpay signature.")

    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Malformed JSON payload.") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook payload must be a JSON object.")

    try:
        result = ingestion_service.ingest_razorpay_event(db, external_event_id=external_event_id, payload=payload)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    except SQLAlchemyError as error:
        db.rollback()
        logger.exception("Unable to persist validated Razorpay webhook event")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Unable to persist webhook event.") from error
    return {"status": "accepted", "duplicate": result.duplicate}
