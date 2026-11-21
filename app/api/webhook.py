"""
Official Meta WhatsApp Cloud API Webhook Endpoints.

Implements:
- GET /webhook: Hub challenge verification with constant-time verify_token comparison.
- POST /webhook: X-Hub-Signature-256 HMAC-SHA256 validation, idempotent event deduplication,
  quick HTTP 200 acknowledgement, and background processing.
"""
import json
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from app.core.logging import log_structured_event
from app.core.security import verify_webhook_signature, verify_webhook_token
from app.workers.message_worker import process_webhook_payload_sync

router = APIRouter(tags=["Meta WhatsApp Webhook"])


@router.get(
    "/webhook",
    summary="Meta WhatsApp Webhook Verification Endpoint",
    response_class=PlainTextResponse,
)
def verify_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    """
    Verify webhook subscription with Meta Graph API:
    - Checks hub.mode == 'subscribe'
    - Compares hub.verify_token securely via constant-time HMAC digest
    - Returns hub.challenge plain text only when verification succeeds
    """
    if not verify_webhook_token(hub_mode, hub_verify_token) or hub_challenge is None:
        log_structured_event(
            "webhook_verification_failed",
            "Rejected invalid Meta webhook verification request",
            hub_mode=hub_mode,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Webhook verification failed: invalid mode or verify token.",
        )

    log_structured_event(
        "webhook_verification_succeeded",
        "Meta webhook verification succeeded",
        hub_mode=hub_mode,
    )
    return PlainTextResponse(content=str(hub_challenge), status_code=status.HTTP_200_OK)


@router.post(
    "/webhook",
    summary="Receive and process Meta WhatsApp webhook events",
    status_code=status.HTTP_200_OK,
)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Receive incoming WhatsApp messages and delivery/read status updates from Meta:
    - Validates X-Hub-Signature-256 using HMAC-SHA256 and Meta App Secret.
    - Rejects missing or invalid signatures.
    - Parses payload safely and supports multiple events in a single webhook call.
    - Offloads processing to background worker and returns HTTP 200 immediately.
    """
    raw_body = await request.body()
    signature_header = request.headers.get("X-Hub-Signature-256")

    if not verify_webhook_signature(raw_body, signature_header):
        log_structured_event(
            "webhook_signature_invalid",
            "Rejected webhook POST with missing or invalid X-Hub-Signature-256 header",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Hub-Signature-256 webhook signature.",
        )

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON webhook payload.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object.",
        )

    log_structured_event(
        "webhook_event_received",
        "Accepted signed Meta WhatsApp webhook payload",
        object_type=payload.get("object"),
        entry_count=len(payload.get("entry") or []),
    )

    background_tasks.add_task(process_webhook_payload_sync, payload)
    return {"status": "accepted"}
