"""
Automated Customer Support Engine for Kanti Auto Parts (KAP).

Handles:
- Configurable business greeting
- Keyword-based routing & FAQ responses
- Product inquiry handling (asks for vehicle model and year; never invents stock or prices)
- Location & operating hours inquiry handling
- Order inquiry handling (escalates to human agent; never fabricates order status)
- Business hours awareness
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

AUTO_PARTS_CATALOG_KEYWORDS = [
    "brake pads",
    "brake pad",
    "brake disc",
    "brake rotor",
    "brakes",
    "oil filter",
    "air filter",
    "fuel filter",
    "cabin filter",
    "spark plug",
    "spark plugs",
    "clutch plate",
    "clutch kit",
    "clutch",
    "shock absorber",
    "suspension",
    "headlight",
    "taillight",
    "wiper blade",
    "battery",
    "alternator",
    "radiator",
    "starter motor",
    "timing belt",
    "water pump",
    "gasket",
    "bearing",
]

GREETING_KEYWORDS = {"hello", "hi", "hey", "namaste", "good morning", "good afternoon", "good evening"}
LOCATION_KEYWORDS = {"location", "address", "where are you", "store hours", "operating hours", "timing", "directions"}
ORDER_KEYWORDS = {"order status", "my order", "track order", "tracking", "where is my order", "invoice", "dispatch"}
ESCALATION_KEYWORDS = {"human", "agent", "representative", "team member", "manager", "speak to someone", "talk to someone"}
PRICE_OR_STOCK_KEYWORDS = {"how much", "price", "cost", "in stock", "available right now", "quote"}


class AutomationService:
    """Processes incoming WhatsApp messages and generates compliant, accurate customer support responses."""

    @staticmethod
    def seed_default_rules(db: Session) -> None:
        """Seed baseline configurable automation rules in the database if none exist."""
        default_rules = [
            {
                "name": "kap_greeting",
                "rule_type": "greeting",
                "keywords": ["hello", "hi", "hey", "namaste"],
                "response_text": settings.BUSINESS_GREETING,
                "requires_human_escalation": False,
                "priority": 10,
            },
            {
                "name": "kap_location_hours",
                "rule_type": "location",
                "keywords": ["location", "address", "where are you", "operating hours", "store hours"],
                "response_text": (
                    "Our team can share our store location and operating hours. "
                    "Would you like to speak with a team member?"
                ),
                "requires_human_escalation": False,
                "priority": 20,
            },
            {
                "name": "kap_order_inquiry",
                "rule_type": "order_inquiry",
                "keywords": ["order status", "my order", "track order", "invoice", "delivery status"],
                "response_text": (
                    "To provide verified information on your order, I am connecting you with a Kanti Auto Parts "
                    "team member. Please share your order or invoice number."
                ),
                "requires_human_escalation": True,
                "priority": 30,
            },
        ]
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
        NEVER invents product availability, stock, prices, delivery promises, or order status.
        """
        cleaned = incoming_text.strip()
        lowered = cleaned.lower()
        normalized_words = re.sub(r"[^\w\s]", "", lowered).strip()

        # 1. Explicit request for human agent
        if any(kw in lowered for kw in ESCALATION_KEYWORDS) or normalized_words in {"yes", "yes please"}:
            return (
                "I have notified a Kanti Auto Parts team member to assist you directly. "
                "A specialist will review your conversation and reply shortly.",
                True,
                "Customer requested human agent assistance",
            )

        # 2. Order inquiry -> escalate to human agent (never fabricate order status or delivery date)
        if any(kw in lowered for kw in ORDER_KEYWORDS) or re.search(r"\border\b", lowered):
            return (
                "To provide verified information on your order, I am connecting you with a Kanti Auto Parts "
                "team member. Please share your order or invoice number.",
                True,
                "Order status inquiry requires verified ERP/human lookup",
            )

        # 3. Location / store hours inquiry
        if any(kw in lowered for kw in LOCATION_KEYWORDS):
            return (
                "Our team can share our store location and operating hours. "
                "Would you like to speak with a team member?",
                False,
                None,
            )

        # 4. Product / Auto part inquiry
        matched_part: Optional[str] = None
        for part in AUTO_PARTS_CATALOG_KEYWORDS:
            if part in lowered:
                matched_part = part
                break

        if matched_part:
            # Check if customer is asking for exact price or stock or already provided a 4-digit year
            has_year = bool(re.search(r"\b(19[89]\d|20[012]\d)\b", lowered))
            asks_price_or_stock = any(pk in lowered for pk in PRICE_OR_STOCK_KEYWORDS)
            if has_year or asks_price_or_stock:
                return (
                    f"Thank you for sharing those details regarding {matched_part}. "
                    "Because live inventory and pricing require verification by our parts counter, "
                    "I am escalating your request to a Kanti Auto Parts specialist who will confirm availability and price.",
                    True,
                    f"Inventory/pricing verification required for {matched_part}",
                )
            display_part = "brake pads" if "brake pad" in matched_part else matched_part
            return (
                f"Certainly! Could you please share your vehicle model and year so we can help you find the correct {display_part}?",
                False,
                None,
            )

        # If customer replies with a vehicle model and 4-digit year (e.g. "Swift 2021" or "Hyundai Creta 2022")
        if re.search(r"\b(19[89]\d|20[012]\d)\b", lowered):
            return (
                "Thank you for providing your vehicle model and year. I have connected your inquiry with our "
                "parts specialist team to verify exact fitment, stock, and pricing for your vehicle.",
                True,
                "Customer provided vehicle model/year for fitment & stock verification",
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
            "Thank you for contacting Kanti Auto Parts. To ensure you receive accurate assistance, "
            "I have forwarded your message to our customer support team. You can also share your "
            "vehicle model and year or order number for faster service.",
            True,
            "Unrecognized inquiry escalated to human support agent",
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
