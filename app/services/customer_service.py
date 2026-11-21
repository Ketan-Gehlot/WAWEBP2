"""
Customer Management Service.

Handles adding, updating, searching, viewing history, exporting permitted records,
and anonymizing/deleting customer data.
Never treats an imported contact as automatically opted-in.
"""
from datetime import datetime, timezone
import hashlib
from typing import Optional
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.compliance import DISALLOWED_CONSENT_SOURCES, is_24h_service_window_open, normalize_e164_phone
from app.core.logging import log_structured_event
from app.models.consent import ConsentRecord
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.schemas.customer import CustomerCreate, CustomerUpdate


class CustomerService:
    """Service layer for customer lifecycle and data privacy operations."""

    @staticmethod
    def create_customer(
        db: Session,
        payload: CustomerCreate,
        actor: str = "admin",
    ) -> Customer:
        """
        Create a new customer record.
        Enforces strict rule: importing a phone number is NOT permission to send WhatsApp messages.
        If marketing_consent or utility_consent is requested during creation, verifiable method,
        source, and evidence must be provided and must not come from a disallowed source.
        """
        phone = normalize_e164_phone(payload.phone_number)
        existing = db.query(Customer).filter(Customer.phone_number == phone).first()
        if existing:
            raise ValueError(f"Customer with phone number {phone} already exists (ID: {existing.id}).")

        has_valid_consent_proof = bool(
            payload.consent_method
            and payload.consent_source
            and payload.consent_evidence
            and payload.consent_source.lower().strip() not in DISALLOWED_CONSENT_SOURCES
        )

        # If consent proof is missing or from an unverified bulk import, force consent flags to False
        grant_marketing = bool(payload.marketing_consent and has_valid_consent_proof)
        grant_utility = bool(payload.utility_consent and has_valid_consent_proof)
        now = datetime.now(timezone.utc)

        customer = Customer(
            phone_number=phone,
            name=payload.name,
            marketing_consent=grant_marketing,
            utility_consent=grant_utility,
            consent_timestamp=now if (grant_marketing or grant_utility) else None,
            consent_method=payload.consent_method if (grant_marketing or grant_utility) else None,
            consent_source=payload.consent_source if (grant_marketing or grant_utility) else None,
            consent_evidence=payload.consent_evidence if (grant_marketing or grant_utility) else None,
            opted_out=False,
            conversation_status="new",
        )
        db.add(customer)
        db.flush()

        if grant_marketing:
            db.add(
                ConsentRecord(
                    customer_id=customer.id,
                    consent_type="marketing",
                    action="opt_in",
                    granted=True,
                    collection_method=payload.consent_method or "explicit",
                    source=payload.consent_source or "explicit",
                    evidence=payload.consent_evidence,
                    recorded_by=actor,
                )
            )
        if grant_utility:
            db.add(
                ConsentRecord(
                    customer_id=customer.id,
                    consent_type="utility",
                    action="opt_in",
                    granted=True,
                    collection_method=payload.consent_method or "explicit",
                    source=payload.consent_source or "explicit",
                    evidence=payload.consent_evidence,
                    recorded_by=actor,
                )
            )

        db.commit()
        db.refresh(customer)
        return customer

    @staticmethod
    def get_or_create_from_inbound(
        db: Session,
        phone_number: str,
        profile_name: Optional[str] = None,
    ) -> Customer:
        """
        Retrieve or create a customer record when they send an inbound WhatsApp message.
        Note: Sending an inbound inquiry opens the 24-hour customer service window,
        but does NOT automatically enroll the customer in marketing campaigns.
        """
        phone = normalize_e164_phone(phone_number)
        if not phone.startswith("+"):
            phone = f"+{phone}"

        now = datetime.now(timezone.utc)
        customer = db.query(Customer).filter(Customer.phone_number == phone).first()
        if not customer:
            customer = Customer(
                phone_number=phone,
                name=profile_name,
                last_interaction_at=now,
                last_inbound_message_at=now,
                conversation_status="active",
                marketing_consent=False,
                utility_consent=False,
                opted_out=False,
            )
            db.add(customer)
            db.commit()
            db.refresh(customer)
        else:
            if profile_name and not customer.name and not customer.is_anonymized:
                customer.name = profile_name
            customer.last_interaction_at = now
            customer.last_inbound_message_at = now
            if customer.conversation_status == "new":
                customer.conversation_status = "active"
            db.commit()
            db.refresh(customer)
        return customer

    @staticmethod
    def update_customer(
        db: Session,
        customer: Customer,
        payload: CustomerUpdate,
    ) -> Customer:
        if payload.name is not None:
            customer.name = payload.name
        if payload.conversation_status is not None:
            customer.conversation_status = payload.conversation_status
        customer.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(customer)
        return customer

    @staticmethod
    def list_customers(
        db: Session,
        search: Optional[str] = None,
        marketing_consent: Optional[bool] = None,
        utility_consent: Optional[bool] = None,
        opted_out: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Customer]:
        query = db.query(Customer).filter(Customer.is_anonymized.is_(False))
        if search:
            pattern = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Customer.phone_number.ilike(pattern),
                    Customer.name.ilike(pattern),
                )
            )
        if marketing_consent is not None:
            query = query.filter(Customer.marketing_consent.is_(marketing_consent))
        if utility_consent is not None:
            query = query.filter(Customer.utility_consent.is_(utility_consent))
        if opted_out is not None:
            query = query.filter(Customer.opted_out.is_(opted_out))

        return query.order_by(Customer.id.desc()).offset(skip).limit(min(limit, 500)).all()

    @staticmethod
    def get_customer_history(db: Session, customer: Customer) -> dict:
        consent_records = (
            db.query(ConsentRecord)
            .filter(ConsentRecord.customer_id == customer.id)
            .order_by(ConsentRecord.created_at.desc())
            .all()
        )
        messages = (
            db.query(Message)
            .filter(Message.customer_id == customer.id)
            .order_by(Message.created_at.desc())
            .limit(200)
            .all()
        )
        conversations = (
            db.query(Conversation)
            .filter(Conversation.customer_id == customer.id)
            .order_by(Conversation.updated_at.desc())
            .all()
        )
        return {
            "customer": customer,
            "service_window_open": is_24h_service_window_open(customer),
            "consent_history": consent_records,
            "messages": [
                {
                    "id": m.id,
                    "whatsapp_message_id": m.whatsapp_message_id,
                    "direction": m.direction,
                    "message_type": m.message_type,
                    "category": m.category,
                    "content": m.content,
                    "template_name": m.template_name,
                    "status": m.status,
                    "compliance_approved": m.compliance_approved,
                    "compliance_rejection_reason": m.compliance_rejection_reason,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in messages
            ],
            "conversations": [
                {
                    "id": c.id,
                    "status": c.status,
                    "assigned_agent": c.assigned_agent,
                    "escalation_reason": c.escalation_reason,
                    "window_expires_at": c.window_expires_at.isoformat() if c.window_expires_at else None,
                    "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                }
                for c in conversations
            ],
        }

    @staticmethod
    def export_permitted_customers(db: Session) -> list[dict]:
        """Export only non-anonymized, non-opted-out customer records with active consent."""
        customers = (
            db.query(Customer)
            .filter(
                Customer.is_anonymized.is_(False),
                Customer.opted_out.is_(False),
                or_(
                    Customer.marketing_consent.is_(True),
                    Customer.utility_consent.is_(True),
                ),
            )
            .order_by(Customer.id.asc())
            .all()
        )
        return [
            {
                "id": c.id,
                "phone_number": c.phone_number,
                "name": c.name,
                "marketing_consent": c.marketing_consent,
                "utility_consent": c.utility_consent,
                "consent_timestamp": c.consent_timestamp.isoformat() if c.consent_timestamp else None,
                "consent_method": c.consent_method,
                "consent_source": c.consent_source,
            }
            for c in customers
        ]

    @staticmethod
    def anonymize_customer(
        db: Session,
        customer: Customer,
        actor: str = "admin",
    ) -> Customer:
        """
        Anonymize personal data for a customer upon deletion/privacy request
        while preserving non-PII compliance audit integrity.
        """
        phone_hash = hashlib.sha256(customer.phone_number.encode("utf-8")).hexdigest()[:12]
        now = datetime.now(timezone.utc)

        customer.phone_number = f"+999{phone_hash[:10]}"
        customer.name = "ANONYMIZED_CUSTOMER"
        customer.marketing_consent = False
        customer.utility_consent = False
        customer.consent_evidence = "[REDACTED_FOR_PRIVACY]"
        customer.opted_out = True
        customer.opted_out_categories = "ALL"
        customer.opt_out_timestamp = now
        customer.opt_out_reason = "Customer data anonymized"
        customer.conversation_status = "anonymized"
        customer.is_anonymized = True
        customer.updated_at = now

        # Redact message contents and cancel queued messages
        for msg in db.query(Message).filter(Message.customer_id == customer.id).all():
            msg.content = "[REDACTED]"
            msg.recipient_phone = customer.phone_number
            if msg.status in {"queued", "scheduled"}:
                msg.status = "cancelled_opt_out"

        db.commit()
        db.refresh(customer)
        log_structured_event(
            "customer_anonymized",
            f"Anonymized customer record {customer.id}",
            customer_id=customer.id,
            actor=actor,
        )
        return customer
