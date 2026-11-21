"""
Background Worker for Webhook Processing, Scheduled Message Execution, and Exponential Backoff Retries.

Supports both Celery distributed workers (Redis broker) and synchronous/FastAPI BackgroundTasks
execution for local development and automated testing.
"""
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Optional

from celery import Celery
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import log_structured_event
from app.database.connection import SessionLocal
from app.models.campaign import Campaign, CampaignRecipient
from app.models.message import Message
from app.models.system import WebhookEvent
from app.services.automation_service import AutomationService
from app.services.customer_service import CustomerService
from app.services.message_service import MessageService
from app.services.whatsapp_client import PERMANENT_META_ERROR_CODES

celery_app = Celery(
    "kap_whatsapp_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER or settings.APP_ENV == "testing",
    beat_schedule={
        "process-due-scheduled-messages-every-60-seconds": {
            "task": "app.workers.message_worker.run_scheduled_messages_task",
            "schedule": 60.0,
        },
    },
)


def _extract_incoming_text(msg_item: dict[str, Any]) -> tuple[str, str]:
    """Extract normalized message type and text content from a Meta webhook message object."""
    mtype = msg_item.get("type", "text")
    if mtype == "text":
        body = (msg_item.get("text") or {}).get("body", "")
        return "text", body
    if mtype == "interactive":
        interactive = msg_item.get("interactive") or {}
        itype = interactive.get("type")
        if itype == "button_reply":
            reply = interactive.get("button_reply") or {}
            return "interactive", reply.get("title") or reply.get("id") or ""
        if itype == "list_reply":
            reply = interactive.get("list_reply") or {}
            return "interactive", reply.get("title") or reply.get("id") or ""
        return "interactive", json.dumps(interactive)
    if mtype == "button":
        button = msg_item.get("button") or {}
        return "interactive", button.get("text") or button.get("payload") or ""
    if mtype in {"image", "document", "audio", "video"}:
        media_obj = msg_item.get(mtype) or {}
        caption = media_obj.get("caption") or f"[{mtype.upper()}_RECEIVED]"
        return mtype, caption
    return mtype, f"[{mtype.upper()}]"


def process_webhook_payload_sync(payload: dict[str, Any], db: Optional[Session] = None) -> dict[str, int]:
    """
    Process a validated Meta WhatsApp webhook payload idempotently.
    Handles multiple entries, multiple changes, incoming customer messages, and delivery/read status updates.
    Never assumes every webhook contains a customer message.
    """
    own_session = False
    if db is None:
        db = SessionLocal()
        own_session = True

    stats = {
        "messages_processed": 0,
        "statuses_processed": 0,
        "duplicates_ignored": 0,
    }

    try:
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        entries = payload.get("entry") or []
        if not isinstance(entries, list):
            return stats

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            changes = entry.get("changes") or []
            if not isinstance(changes, list):
                continue

            for change in changes:
                if not isinstance(change, dict):
                    continue
                value = change.get("value") or {}
                if not isinstance(value, dict):
                    continue

                # Build map of wa_id -> profile name from contacts array if present
                contact_names: dict[str, str] = {}
                for contact in value.get("contacts") or []:
                    if isinstance(contact, dict):
                        wa_id = str(contact.get("wa_id") or "").strip()
                        cname = (contact.get("profile") or {}).get("name")
                        if wa_id and cname:
                            contact_names[wa_id] = cname

                # 1. Process incoming messages (if any)
                for msg_item in value.get("messages") or []:
                    if not isinstance(msg_item, dict):
                        continue
                    wamid = str(msg_item.get("id") or "").strip()
                    sender_wa_id = str(msg_item.get("from") or "").strip()
                    if not wamid or not sender_wa_id:
                        continue

                    event_key = f"msg:{wamid}"
                    existing_event = (
                        db.query(WebhookEvent).filter(WebhookEvent.event_key == event_key).first()
                    )
                    if existing_event:
                        stats["duplicates_ignored"] += 1
                        log_structured_event(
                            "webhook_duplicate_ignored",
                            f"Ignored duplicate incoming message webhook event {event_key}",
                            event_key=event_key,
                            whatsapp_id=wamid,
                        )
                        continue

                    try:
                        db.add(
                            WebhookEvent(
                                event_key=event_key,
                                event_type="message",
                                whatsapp_id=wamid,
                                payload_hash=payload_hash,
                                processing_status="processed",
                            )
                        )
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        stats["duplicates_ignored"] += 1
                        continue

                    profile_name = contact_names.get(sender_wa_id)
                    customer = CustomerService.get_or_create_from_inbound(
                        db=db,
                        phone_number=f"+{sender_wa_id.lstrip('+')}",
                        profile_name=profile_name,
                    )
                    mtype, text_content = _extract_incoming_text(msg_item)
                    AutomationService.handle_incoming_message(
                        db=db,
                        customer=customer,
                        whatsapp_message_id=wamid,
                        message_type=mtype,
                        text_content=text_content,
                    )
                    stats["messages_processed"] += 1

                # 2. Process message delivery / read / failure statuses (if any)
                for status_item in value.get("statuses") or []:
                    if not isinstance(status_item, dict):
                        continue
                    wamid = str(status_item.get("id") or "").strip()
                    status_val = str(status_item.get("status") or "").strip().lower()
                    timestamp_val = str(status_item.get("timestamp") or "").strip()
                    if not wamid or not status_val:
                        continue

                    event_key = f"status:{wamid}:{status_val}:{timestamp_val}"
                    existing_event = (
                        db.query(WebhookEvent).filter(WebhookEvent.event_key == event_key).first()
                    )
                    if existing_event:
                        stats["duplicates_ignored"] += 1
                        continue

                    try:
                        db.add(
                            WebhookEvent(
                                event_key=event_key,
                                event_type="status",
                                whatsapp_id=wamid,
                                payload_hash=payload_hash,
                                processing_status="processed",
                            )
                        )
                        db.commit()
                    except IntegrityError:
                        db.rollback()
                        stats["duplicates_ignored"] += 1
                        continue

                    error_code: Optional[str] = None
                    error_message: Optional[str] = None
                    errors_list = status_item.get("errors") or []
                    if errors_list and isinstance(errors_list[0], dict):
                        err_obj = errors_list[0]
                        error_code = str(err_obj.get("code") or "")
                        error_message = err_obj.get("title") or err_obj.get("message")

                    updated_msg = MessageService.update_delivery_status(
                        db=db,
                        whatsapp_message_id=wamid,
                        new_status=status_val,
                        error_code=error_code,
                        error_message=error_message,
                    )

                    # Synchronize campaign recipient & campaign counters if part of a campaign
                    if updated_msg and updated_msg.campaign_id:
                        rec = (
                            db.query(CampaignRecipient)
                            .filter(CampaignRecipient.message_id == updated_msg.id)
                            .first()
                        )
                        if rec:
                            rec.delivery_status = updated_msg.status
                        camp = db.query(Campaign).filter(Campaign.id == updated_msg.campaign_id).first()
                        if camp:
                            if status_val == "delivered":
                                camp.delivered_count += 1
                            elif status_val == "read":
                                camp.read_count += 1
                            elif status_val == "failed":
                                camp.failed_count += 1
                        db.commit()

                    stats["statuses_processed"] += 1

        return stats
    finally:
        if own_session:
            db.close()


