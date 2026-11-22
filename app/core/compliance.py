"""
Strict WhatsApp Policy Compliance Engine.

EVERY outbound message—whether initiated by an API endpoint, automated customer support,
campaign executor, or background scheduler—MUST pass through this engine before sending.
No bypass is permitted.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import log_structured_event
from app.core.rate_limiter import outbound_rate_limiter
from app.models.customer import Customer
from app.models.system import ComplianceState
from app.models.template import MessageTemplate

E164_PHONE_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")
INDIAN_E164_PHONE_REGEX = re.compile(r"^\+91[6-9]\d{9}$")

PROMOTIONAL_KEYWORDS = (
    "% off",
    "discount",
    "flash sale",
    "special offer",
    "limited time offer",
    "promo code",
    "buy one get",
    "coupon",
    "clearance sale",
    "exclusive deal",
    "shop now and save",
)

DISALLOWED_CONSENT_SOURCES = {
    "purchased_list",
    "scraped",
    "third_party_list",
    "bulk_csv_unverified",
    "random_generation",
    "invoice_import_without_whatsapp_optin",
}


@dataclass
class ComplianceVerdict:
    """Result of evaluating an outbound message against Meta and KAP compliance rules."""

    allowed: bool
    reason_code: Optional[str] = None
    reason_detail: Optional[str] = None
    template: Optional[MessageTemplate] = None
    window_open: bool = False


def normalize_e164_phone(phone: str) -> str:
    """Normalize a phone string while keeping strict E.164 leading '+' requirement."""
    if not phone:
        return ""
    cleaned = re.sub(r"[\s\-\(\)\.]", "", str(phone).strip())
    return cleaned


def normalize_indian_e164_phone(raw_phone: str) -> tuple[str, bool]:
    """
    Normalize Indian phone numbers into strict E.164 format (+91XXXXXXXXXX) and validate.
    Handles:
    - 10-digit Indian mobile numbers starting with 6-9 (e.g. '9876543210' -> '+919876543210')
    - 11-digit numbers with leading 0 (e.g. '09876543210' -> '+919876543210')
    - 12-digit numbers with leading 91 (e.g. '919876543210' -> '+919876543210')
    - Numbers with leading 0091 (e.g. '00919876543210' -> '+919876543210')
    - Formatted strings with spaces, hyphens, parentheses, or Excel '.0' suffix
    Returns (normalized_phone, is_valid_indian_e164).
    """
    if raw_phone is None:
        return "", False
    text = str(raw_phone).strip()
    if text.endswith(".0"):
        text = text[:-2]
    # Remove spaces, hyphens, parentheses, dots
    cleaned = re.sub(r"[\s\-\(\)\.]", "", text)
    if not cleaned:
        return "", False

    if re.fullmatch(r"[6-9]\d{9}", cleaned):
        normalized = f"+91{cleaned}"
    elif re.fullmatch(r"0[6-9]\d{9}", cleaned):
        normalized = f"+91{cleaned[1:]}"
    elif re.fullmatch(r"91[6-9]\d{9}", cleaned):
        normalized = f"+{cleaned}"
    elif re.fullmatch(r"0091[6-9]\d{9}", cleaned):
        normalized = f"+91{cleaned[4:]}"
    elif cleaned.startswith("+"):
        normalized = cleaned
    else:
        return cleaned, False

    is_valid = bool(INDIAN_E164_PHONE_REGEX.fullmatch(normalized))
    return normalized, is_valid


def is_valid_e164_phone(phone: str) -> bool:
    """
    Return True if phone number is in valid international E.164 format (+[1-9]...).
    For Indian (+91) numbers, strictly requires +91 followed by 10 digits starting with 6-9.
    """
    normalized = normalize_e164_phone(phone)
    if normalized.startswith("+91"):
        return bool(INDIAN_E164_PHONE_REGEX.fullmatch(normalized))
    return bool(E164_PHONE_REGEX.match(normalized))


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime instance is timezone-aware in UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_24h_service_window_open(
    customer: Customer,
    reference_time: Optional[datetime] = None,
) -> bool:
    """
    Return True if the customer has sent an inbound message within the past 24 hours.
    Free-form messages are ONLY permitted when this 24-hour customer service window is open.
    """
    last_inbound = ensure_utc(customer.last_inbound_message_at)
    if not last_inbound:
        return False
    now = ensure_utc(reference_time) or datetime.now(timezone.utc)
    elapsed = now - last_inbound
    return timedelta(seconds=0) <= elapsed <= timedelta(hours=24)


def contains_promotional_content(text: str) -> bool:
    """Detect promotional/marketing language to prevent misclassifying promotions as utility."""
    if not text:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in PROMOTIONAL_KEYWORDS)


class ComplianceEngine:
    """Centralized policy enforcement gateway for all outbound WhatsApp communications."""

    @staticmethod
    def get_or_create_state(db: Session) -> ComplianceState:
        """Retrieve or initialize the persistent runtime compliance state."""
        state = db.query(ComplianceState).filter(ComplianceState.id == 1).first()
        if not state:
            state = ComplianceState(
                id=1,
                global_kill_switch=settings.GLOBAL_KILL_SWITCH,
                account_quality_status=settings.ACCOUNT_QUALITY_STATUS,
                reason="Initial system state",
                updated_by="system",
            )
            db.add(state)
            db.commit()
            db.refresh(state)
        return state

    @classmethod
    def set_kill_switch(
        cls,
        db: Session,
        enabled: bool,
        actor: str = "admin",
        reason: str = "Manual administrator toggle",
    ) -> ComplianceState:
        """Activate or deactivate the global emergency kill switch."""
        state = cls.get_or_create_state(db)
        state.global_kill_switch = enabled
        state.reason = reason
        state.updated_by = actor
        db.commit()
        db.refresh(state)
        log_structured_event(
            "compliance_kill_switch_updated",
            f"Global emergency kill switch set to {enabled} by {actor}",
            global_kill_switch=enabled,
            actor=actor,
            reason=reason,
        )
        return state

    @classmethod
    def set_account_quality_status(
        cls,
        db: Session,
        quality_status: str,
        actor: str = "system",
        reason: str = "Account quality status updated",
    ) -> ComplianceState:
        """Update the account quality status (GREEN, YELLOW, RED, RESTRICTED, PAUSED)."""
        normalized = quality_status.upper().strip()
        state = cls.get_or_create_state(db)
        state.account_quality_status = normalized
        state.reason = reason
        state.updated_by = actor
        db.commit()
        db.refresh(state)
        log_structured_event(
            "compliance_account_quality_updated",
            f"Account quality status updated to {normalized}",
            account_quality_status=normalized,
            actor=actor,
            reason=reason,
        )
        return state

    @classmethod
    def validate_template(
        cls,
        db: Session,
        template_name: str,
        template_language: str = "en_US",
        expected_category: Optional[str] = None,
        template_params: Optional[list] = None,
    ) -> ComplianceVerdict:
        """
        Validate that a template exists in the registry, is APPROVED, is active,
        is not paused or low-quality, is not misclassified, and receives matching parameters.
        """
        if not template_name:
            return ComplianceVerdict(
                allowed=False,
                reason_code="TEMPLATE_MISSING",
                reason_detail="A valid approved WhatsApp template name is required.",
            )

        template = (
            db.query(MessageTemplate)
            .filter(
                MessageTemplate.name == template_name,
                MessageTemplate.language == template_language,
            )
            .first()
        )
        if not template:
            log_structured_event(
                "template_rejection",
                f"Template '{template_name}' ({template_language}) not found in registry",
                template_name=template_name,
                language=template_language,
                reason="UNKNOWN_TEMPLATE",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="UNKNOWN_TEMPLATE",
                reason_detail=f"Template '{template_name}' ({template_language}) is not registered.",
            )

        if template.approval_status.upper() != "APPROVED":
            log_structured_event(
                "template_rejection",
                f"Template '{template_name}' has status '{template.approval_status}'",
                template_name=template_name,
                approval_status=template.approval_status,
                reason="TEMPLATE_NOT_APPROVED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="TEMPLATE_NOT_APPROVED",
                reason_detail=f"Template '{template_name}' status is '{template.approval_status}' (must be APPROVED).",
            )

        if not template.is_active:
            log_structured_event(
                "template_rejection",
                f"Template '{template_name}' is paused/inactive",
                template_name=template_name,
                reason="TEMPLATE_PAUSED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="TEMPLATE_PAUSED",
                reason_detail=f"Template '{template_name}' is currently paused or inactive.",
            )

        if template.quality_score.upper() in {"RED", "DISABLED"}:
            log_structured_event(
                "template_rejection",
                f"Template '{template_name}' blocked due to quality score '{template.quality_score}'",
                template_name=template_name,
                quality_score=template.quality_score,
                reason="TEMPLATE_QUALITY_RESTRICTED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="TEMPLATE_QUALITY_RESTRICTED",
                reason_detail=f"Template '{template_name}' is paused due to low quality rating ({template.quality_score}).",
            )

        template_cat = template.category.upper()
        if expected_category and expected_category.upper() != "SERVICE":
            if template_cat != expected_category.upper():
                log_structured_event(
                    "template_rejection",
                    f"Template '{template_name}' category '{template_cat}' does not match '{expected_category}'",
                    template_name=template_name,
                    template_category=template_cat,
                    expected_category=expected_category,
                    reason="TEMPLATE_CATEGORY_MISMATCH",
                )
                return ComplianceVerdict(
                    allowed=False,
                    reason_code="TEMPLATE_CATEGORY_MISMATCH",
                    reason_detail=(
                        f"Template '{template_name}' has category '{template_cat}', "
                        f"which does not match requested category '{expected_category.upper()}'."
                    ),
                )

        # Prevent misclassifying promotional messages as UTILITY or AUTHENTICATION
        if template_cat in {"UTILITY", "AUTHENTICATION"} and contains_promotional_content(template.body_text):
            log_structured_event(
                "template_rejection",
                f"Promotional content detected in {template_cat} template '{template_name}'",
                template_name=template_name,
                template_category=template_cat,
                reason="PROMOTIONAL_MISCLASSIFICATION",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="PROMOTIONAL_MISCLASSIFICATION",
                reason_detail=(
                    f"Template '{template_name}' contains promotional content and cannot be sent as {template_cat}."
                ),
            )

        # Validate required template variables count
        expected_vars = template.variable_definitions or []
        provided_vars = template_params or []
        if len(expected_vars) != len(provided_vars):
            log_structured_event(
                "template_rejection",
                f"Template '{template_name}' expected {len(expected_vars)} parameters, got {len(provided_vars)}",
                template_name=template_name,
                expected_count=len(expected_vars),
                provided_count=len(provided_vars),
                reason="TEMPLATE_PARAMETER_COUNT_MISMATCH",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="TEMPLATE_PARAMETER_COUNT_MISMATCH",
                reason_detail=(
                    f"Template '{template_name}' requires {len(expected_vars)} parameters "
                    f"({expected_vars}), but {len(provided_vars)} were provided."
                ),
            )

        return ComplianceVerdict(allowed=True, template=template)

    @classmethod
    def check_customer_consent(
        cls,
        customer: Customer,
        category: str,
        window_open: bool,
        message_type: str,
    ) -> ComplianceVerdict:
        """
        Verify explicit customer consent and opt-out rules.
        - Opted-out customers are ALWAYS blocked.
        - Contacts from disallowed unverified/scraped sources are ALWAYS blocked.
        - Business-initiated / template messages require explicit recorded opt-in for the category.
        - Promotional/Marketing messages ALWAYS require explicit marketing consent even if the 24h window is open.
        """
        normalized_cat = (category or "service").lower().strip()

        if customer.is_anonymized:
            return ComplianceVerdict(
                allowed=False,
                reason_code="CUSTOMER_ANONYMIZED",
                reason_detail="Customer record has been anonymized and cannot receive messages.",
            )

        if customer.opted_out:
            log_structured_event(
                "consent_rejection",
                "Blocked send to opted-out customer",
                customer_id=customer.id,
                phone_number=customer.phone_number,
                category=normalized_cat,
                reason="CUSTOMER_OPTED_OUT",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="CUSTOMER_OPTED_OUT",
                reason_detail="Customer has opted out of WhatsApp communications.",
            )

        opted_out_cats = (customer.opted_out_categories or "").upper().split(",")
        if "ALL" in opted_out_cats or normalized_cat.upper() in opted_out_cats:
            log_structured_event(
                "consent_rejection",
                f"Blocked send to customer opted out of category {normalized_cat}",
                customer_id=customer.id,
                phone_number=customer.phone_number,
                category=normalized_cat,
                reason="CATEGORY_OPTED_OUT",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="CATEGORY_OPTED_OUT",
                reason_detail=f"Customer has opted out of '{normalized_cat}' notifications.",
            )

        # Reject scraped, purchased, or unverified bulk import sources
        if customer.consent_source and customer.consent_source.lower().strip() in DISALLOWED_CONSENT_SOURCES:
            log_structured_event(
                "consent_rejection",
                f"Disallowed consent source '{customer.consent_source}'",
                customer_id=customer.id,
                consent_source=customer.consent_source,
                reason="DISALLOWED_CONSENT_SOURCE",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="DISALLOWED_CONSENT_SOURCE",
                reason_detail=f"Consent source '{customer.consent_source}' is prohibited by WhatsApp policy.",
            )

        # Marketing messages ALWAYS require explicit marketing consent + metadata
        if normalized_cat == "marketing":
            if not (
                customer.marketing_consent
                and customer.consent_timestamp
                and customer.consent_method
                and customer.consent_source
            ):
                log_structured_event(
                    "consent_rejection",
                    "Missing explicit marketing consent or consent audit metadata",
                    customer_id=customer.id,
                    phone_number=customer.phone_number,
                    category="marketing",
                    reason="MISSING_MARKETING_CONSENT",
                )
                return ComplianceVerdict(
                    allowed=False,
                    reason_code="MISSING_MARKETING_CONSENT",
                    reason_detail=(
                        "Customer does not have valid, documented WhatsApp marketing opt-in consent."
                    ),
                )
            return ComplianceVerdict(allowed=True, window_open=window_open)

        # Utility notifications require utility consent + metadata
        if normalized_cat == "utility":
            if not (
                customer.utility_consent
                and customer.consent_timestamp
                and customer.consent_method
                and customer.consent_source
            ):
                log_structured_event(
                    "consent_rejection",
                    "Missing explicit utility notification consent",
                    customer_id=customer.id,
                    phone_number=customer.phone_number,
                    category="utility",
                    reason="MISSING_UTILITY_CONSENT",
                )
                return ComplianceVerdict(
                    allowed=False,
                    reason_code="MISSING_UTILITY_CONSENT",
                    reason_detail=(
                        "Customer does not have documented WhatsApp utility notification opt-in consent."
                    ),
                )
            return ComplianceVerdict(allowed=True, window_open=window_open)

        # Customer service replies (category == "service")
        if normalized_cat == "service":
            if message_type != "template" and not window_open:
                log_structured_event(
                    "consent_rejection",
                    "Free-form service message blocked because 24-hour customer service window is closed",
                    customer_id=customer.id,
                    phone_number=customer.phone_number,
                    reason="SERVICE_WINDOW_CLOSED",
                )
                return ComplianceVerdict(
                    allowed=False,
                    reason_code="SERVICE_WINDOW_CLOSED",
                    reason_detail=(
                        "The 24-hour customer service window is closed. Only approved templates may be sent "
                        "to customers with appropriate opt-in consent."
                    ),
                )
            if message_type == "template" and not window_open:
                # If sending a template when the 24h window is closed, customer must have utility or marketing consent
                if not (customer.utility_consent or customer.marketing_consent):
                    return ComplianceVerdict(
                        allowed=False,
                        reason_code="MISSING_CONSENT_OUTSIDE_WINDOW",
                        reason_detail="Cannot send template outside 24h window without recorded customer opt-in.",
                    )
            return ComplianceVerdict(allowed=True, window_open=window_open)

        return ComplianceVerdict(
            allowed=False,
            reason_code="INVALID_MESSAGE_CATEGORY",
            reason_detail=f"Unsupported or disallowed message category '{category}'.",
        )

    @classmethod
    def evaluate_outbound(
        cls,
        db: Session,
        customer: Optional[Customer],
        recipient_phone: str,
        message_type: str,
        category: str,
        content: Optional[str] = None,
        template_name: Optional[str] = None,
        template_language: str = "en_US",
        template_params: Optional[list] = None,
        reserve_rate_limit: bool = True,
        reference_time: Optional[datetime] = None,
    ) -> ComplianceVerdict:
        """
        Master compliance gate for all outbound messages.
        Checks:
        1. Global emergency kill switch
        2. Account quality status / warnings
        3. E.164 phone number format
        4. Customer existence & opt-out status
        5. 24-hour customer service window enforcement
        6. Explicit category consent & prohibited misclassification check
        7. Approved template validation
        8. Conservative sending rate limits
        """
        state = cls.get_or_create_state(db)

        # 1. Global emergency kill switch
        if settings.GLOBAL_KILL_SWITCH or state.global_kill_switch:
            log_structured_event(
                "compliance_blocked",
                "Outbound message blocked by Global Emergency Kill Switch",
                recipient_phone=recipient_phone,
                reason="GLOBAL_KILL_SWITCH_ACTIVE",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="GLOBAL_KILL_SWITCH_ACTIVE",
                reason_detail="Global emergency kill switch is active. All outbound messaging is halted.",
            )

        # 2. Account status & quality protection
        effective_quality = (
            state.account_quality_status
            if state.account_quality_status != "GREEN"
            else settings.ACCOUNT_QUALITY_STATUS
        ).upper()
        if effective_quality in {"RED", "RESTRICTED", "PAUSED"}:
            log_structured_event(
                "compliance_blocked",
                f"Outbound message blocked due to account quality status '{effective_quality}'",
                recipient_phone=recipient_phone,
                account_quality=effective_quality,
                reason="ACCOUNT_QUALITY_RESTRICTED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="ACCOUNT_QUALITY_RESTRICTED",
                reason_detail=f"Sending paused because account quality status is '{effective_quality}'.",
            )

        normalized_cat = (category or "service").lower().strip()
        if effective_quality == "YELLOW" and normalized_cat == "marketing":
            log_structured_event(
                "compliance_blocked",
                "Marketing message paused while account quality is YELLOW",
                recipient_phone=recipient_phone,
                account_quality=effective_quality,
                reason="ACCOUNT_QUALITY_WARNING_MARKETING_PAUSED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="ACCOUNT_QUALITY_WARNING_MARKETING_PAUSED",
                reason_detail="Marketing sends are automatically paused when account quality is YELLOW.",
            )

        # 3. Validate E.164 phone number
        if not is_valid_e164_phone(recipient_phone):
            log_structured_event(
                "compliance_blocked",
                "Invalid E.164 phone number format",
                recipient_phone=recipient_phone,
                reason="INVALID_PHONE_NUMBER",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="INVALID_PHONE_NUMBER",
                reason_detail="Recipient phone number must be in international E.164 format (e.g. +919876543210).",
            )

        # 4. Customer record check
        if customer is None:
            log_structured_event(
                "compliance_blocked",
                "Cannot send message to unregistered contact without consent record",
                recipient_phone=recipient_phone,
                reason="CUSTOMER_NOT_REGISTERED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="CUSTOMER_NOT_REGISTERED",
                reason_detail="Recipient is not registered in the customer database with verified consent.",
            )

        # 5. Check 24-hour customer service window
        window_open = is_24h_service_window_open(customer, reference_time=reference_time)
        normalized_type = (message_type or "text").lower().strip()

        if normalized_type != "template" and not window_open:
            log_structured_event(
                "compliance_blocked",
                "Free-form message blocked outside 24-hour customer service window",
                customer_id=customer.id,
                recipient_phone=recipient_phone,
                message_type=normalized_type,
                reason="SERVICE_WINDOW_CLOSED",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="SERVICE_WINDOW_CLOSED",
                reason_detail=(
                    "The 24-hour customer service window is closed. Free-form messages are blocked; "
                    "only approved WhatsApp templates may be sent."
                ),
                window_open=False,
            )

        # Prevent sending promotional content disguised as free-form service or utility message
        if normalized_cat in {"service", "utility"} and content and contains_promotional_content(content):
            log_structured_event(
                "compliance_blocked",
                f"Promotional text blocked in '{normalized_cat}' category message",
                customer_id=customer.id,
                recipient_phone=recipient_phone,
                reason="PROMOTIONAL_MISCLASSIFICATION",
            )
            return ComplianceVerdict(
                allowed=False,
                reason_code="PROMOTIONAL_MISCLASSIFICATION",
                reason_detail=f"Promotional content cannot be sent under category '{normalized_cat}'.",
                window_open=window_open,
            )

        # 6. Check explicit consent & opt-out status
        consent_verdict = cls.check_customer_consent(
            customer=customer,
            category=normalized_cat,
            window_open=window_open,
            message_type=normalized_type,
        )
        if not consent_verdict.allowed:
            return consent_verdict

        # 7. If template message, validate against approved template registry
        matched_template: Optional[MessageTemplate] = None
        if normalized_type == "template":
            template_verdict = cls.validate_template(
                db=db,
                template_name=template_name or "",
                template_language=template_language,
                expected_category=normalized_cat,
                template_params=template_params,
            )
            if not template_verdict.allowed:
                return template_verdict
            matched_template = template_verdict.template

        # 8. Check conservative sending limits & rolling 24-hour unique-recipient portfolio limit
        if reserve_rate_limit:
            within_limits, limit_error = outbound_rate_limiter.check_and_reserve(
                db=db,
                recipient_phone=recipient_phone,
            )
            if not within_limits:
                reason_code = (
                    "PORTFOLIO_24H_UNIQUE_RECIPIENT_LIMIT_REACHED"
                    if limit_error and "PORTFOLIO_24H_UNIQUE_RECIPIENT_LIMIT_REACHED" in limit_error
                    else "RATE_LIMIT_EXCEEDED"
                )
                log_structured_event(
                    "compliance_blocked",
                    f"Rate/portfolio limit prevented outbound send: {limit_error}",
                    customer_id=customer.id,
                    recipient_phone=recipient_phone,
                    reason=reason_code,
                )
                return ComplianceVerdict(
                    allowed=False,
                    reason_code=reason_code,
                    reason_detail=limit_error,
                    window_open=window_open,
                )

        return ComplianceVerdict(
            allowed=True,
            reason_code="COMPLIANCE_APPROVED",
            reason_detail="Message passed all WhatsApp Business Platform compliance checks.",
            template=matched_template,
            window_open=window_open,
        )
