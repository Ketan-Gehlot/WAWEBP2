"""
Automated Customer Support Engine for Maxis Gold Jewellery.

Handles:
- Configurable business greeting
- Keyword-based routing & FAQ responses
- Jewellery product & gold rate inquiry handling (never invents live gold rates, stock, or prices)
- Showroom location & operating hours inquiry handling
- Order inquiry handling (escalates to human agent; never fabricates order status)
- Human agent escalation
- Strict compliance: all automated replies pass through MessageService -> ComplianceEngine
- Never autonomously initiates marketing campaigns.
"""
from datetime import datetime, timedelta, timezone
import re
from typing import Optional
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import log_structured_event
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.system import AutomationRule
from app.schemas.message import MessageSendRequest
from app.services.consent_service import ConsentService, is_opt_out_message
from app.services.message_service import MessageService

JEWELLERY_CATALOG_KEYWORDS = [
    "bridal necklace",
    "bridal necklaces",
    "gold necklace",
    "gold necklaces",
    "necklace",
    "necklaces",
    "bridal jewellery",
    "bridal jewelry",
    "bridal set",
    "mangalsutra",
    "gold ring",
    "gold rings",
    "diamond ring",
    "ring",
    "rings",
    "gold bangle",
    "gold bangles",
    "bangle",
    "bangles",
    "bracelet",
    "bracelets",
    "earrings",
    "earring",
    "jhumka",
    "gold chain",
    "chain",
    "pendant",
    "polki",
    "kundan",
    "diamond",
    "gold coin",
    "gold coins",
    "22k",
    "24k",
    "18k",
    "hallmark",
    "jewellery",
    "jewelry",
    "gold",
]

GREETING_KEYWORDS = {"hello", "hi", "hey", "namaste", "good morning", "good afternoon", "good evening"}
LOCATION_KEYWORDS = {"location", "address", "where are you", "showroom", "store hours", "operating hours", "timing", "directions"}
ORDER_KEYWORDS = {"order status", "my order", "track order", "tracking", "where is my order", "invoice", "dispatch", "delivery"}
ESCALATION_KEYWORDS = {"human", "agent", "representative", "team member", "manager", "speak to someone", "talk to someone", "appointment"}
PRICE_OR_STOCK_KEYWORDS = {"how much", "price", "cost", "gold rate", "today's rate", "rate today", "making charges", "in stock", "available right now", "quote"}