def process_scheduled_messages_now(db: Optional[Session] = None) -> dict[str, int]:
    """
    Process all due scheduled outbound messages.
    Revalidates compliance (consent, opt-out status, template approval, 24h window,
    account status, and rate limits) at send time before every send.
    """
    own_session = False
    if db is None:
        db = SessionLocal()
        own_session = True

    results = {
        "total_due": 0,
        "sent_or_dry_run": 0,
        "blocked_or_cancelled": 0,
        "failed": 0,
    }
    try:
        now = datetime.now(timezone.utc)
        due_messages = (
            db.query(Message)
            .filter(
                Message.direction == "outbound",
                Message.status == "scheduled",
                Message.scheduled_at <= now,
            )
            .order_by(Message.scheduled_at.asc())
            .all()
        )
        results["total_due"] = len(due_messages)

        for msg in due_messages:
            updated = MessageService.execute_scheduled_message(db=db, message=msg)
            if updated.status in {"sent", "dry_run"}:
                results["sent_or_dry_run"] += 1
            elif updated.status in {"blocked_compliance", "cancelled_opt_out"}:
                results["blocked_or_cancelled"] += 1
            else:
                results["failed"] += 1

        return results
    finally:
        if own_session:
            db.close()


@celery_app.task(bind=True, max_retries=3)
def process_webhook_event_task(self, payload: dict[str, Any]) -> dict[str, int]:
    """Celery background task for processing incoming Meta webhook payloads."""
    return process_webhook_payload_sync(payload)


@celery_app.task(bind=True, max_retries=3)
def retry_transient_message_task(self, message_id: int) -> dict[str, Any]:
    """
    Celery task to retry a failed outbound message ONLY if the failure was transient.
    Never retries permanent Meta API errors, and always re-runs ComplianceEngine before retrying.
    """
    db = SessionLocal()
    try:
        msg = db.query(Message).filter(Message.id == message_id).first()
        if not msg:
            return {"retried": False, "reason": "MESSAGE_NOT_FOUND"}

        # Check if error code is permanent
        if msg.error_code and msg.error_code.startswith("META_"):
            parts = msg.error_code.split("_")
            if len(parts) >= 2 and parts[1].isdigit() and int(parts[1]) in PERMANENT_META_ERROR_CODES:
                return {"retried": False, "reason": f"PERMANENT_ERROR_NO_RETRY:{msg.error_code}"}

        if msg.retry_count >= 3:
            return {"retried": False, "reason": "MAX_RETRIES_EXCEEDED"}

        msg.retry_count += 1
        db.commit()

        updated = MessageService.execute_scheduled_message(db=db, message=msg)
        if updated.status == "failed" and msg.retry_count < 3:
            backoff_seconds = 2 ** msg.retry_count
            raise self.retry(countdown=backoff_seconds)

        return {"retried": True, "status": updated.status, "retry_count": updated.retry_count}
    finally:
        db.close()


@celery_app.task
def run_scheduled_messages_task() -> dict[str, int]:
    """Periodic Celery Beat task to dispatch due scheduled messages."""
    return process_scheduled_messages_now()
