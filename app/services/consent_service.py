"""
Customer Consent and Opt-Out Management Service.

Enforces explicit opt-in tracking, preference withdrawal, automatic opt-out keyword detection,
and immediate cancellation of queued or scheduled communications when a customer opts out.
"""
import re
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.core.compliance import DISALLOWED_CONSENT_SOURCES
from app.core.logging import log_structured_event
from app.models.campaign import CampaignRecipient
from app.models.consent import ConsentRecord
from app.models.customer import Customer
from app.models.message import Message

OPT_OUT_KEYWORDS = {
    "STOP",
    "STOP ALL",
    "UNSUBSCRIBE",
    "CANCEL",
    "QUIT",
    "END",
    "OPTOUT",
    "OPT OUT",
    "OPT-OUT",
}


def is_opt_out_message(text: Optional[str]) -> bool:
    """
    Detect standard WhatsApp opt-out commands such as STOP, UNSUBSCRIBE, CANCEL, QUIT, END.
    Matches exact commands or punctuation-wrapped single commands.
    """
    if not text:
        return False
    cleaned = re.sub(r"[^\w\s\-]", "", text.strip()).upper()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned in OPT_OUT_KEYWORDS


class ConsentService:
    """Manages explicit customer consent records, preference updates, and opt-out enforcement."""

    @staticmethod
    def record_consent(
        db: Session,
        customer: Customer,
        marketing_consent: Optional[bool],
        utility_consent: Optional[bool],
        collection_method: str,
        source: str,
        evidence: str,
        actor: str = "admin",
    ) -> Customer:
        """Record verifiable customer opt-in consent and create an immutable ConsentRecord."""
        if source.lower().strip() in DISALLOWED_CONSENT_SOURCES:
            raise ValueError(
                f"Consent source '{source}' is disallowed. Purchased, scraped, or unverified bulk lists are prohibited."
            )

        now = datetime.now(timezone.utc)

        if marketing_consent is not None:
            customer.marketing_consent = marketing_consent
            db.add(
                ConsentRecord(
                    customer_id=customer.id,
                    consent_type="marketing",
                    action="opt_in" if marketing_consent else "opt_out",
                    granted=marketing_consent,
                    collection_method=collection_method,
                    source=source,
                    evidence=evidence,
                    recorded_by=actor,
                    created_at=now,
                )
            )

        if utility_consent is not None:
            customer.utility_consent = utility_consent
            db.add(
                ConsentRecord(
                    customer_id=customer.id,
                    consent_type="utility",
                    action="opt_in" if utility_consent else "opt_out",
                    granted=utility_consent,
                    collection_method=collection_method,
                    source=source,
                    evidence=evidence,
                    recorded_by=actor,
                    created_at=now,
                )
            )

        if customer.marketing_consent or customer.utility_consent:
            customer.consent_timestamp = now
            customer.consent_method = collection_method
            customer.consent_source = source
            customer.consent_evidence = evidence
            # Clear global opt-out if customer explicitly re-consented with evidence
            if customer.opted_out:
                customer.opted_out = False
                if customer.marketing_consent and customer.utility_consent:
                    customer.opted_out_categories = None
                elif customer.utility_consent and not customer.marketing_consent:
                    customer.opted_out_categories = "MARKETING"
                elif customer.marketing_consent and not customer.utility_consent:
                    customer.opted_out_categories = "UTILITY"
            elif customer.marketing_consent and customer.utility_consent:
                customer.opted_out_categories = None

        customer.updated_at = now
        db.commit()
        db.refresh(customer)

        log_structured_event(
            "consent_updated",
            f"Updated consent preferences for customer {customer.id}",
            customer_id=customer.id,
            phone_number=customer.phone_number,
            marketing_consent=customer.marketing_consent,
            utility_consent=customer.utility_consent,
            collection_method=collection_method,
            source=source,
            actor=actor,
        )
        return customer

    @staticmethod
    def process_opt_out(
        db: Session,
        customer: Customer,
        categories: str = "ALL",
        reason: str = "Customer sent opt-out command",
        source: str = "whatsapp_inbound",
        evidence: Optional[str] = None,
        actor: str = "customer",
    ) -> dict:
        """
        Immediately enforce customer opt-out:
        1. Block the relevant communication category (default ALL).
        2. Persist immutable ConsentRecord opt-out event.
        3. Cancel any queued or scheduled outbound messages and pending campaign recipients.
        """
        now = datetime.now(timezone.utc)
        norm_cat = categories.upper().strip()

        if norm_cat in {"ALL", "*"}:
            customer.opted_out = True
            customer.opted_out_categories = "ALL"
            customer.marketing_consent = False
            customer.utility_consent = False
        elif norm_cat == "MARKETING":
            customer.marketing_consent = False
            customer.opted_out_categories = "MARKETING"
            if not customer.utility_consent:
                customer.opted_out = True
                customer.opted_out_categories = "ALL"
        elif norm_cat == "UTILITY":
            customer.utility_consent = False
            customer.opted_out_categories = "UTILITY"
            if not customer.marketing_consent:
                customer.opted_out = True
                customer.opted_out_categories = "ALL"
        else:
            customer.opted_out = True
            customer.opted_out_categories = "ALL"
            customer.marketing_consent = False
            customer.utility_consent = False

        customer.opt_out_timestamp = now
        customer.opt_out_reason = reason
        customer.updated_at = now

        consent_record = ConsentRecord(
            customer_id=customer.id,
            consent_type=norm_cat.lower(),
            action="opt_out",
            granted=False,
            collection_method=source,
            source=source,
            evidence=evidence or reason,
            recorded_by=actor,
            created_at=now,
        )
        db.add(consent_record)

        # Cancel all queued or scheduled messages for this customer
        queued_query = db.query(Message).filter(
            Message.customer_id == customer.id,
            Message.direction == "outbound",
            Message.status.in_(("queued", "scheduled")),
        )
        if norm_cat == "MARKETING":
            queued_query = queued_query.filter(Message.category == "marketing")

        cancelled_messages = 0
        for msg in queued_query.all():
            msg.status = "cancelled_opt_out"
            msg.compliance_approved = False
            msg.compliance_rejection_reason = f"Cancelled due to customer opt-out ({norm_cat})"
            cancelled_messages += 1

        # Cancel pending campaign recipient entries for this customer
        pending_recipients = (
            db.query(CampaignRecipient)
            .filter(
                CampaignRecipient.customer_id == customer.id,
                CampaignRecipient.delivery_status.in_(("pending", "scheduled")),
            )
            .all()
        )
        cancelled_campaign_items = 0
        for rec in pending_recipients:
            rec.is_eligible = False
            rec.delivery_status = "cancelled_opt_out"
            rec.exclusion_reason = "CUSTOMER_OPTED_OUT"
            rec.processed_at = now
            cancelled_campaign_items += 1

        db.commit()
        db.refresh(customer)

        log_structured_event(
            "customer_opt_out",
            f"Customer {customer.id} opted out ({norm_cat}); cancelled {cancelled_messages} scheduled messages",
            customer_id=customer.id,
            phone_number=customer.phone_number,
            categories=norm_cat,
            cancelled_messages=cancelled_messages,
            cancelled_campaign_items=cancelled_campaign_items,
            source=source,
        )

        return {
            "customer_id": customer.id,
            "opted_out": customer.opted_out,
            "opted_out_categories": customer.opted_out_categories,
            "cancelled_scheduled_messages": cancelled_messages,
            "cancelled_campaign_recipients": cancelled_campaign_items,
        }
