"""
Outbound & Scheduled Message Service.

All outbound messages pass through ComplianceEngine.evaluate_outbound() at send time
(and again at execution time for scheduled messages).
"""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.core.compliance import ComplianceEngine, ensure_utc, normalize_e164_phone
from app.core.logging import log_structured_event
from app.models.customer import Customer
from app.models.message import Message
from app.schemas.message import MessageSendRequest
from app.services.whatsapp_client import WhatsAppCloudClient, whatsapp_client


class MessageService:
    """Centralized service for validating, scheduling, sending, and tracking WhatsApp messages."""

    @staticmethod
    def resolve_customer(
        db: Session,
        customer_id: Optional[int],
        recipient_phone: Optional[str],
    ) -> tuple[Optional[Customer], str]:
        customer: Optional[Customer] = None
        phone = normalize_e164_phone(recipient_phone or "")

        if customer_id is not None:
            customer = db.query(Customer).filter(Customer.id == customer_id).first()
            if customer and not phone:
                phone = customer.phone_number

        if customer is None and phone:
            customer = db.query(Customer).filter(Customer.phone_number == phone).first()

        return customer, phone

    @classmethod
    def send_or_schedule_message(
        cls,
        db: Session,
        request: MessageSendRequest,
        campaign_id: Optional[int] = None,
        conversation_id: Optional[int] = None,
        client: Optional[WhatsAppCloudClient] = None,
        force_dry_run: bool = False,
    ) -> Message:
        """
        Evaluate outbound message through the Strict WhatsApp Policy Compliance Engine,
        and either schedule it for future execution or dispatch it via Meta WhatsApp Cloud API.
        """
        customer, phone = cls.resolve_customer(db, request.customer_id, request.recipient_phone)
        now = datetime.now(timezone.utc)
        scheduled_at = ensure_utc(request.scheduled_at)
        is_scheduled_future = bool(scheduled_at and scheduled_at > now)

        # Evaluate compliance (do not reserve immediate rate-limit slot if scheduled for the future)
        verdict = ComplianceEngine.evaluate_outbound(
            db=db,
            customer=customer,
            recipient_phone=phone,
            message_type=request.message_type,
            category=request.category,
            content=request.content,
            template_name=request.template_name,
            template_language=request.template_language,
            template_params=request.template_params,
            reserve_rate_limit=not is_scheduled_future,
            reference_time=scheduled_at if is_scheduled_future else now,
        )

        log_structured_event(
            "outbound_message_attempt",
            f"Outbound message attempt evaluated: allowed={verdict.allowed}",
            customer_id=customer.id if customer else None,
            recipient_phone=phone,
            message_type=request.message_type,
            category=request.category,
            template_name=request.template_name,
            allowed=verdict.allowed,
            reason_code=verdict.reason_code,
        )

        # If customer doesn't exist at all, we cannot attach a valid customer_id FK unless created or blocked;
        # if customer is None, return a transient or raise HTTP-friendly error or attach if customer exists.
        if customer is None:
            raise PermissionError(
                f"{verdict.reason_code}: {verdict.reason_detail}"
            )

        if not verdict.allowed:
            blocked_msg = Message(
                customer_id=customer.id,
                conversation_id=conversation_id,
                campaign_id=campaign_id,
                template_id=verdict.template.id if verdict.template else None,
                recipient_phone=phone,
                direction="outbound",
                message_type=request.message_type,
                category=request.category,
                content=request.content,
                template_name=request.template_name,
                template_language=request.template_language,
                template_params=request.template_params,
                media_url=request.media_url,
                media_caption=request.media_caption,
                interactive_payload=request.interactive_payload,
                status="blocked_compliance",
                compliance_approved=False,
                compliance_rejection_reason=f"{verdict.reason_code}: {verdict.reason_detail}",
                scheduled_at=scheduled_at,
                created_at=now,
            )
            db.add(blocked_msg)
            db.commit()
            db.refresh(blocked_msg)
            return blocked_msg

        if is_scheduled_future:
            scheduled_msg = Message(
                customer_id=customer.id,
                conversation_id=conversation_id,
                campaign_id=campaign_id,
                template_id=verdict.template.id if verdict.template else None,
                recipient_phone=phone,
                direction="outbound",
                message_type=request.message_type,
                category=request.category,
                content=request.content,
                template_name=request.template_name,
                template_language=request.template_language,
                template_params=request.template_params,
                media_url=request.media_url,
                media_caption=request.media_caption,
                interactive_payload=request.interactive_payload,
                status="scheduled",
                compliance_approved=True,
                scheduled_at=scheduled_at,
                created_at=now,
            )
            db.add(scheduled_msg)
            db.commit()
            db.refresh(scheduled_msg)
            return scheduled_msg

        msg = Message(
            customer_id=customer.id,
            conversation_id=conversation_id,
            campaign_id=campaign_id,
            template_id=verdict.template.id if verdict.template else None,
            recipient_phone=phone,
            direction="outbound",
            message_type=request.message_type,
            category=request.category,
            content=request.content,
            template_name=request.template_name,
            template_language=request.template_language,
            template_params=request.template_params,
            media_url=request.media_url,
            media_caption=request.media_caption,
            interactive_payload=request.interactive_payload,
            status="queued",
            compliance_approved=True,
            created_at=now,
        )
        db.add(msg)
        db.flush()

        return cls._dispatch_to_meta(
            db=db,
            message=msg,
            client=client or whatsapp_client,
            force_dry_run=force_dry_run,
        )

    @classmethod
    def execute_scheduled_message(
        cls,
        db: Session,
        message: Message,
        client: Optional[WhatsAppCloudClient] = None,
    ) -> Message:
        """
        Execute a previously scheduled message at send time.
        CRITICAL REQUIREMENT: Revalidates customer consent, opt-out status, template approval,
        template category, 24-hour window, account status, and sending limits BEFORE sending.
        """
        customer = db.query(Customer).filter(Customer.id == message.customer_id).first()
        now = datetime.now(timezone.utc)

        if customer and customer.opted_out:
            message.status = "cancelled_opt_out"
            message.compliance_approved = False
            message.compliance_rejection_reason = "CUSTOMER_OPTED_OUT: Customer opted out before scheduled send time."
            db.commit()
            db.refresh(message)
            return message

        verdict = ComplianceEngine.evaluate_outbound(
            db=db,
            customer=customer,
            recipient_phone=message.recipient_phone,
            message_type=message.message_type,
            category=message.category,
            content=message.content,
            template_name=message.template_name,
            template_language=message.template_language or "en_US",
            template_params=message.template_params,
            reserve_rate_limit=True,
            reference_time=now,
        )

        if not verdict.allowed:
            message.status = (
                "cancelled_opt_out"
                if verdict.reason_code in {"CUSTOMER_OPTED_OUT", "CATEGORY_OPTED_OUT"}
                else "blocked_compliance"
            )
            message.compliance_approved = False
            message.compliance_rejection_reason = f"{verdict.reason_code}: {verdict.reason_detail}"
            db.commit()
            db.refresh(message)
            return message

        return cls._dispatch_to_meta(
            db=db,
            message=message,
            client=client or whatsapp_client,
        )

    @classmethod
    def _dispatch_to_meta(
        cls,
        db: Session,
        message: Message,
        client: WhatsAppCloudClient,
        force_dry_run: bool = False,
    ) -> Message:
        """Construct the Meta Graph API payload and dispatch via HTTPX client."""
        mtype = message.message_type.lower()
        if mtype == "text":
            payload = client.build_text_payload(message.recipient_phone, message.content or "")
        elif mtype == "template":
            payload = client.build_template_payload(
                recipient_phone=message.recipient_phone,
                template_name=message.template_name or "",
                language_code=message.template_language or "en_US",
                body_params=message.template_params,
            )
        elif mtype in {"image", "document", "audio", "video"}:
            payload = client.build_media_payload(
                recipient_phone=message.recipient_phone,
                media_type=mtype,
                media_url=message.media_url or "",
                caption=message.media_caption,
            )
        elif mtype == "interactive":
            payload = client.build_interactive_payload(
                recipient_phone=message.recipient_phone,
                interactive_spec=message.interactive_payload or {},
            )
        else:
            message.status = "failed"
            message.error_code = "UNSUPPORTED_MESSAGE_TYPE"
            message.error_message = f"Message type '{mtype}' is not supported."
            db.commit()
            db.refresh(message)
            return message

        result = client.send_payload(payload, force_dry_run=force_dry_run)
        now = datetime.now(timezone.utc)

        if result.success:
            message.whatsapp_message_id = result.whatsapp_message_id
            message.status = result.status_label  # "sent" or "dry_run"
            message.sent_at = now
        else:
            message.status = "failed"
            message.error_code = result.error_code
            message.error_message = result.error_message
            message.failed_at = now
            log_structured_event(
                "outbound_send_failed",
                f"Failed to send message {message.id}: {result.error_code}",
                message_id=message.id,
                error_code=result.error_code,
                is_retryable=result.is_retryable,
            )

            # If Meta returned an account policy/quality restriction error, automatically pause account sending
            if result.triggers_account_pause:
                ComplianceEngine.set_account_quality_status(
                    db=db,
                    quality_status="RESTRICTED",
                    actor="meta_api_protection",
                    reason=f"Automatically paused due to Meta API error {result.error_code}: {result.error_message}",
                )

        db.commit()
        db.refresh(message)
        return message

    @staticmethod
    def update_delivery_status(
        db: Session,
        whatsapp_message_id: str,
        new_status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Optional[Message]:
        """Update outbound message status from Meta webhook status notifications (sent, delivered, read, failed)."""
        msg = db.query(Message).filter(Message.whatsapp_message_id == whatsapp_message_id).first()
        if not msg:
            return None

        now = datetime.now(timezone.utc)
        normalized = new_status.lower().strip()

        # Maintain progressive status ordering (e.g. do not overwrite 'read' with 'delivered')
        status_rank = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
        current_rank = status_rank.get(msg.status, 0)
        incoming_rank = status_rank.get(normalized, 0)

        if normalized == "sent":
            if current_rank <= incoming_rank:
                msg.status = "sent"
            msg.sent_at = msg.sent_at or now
        elif normalized == "delivered":
            if current_rank <= incoming_rank:
                msg.status = "delivered"
            msg.delivered_at = msg.delivered_at or now
        elif normalized == "read":
            msg.status = "read"
            msg.delivered_at = msg.delivered_at or now
            msg.read_at = msg.read_at or now
        elif normalized == "failed":
            msg.status = "failed"
            msg.failed_at = msg.failed_at or now
            msg.error_code = error_code or msg.error_code
            msg.error_message = error_message or msg.error_message

        db.commit()
        db.refresh(msg)
        return msg
