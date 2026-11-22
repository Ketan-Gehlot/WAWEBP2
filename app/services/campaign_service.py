"""
Campaign Management Service.

Enforces:
- Mandatory preview and dry-run before execution.
- Strict consent eligibility evaluation (never sends simply because an admin uploaded a list).
- Detailed exclusion breakdown (without consent, opted-out, invalid phone numbers, exceeding restrictions).
- Explicit administrator confirmation before execution.
- Defaults to dry-run mode and NEVER sends real messages without explicit administrator authorization.
- Enforces Meta rolling 24-hour unique-recipient portfolio capacity before every send.
- Rechecks consent and opt-out status immediately before each send.
"""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.orm import Session

from app.config import settings
from app.core.compliance import ComplianceEngine, is_valid_e164_phone, normalize_e164_phone
from app.core.logging import log_structured_event
from app.core.rate_limiter import outbound_rate_limiter
from app.models.campaign import Campaign, CampaignRecipient
from app.models.customer import Customer
from app.models.template import MessageTemplate
from app.schemas.campaign import CampaignCreate, CampaignExecuteRequest, CampaignPreviewResponse
from app.schemas.message import MessageSendRequest
from app.services.message_service import MessageService


class CampaignService:
    """Service managing compliant WhatsApp template campaigns."""

    @staticmethod
    def create_campaign(
        db: Session,
        payload: CampaignCreate,
        actor: str = "admin",
    ) -> Campaign:
        template = db.query(MessageTemplate).filter(MessageTemplate.id == payload.template_id).first()
        if not template:
            raise ValueError(f"Template ID {payload.template_id} not found in template registry.")

        campaign = Campaign(
            name=payload.name,
            purpose=payload.purpose,
            category=payload.category.upper(),
            template_id=template.id,
            default_template_params=payload.default_template_params,
            audience_selection={
                "customer_ids": payload.customer_ids,
                "raw_phone_numbers": payload.raw_phone_numbers,
            },
            scheduled_at=payload.scheduled_at,
            status="draft",
            preview_completed=False,
            admin_confirmed=False,
            created_by=actor,
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign

    @classmethod
    def preview_campaign(
        cls,
        db: Session,
        campaign: Campaign,
    ) -> CampaignPreviewResponse:
        """
        Execute mandatory dry-run preview of a campaign.
        Evaluates all selected contacts and categorizes:
        - Total contacts selected
        - Contacts with valid consent
        - Contacts without consent
        - Opted-out contacts
        - Contacts with invalid phone numbers
        - Contacts exceeding applicable restrictions (including Meta 24h unique-recipient limit)
        - List of eligible recipients
        """
        template = db.query(MessageTemplate).filter(MessageTemplate.id == campaign.template_id).first()
        if not template:
            raise ValueError("Associated message template does not exist.")

        # Validate template eligibility first (must be APPROVED, active, matching category)
        tpl_verdict = ComplianceEngine.validate_template(
            db=db,
            template_name=template.name,
            template_language=template.language,
            expected_category=campaign.category,
            template_params=campaign.default_template_params,
        )

        # Clear any old preview recipient rows if re-previewing a draft/previewed campaign
        db.query(CampaignRecipient).filter(CampaignRecipient.campaign_id == campaign.id).delete()
        db.flush()

        audience = campaign.audience_selection or {}
        customer_ids = audience.get("customer_ids")
        raw_phones = audience.get("raw_phone_numbers") or []

        candidates: list[tuple[Optional[Customer], str]] = []
        seen_phones: set[str] = set()

        if customer_ids is not None:
            for cid in customer_ids:
                cust = db.query(Customer).filter(Customer.id == cid).first()
                if cust:
                    candidates.append((cust, cust.phone_number))
                    seen_phones.add(cust.phone_number)
        elif not raw_phones:
            for cust in db.query(Customer).filter(Customer.is_anonymized.is_(False)).all():
                candidates.append((cust, cust.phone_number))
                seen_phones.add(cust.phone_number)

        for raw_phone in raw_phones:
            norm_phone = normalize_e164_phone(raw_phone)
            if norm_phone in seen_phones:
                continue
            seen_phones.add(norm_phone)
            cust = db.query(Customer).filter(Customer.phone_number == norm_phone).first()
            candidates.append((cust, norm_phone or str(raw_phone)))

        total_selected = len(candidates)
        valid_consent_count = 0
        without_consent_count = 0
        opted_out_count = 0
        invalid_phone_count = 0
        exceeding_restrictions_count = 0
        exclusion_reasons: dict[str, int] = {}
        eligible_recipients_list: list[dict] = []

        def _inc_reason(code: str) -> None:
            exclusion_reasons[code] = exclusion_reasons.get(code, 0) + 1

        portfolio_cap = outbound_rate_limiter.get_portfolio_capacity(db)
        remaining_portfolio_capacity = portfolio_cap["remaining_portfolio_capacity"]
        active_unique_recipients = portfolio_cap["active_unique_recipients"]
        new_unique_allocated = 0

        max_recipients = min(settings.MAX_CAMPAIGN_RECIPIENTS, settings.MAX_DAILY_MESSAGES)

        for customer, phone in candidates:
            is_eligible = False
            reason: Optional[str] = None

            if not is_valid_e164_phone(phone):
                invalid_phone_count += 1
                reason = "INVALID_PHONE_NUMBER"
                _inc_reason(reason)
            elif customer is None or customer.is_anonymized:
                without_consent_count += 1
                reason = "UNREGISTERED_CONTACT_NO_CONSENT"
                _inc_reason(reason)
            elif customer.opted_out or (
                customer.opted_out_categories
                and (
                    "ALL" in customer.opted_out_categories.upper()
                    or campaign.category.upper() in customer.opted_out_categories.upper()
                )
            ):
                opted_out_count += 1
                reason = "CUSTOMER_OPTED_OUT"
                _inc_reason(reason)
            else:
                consent_verdict = ComplianceEngine.check_customer_consent(
                    customer=customer,
                    category=campaign.category.lower(),
                    window_open=False,
                    message_type="template",
                )
                if not consent_verdict.allowed:
                    without_consent_count += 1
                    reason = consent_verdict.reason_code or "MISSING_CONSENT"
                    _inc_reason(reason)
                elif not tpl_verdict.allowed:
                    exceeding_restrictions_count += 1
                    reason = tpl_verdict.reason_code or "TEMPLATE_INVALID"
                    _inc_reason(reason)
                elif valid_consent_count >= max_recipients:
                    exceeding_restrictions_count += 1
                    reason = "EXCEEDS_CAMPAIGN_OR_DAILY_SENDING_LIMIT"
                    _inc_reason(reason)
                elif (
                    phone not in active_unique_recipients
                    and new_unique_allocated >= remaining_portfolio_capacity
                ):
                    exceeding_restrictions_count += 1
                    reason = "PORTFOLIO_24H_UNIQUE_RECIPIENT_LIMIT_REACHED"
                    _inc_reason(reason)
                else:
                    is_eligible = True
                    valid_consent_count += 1
                    if phone not in active_unique_recipients:
                        new_unique_allocated += 1
                    eligible_recipients_list.append(
                        {
                            "customer_id": customer.id,
                            "phone_number": customer.phone_number,
                            "name": customer.name,
                            "marketing_consent": customer.marketing_consent,
                            "utility_consent": customer.utility_consent,
                            "consent_source": customer.consent_source,
                            "consent_evidence": customer.consent_evidence,
                        }
                    )

            db.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    customer_id=customer.id if customer else None,
                    phone_number=phone,
                    template_params=campaign.default_template_params,
                    is_eligible=is_eligible,
                    exclusion_reason=reason,
                    delivery_status="pending" if is_eligible else "blocked_compliance",
                )
            )

        now = datetime.now(timezone.utc)
        campaign.total_contacts_selected = total_selected
        campaign.eligible_recipient_count = valid_consent_count
        campaign.excluded_recipient_count = total_selected - valid_consent_count
        campaign.exclusion_reasons = exclusion_reasons
        campaign.preview_completed = True
        campaign.previewed_at = now
        if campaign.status == "draft":
            campaign.status = "previewed"

        db.commit()
        db.refresh(campaign)

        log_structured_event(
            "campaign_previewed",
            f"Campaign {campaign.id} preview completed: {valid_consent_count}/{total_selected} eligible",
            campaign_id=campaign.id,
            total_selected=total_selected,
            eligible=valid_consent_count,
            excluded=total_selected - valid_consent_count,
            exclusion_reasons=exclusion_reasons,
            remaining_24h_portfolio_capacity=remaining_portfolio_capacity,
        )

        return CampaignPreviewResponse(
            campaign_id=campaign.id,
            campaign_name=campaign.name,
            category=campaign.category,
            template_name=template.name,
            template_approval_status=template.approval_status,
            preview_completed=True,
            total_contacts_selected=total_selected,
            contacts_with_valid_consent=valid_consent_count,
            contacts_without_consent=without_consent_count,
            opted_out_contacts=opted_out_count,
            contacts_with_invalid_phone_numbers=invalid_phone_count,
            contacts_exceeding_restrictions=exceeding_restrictions_count,
            eligible_recipient_count=valid_consent_count,
            excluded_recipient_count=total_selected - valid_consent_count,
            exclusion_reasons=exclusion_reasons,
            eligible_recipients=eligible_recipients_list,
            remaining_24h_portfolio_capacity=remaining_portfolio_capacity,
            requires_admin_confirmation=True,
        )

    @classmethod
    def execute_campaign(
        cls,
        db: Session,
        campaign: Campaign,
        request: CampaignExecuteRequest,
        actor: str = "admin",
    ) -> Campaign:
        """
        Execute a previewed campaign after explicit administrator confirmation.
        - Defaults to dry-run mode (dry_run=True).
        - Never sends live messages unless dry_run=False AND authorize_live_send=True.
        - Rechecks consent, opt-out status, approved template status, and remaining Meta
          rolling 24-hour unique-recipient portfolio capacity immediately before EACH send.
        """
        if not campaign.preview_completed:
            raise ValueError(
                "Campaign cannot be executed before running mandatory preview/dry-run (/preview)."
            )
        if not request.confirm_execution:
            raise ValueError(
                "Explicit administrator confirmation (confirm_execution=true) is required to execute a campaign."
            )

        # Determine effective dry-run mode (default is True)
        requested_dry_run = (
            request.dry_run_override
            if request.dry_run_override is not None
            else request.dry_run
        )
        if requested_dry_run is False and not request.authorize_live_send:
            raise ValueError(
                "Live campaign sending requires explicit administrator authorization "
                "(set authorize_live_send=true when dry_run=false)."
            )
        effective_dry_run = bool(requested_dry_run)

        if campaign.status == "executing":
            raise ValueError("Campaign is currently executing.")
        if campaign.status == "completed" and campaign.sent_count > 0:
            raise ValueError("Campaign has already been completed and sent.")

        # Re-evaluate recipient eligibility right before execution so any newly granted consent
        # or recent opt-out is accurately reflected
        cls.preview_campaign(db=db, campaign=campaign)

        template = db.query(MessageTemplate).filter(MessageTemplate.id == campaign.template_id).first()
        if not template:
            raise ValueError("Campaign template not found.")

        # Revalidate template approval immediately before execution starts
        tpl_verdict = ComplianceEngine.validate_template(
            db=db,
            template_name=template.name,
            template_language=template.language,
            expected_category=campaign.category,
            template_params=campaign.default_template_params,
        )
        if not tpl_verdict.allowed:
            raise ValueError(
                f"Campaign template failed compliance validation: {tpl_verdict.reason_code} - {tpl_verdict.reason_detail}"
            )

        now = datetime.now(timezone.utc)
        campaign.admin_confirmed = True
        campaign.confirmed_by = actor
        campaign.confirmed_at = now
        campaign.status = "executing"
        db.commit()

        eligible_recipients = (
            db.query(CampaignRecipient)
            .filter(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.is_eligible.is_(True),
                CampaignRecipient.delivery_status == "pending",
            )
            .all()
        )

        sent_count = 0
        failed_count = 0
        blocked_count = campaign.excluded_recipient_count

        for recipient in eligible_recipients:
            # 1. Recheck customer consent and opt-out status immediately before each send
            fresh_customer = (
                db.query(Customer).filter(Customer.id == recipient.customer_id).first()
                if recipient.customer_id
                else db.query(Customer).filter(Customer.phone_number == recipient.phone_number).first()
            )
            if fresh_customer is not None:
                db.refresh(fresh_customer)

            if fresh_customer is None or fresh_customer.opted_out:
                recipient.is_eligible = False
                recipient.delivery_status = "cancelled_opt_out"
                recipient.exclusion_reason = "CUSTOMER_OPTED_OUT: Customer opted out prior to send."
                recipient.processed_at = datetime.now(timezone.utc)
                blocked_count += 1
                db.commit()
                continue

            consent_recheck = ComplianceEngine.check_customer_consent(
                customer=fresh_customer,
                category=campaign.category.lower(),
                window_open=False,
                message_type="template",
            )
            if not consent_recheck.allowed:
                recipient.is_eligible = False
                recipient.delivery_status = "blocked_compliance"
                recipient.exclusion_reason = (
                    f"{consent_recheck.reason_code}: {consent_recheck.reason_detail}"
                )
                recipient.processed_at = datetime.now(timezone.utc)
                blocked_count += 1
                db.commit()
                continue

            # 2. Check remaining Meta rolling 24-hour unique-recipient portfolio capacity before every send
            cap_ok, _, cap_error = outbound_rate_limiter.check_rolling_24h_unique_recipient_capacity(
                db=db,
                recipient_phone=recipient.phone_number,
            )
            if not cap_ok:
                recipient.is_eligible = False
                recipient.delivery_status = "blocked_compliance"
                recipient.exclusion_reason = cap_error
                recipient.processed_at = datetime.now(timezone.utc)
                blocked_count += 1
                db.commit()
                continue

            # 3. Dispatch through MessageService -> ComplianceEngine
            msg_request = MessageSendRequest(
                customer_id=fresh_customer.id,
                recipient_phone=recipient.phone_number,
                message_type="template",
                category=campaign.category.lower(),
                template_name=template.name,
                template_language=template.language,
                template_params=recipient.template_params or campaign.default_template_params,
                scheduled_at=campaign.scheduled_at,
            )

            msg = MessageService.send_or_schedule_message(
                db=db,
                request=msg_request,
                campaign_id=campaign.id,
                force_dry_run=effective_dry_run,
            )
            recipient.message_id = msg.id
            recipient.delivery_status = msg.status
            recipient.processed_at = datetime.now(timezone.utc)

            if msg.status in {"sent", "dry_run", "scheduled"}:
                sent_count += 1
            elif msg.status in {"blocked_compliance", "cancelled_opt_out"}:
                blocked_count += 1
                recipient.is_eligible = False
                recipient.exclusion_reason = msg.compliance_rejection_reason
            else:
                failed_count += 1

        db.commit()

        blocked_count = (
            db.query(CampaignRecipient)
            .filter(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.delivery_status.in_(("blocked_compliance", "cancelled_opt_out")),
            )
            .count()
        )

        campaign.sent_count = sent_count
        campaign.failed_count = failed_count
        campaign.blocked_count = blocked_count
        campaign.status = (
            "scheduled"
            if (campaign.scheduled_at and campaign.scheduled_at > now and sent_count > 0)
            else "completed"
        )
        campaign.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(campaign)

        log_structured_event(
            "campaign_executed",
            (
                f"Campaign {campaign.id} executed by {actor} (dry_run={effective_dry_run}): "
                f"sent={sent_count}, failed={failed_count}, blocked={blocked_count}"
            ),
            campaign_id=campaign.id,
            actor=actor,
            dry_run=effective_dry_run,
            sent_count=sent_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
        )
        return campaign