class AutomationService:
    """Processes incoming WhatsApp messages and generates compliant, accurate customer support responses."""

    @staticmethod
    def seed_default_rules(db: Session) -> None:
        """Seed or update baseline configurable automation rules for Maxis Gold Jewellery."""
        default_rules = [
            {
                "name": "maxis_greeting",
                "rule_type": "greeting",
                "keywords": ["hello", "hi", "hey", "namaste"],
                "response_text": settings.BUSINESS_GREETING,
                "requires_human_escalation": False,
                "priority": 10,
            },
            {
                "name": "maxis_showroom_location_hours",
                "rule_type": "location",
                "keywords": ["location", "address", "showroom", "where are you", "operating hours", "store hours"],
                "response_text": (
                    "Our team can share our showroom location and operating hours. "
                    "Would you like to speak with a team member?"
                ),
                "requires_human_escalation": False,
                "priority": 20,
            },
            {
                "name": "maxis_order_inquiry",
                "rule_type": "order_inquiry",
                "keywords": ["order status", "my order", "track order", "invoice", "delivery status"],
                "response_text": (
                    "To provide verified information on your order, I am connecting you with a Maxis Gold Jewellery "
                    "team member. Please share your order or invoice number."
                ),
                "requires_human_escalation": True,
                "priority": 30,
            },
        ]

        # Clean up any legacy Kanti Auto Parts default rule names/texts in existing DB
        for existing_rule in db.query(AutomationRule).all():
            if existing_rule.response_text and "Kanti Auto Parts" in existing_rule.response_text:
                existing_rule.response_text = existing_rule.response_text.replace(
                    "Kanti Auto Parts", "Maxis Gold Jewellery"
                )
            if existing_rule.name == "kap_greeting":
                existing_rule.name = "maxis_greeting"
                existing_rule.response_text = settings.BUSINESS_GREETING
            elif existing_rule.name == "kap_location_hours":
                existing_rule.name = "maxis_showroom_location_hours"
                existing_rule.response_text = (
                    "Our team can share our showroom location and operating hours. "
                    "Would you like to speak with a team member?"
                )
            elif existing_rule.name == "kap_order_inquiry":
                existing_rule.name = "maxis_order_inquiry"

        db.flush()

        for rule_data in default_rules:
            existing = db.query(AutomationRule).filter(AutomationRule.name == rule_data["name"]).first()
            if not existing:
                db.add(AutomationRule(**rule_data, is_active=True))
        db.commit()

    @staticmethod
    def get_or_create_active_conversation(db: Session, customer: Customer) -> Conversation:
        now = datetime.now(timezone.utc)
        conv = (
            db.query(Conversation)
            .filter(
                Conversation.customer_id == customer.id,
                Conversation.status.in_(("open", "escalated_to_human")),
            )
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if not conv:
            conv = Conversation(
                customer_id=customer.id,
                status="open",
                last_customer_message_at=now,
                window_expires_at=now + timedelta(hours=24),
            )
            db.add(conv)
        else:
            conv.last_customer_message_at = now
            conv.window_expires_at = now + timedelta(hours=24)
            conv.updated_at = now
        db.commit()
        db.refresh(conv)
        return conv

    @classmethod
    def determine_support_response(
        cls,
        db: Session,
        customer: Customer,
        conversation: Conversation,
        incoming_text: str,
    ) -> tuple[str, bool, Optional[str]]:
        """
        Determine the appropriate customer support response, whether human escalation is required,
        and the escalation reason.
        NEVER invents live gold rates, stock availability, making charges, delivery promises, or order status.
        """
        cleaned = incoming_text.strip()
        lowered = cleaned.lower()
        normalized_words = re.sub(r"[^\w\s]", "", lowered).strip()

        # 1. Explicit request for human specialist or showroom appointment
        if any(kw in lowered for kw in ESCALATION_KEYWORDS) or normalized_words in {"yes", "yes please"}:
            return (
                "I have notified a Maxis Gold Jewellery showroom specialist to assist you directly. "
                "A team member will review your conversation and reply shortly.",
                True,
                "Customer requested human specialist assistance",
            )

        # 2. Order inquiry -> escalate to human agent (never fabricate order status or delivery date)
        if any(kw in lowered for kw in ORDER_KEYWORDS) or re.search(r"\border\b", lowered):
            return (
                "To provide verified information on your order, I am connecting you with a Maxis Gold Jewellery "
                "team member. Please share your order or invoice number.",
                True,
                "Order status inquiry requires verified showroom/human lookup",
            )

        # 3. Showroom location / store hours inquiry
        if any(kw in lowered for kw in LOCATION_KEYWORDS):
            return (
                "Our team can share our showroom location and operating hours. "
                "Would you like to speak with a team member?",
                False,
                None,
            )

        # 4. Live gold rate / price / making charges / stock inquiry -> escalate without inventing rates
        asks_price_or_stock = any(pk in lowered for pk in PRICE_OR_STOCK_KEYWORDS)
        matched_item: Optional[str] = None
        for item in JEWELLERY_CATALOG_KEYWORDS:
            if item in lowered:
                matched_item = item
                break

        if asks_price_or_stock:
            topic = matched_item or "today's gold rate and pricing"
            return (
                f"Thank you for inquiring about {topic} at Maxis Gold Jewellery. "
                "Because live gold rates, making charges, and showroom availability are updated daily, "
                "I am connecting you with a showroom specialist who will confirm the exact verified details.",
                True,
                f"Live pricing/gold rate verification required for {topic}",
            )

        if matched_item:
            return (
                f"Certainly! Could you please share your preferred purity (22K Gold / 18K / Diamond) or occasion "
                f"so we can help you find the perfect {matched_item} at Maxis Gold Jewellery?",
                False,
                None,
            )

        # 5. Greeting
        if normalized_words in GREETING_KEYWORDS or any(
            normalized_words.startswith(g + " ") for g in GREETING_KEYWORDS
        ):
            return (settings.BUSINESS_GREETING, False, None)

        # 6. Check custom active database automation rules
        custom_rules = (
            db.query(AutomationRule)
            .filter(AutomationRule.is_active.is_(True))
            .order_by(AutomationRule.priority.asc())
            .all()
        )
        for rule in custom_rules:
            for kw in rule.keywords or []:
                if kw.lower() in lowered:
                    return (
                        rule.response_text,
                        rule.requires_human_escalation,
                        f"Matched automation rule '{rule.name}'" if rule.requires_human_escalation else None,
                    )

        # 7. Fallback response (escalates to human agent rather than guessing)
        return (
            "Thank you for contacting Maxis Gold Jewellery. To ensure you receive accurate assistance, "
            "I have forwarded your message to our showroom team. You can also share your preferred design "
            "or order number for faster service.",
            True,
            "Unrecognized inquiry escalated to human showroom specialist",
        )

    @classmethod
    def handle_incoming_message(
        cls,
        db: Session,
        customer: Customer,
        whatsapp_message_id: str,
        message_type: str,
        text_content: Optional[str],
    ) -> dict:
        """
        Record an inbound customer message, check for opt-out keywords first,
        and otherwise trigger an automated customer support reply within the 24-hour service window.
        """
        conv = cls.get_or_create_active_conversation(db, customer)

        inbound_msg = Message(
            whatsapp_message_id=whatsapp_message_id,
            customer_id=customer.id,
            conversation_id=conv.id,
            recipient_phone=customer.phone_number,
            direction="inbound",
            message_type=message_type,
            category="service",
            content=text_content,
            status="received",
            compliance_approved=True,
        )
        db.add(inbound_msg)
        db.commit()
        db.refresh(inbound_msg)

        # Check for automatic opt-out keywords (STOP, UNSUBSCRIBE, CANCEL, QUIT, END)
        if text_content and is_opt_out_message(text_content):
            opt_out_summary = ConsentService.process_opt_out(
                db=db,
                customer=customer,
                categories="ALL",
                reason=f"Customer sent opt-out keyword: '{text_content.strip()}'",
                source="whatsapp_inbound_keyword",
                evidence=f"wamid:{whatsapp_message_id}",
                actor="customer",
            )
            return {
                "action": "opt_out_processed",
                "inbound_message_id": inbound_msg.id,
                "opt_out_summary": opt_out_summary,
            }

        # Do not send automated replies if customer is opted out
        if customer.opted_out:
            return {
                "action": "ignored_opted_out_customer",
                "inbound_message_id": inbound_msg.id,
            }

        reply_text, escalate, escalation_reason = cls.determine_support_response(
            db=db,
            customer=customer,
            conversation=conv,
            incoming_text=text_content or "",
        )

        if escalate:
            conv.status = "escalated_to_human"
            conv.escalation_reason = escalation_reason
            customer.conversation_status = "escalated"
            db.commit()
            log_structured_event(
                "conversation_escalated",
                f"Conversation {conv.id} escalated to human agent: {escalation_reason}",
                conversation_id=conv.id,
                customer_id=customer.id,
                reason=escalation_reason,
            )

        outbound_request = MessageSendRequest(
            customer_id=customer.id,
            recipient_phone=customer.phone_number,
            message_type="text",
            category="service",
            content=reply_text,
        )
        outbound_msg = MessageService.send_or_schedule_message(
            db=db,
            request=outbound_request,
            conversation_id=conv.id,
        )

        return {
            "action": "auto_replied",
            "inbound_message_id": inbound_msg.id,
            "outbound_message_id": outbound_msg.id,
            "outbound_status": outbound_msg.status,
            "escalated_to_human": escalate,
        }
