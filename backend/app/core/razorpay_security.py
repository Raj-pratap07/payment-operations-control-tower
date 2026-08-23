"""Razorpay webhook signature verification."""

import hashlib
import hmac


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Return whether a Razorpay HMAC-SHA256 signature matches the raw body."""
    expected_signature = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_signature, signature)
