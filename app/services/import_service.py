"""
Excel (.xlsx) and CSV (.csv) Customer Import & Campaign Preparation Service.

Implements:
- Pandas + OpenPyXL parsing of .xlsx and .csv files.
- Normalization of Indian phone numbers into strict E.164 format (+91XXXXXXXXXX).
- Phone number validation and duplicate detection (both in-file and database duplicates).
- Strict marketing consent validation requiring explicit consent evidence.
- Exclusion of customers without valid marketing consent.
- Exclusion of all opted-out customers (never overwrites existing opt-outs).
- Persistent storage of imported contacts in PostgreSQL / SQLAlchemy DB.
- Optional creation of a draft Campaign Preview showing eligible recipients.
- NEVER sends messages automatically after Excel/CSV upload.
"""
from datetime import datetime, timezone
import io
from typing import Any, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.core.compliance import DISALLOWED_CONSENT_SOURCES, normalize_indian_e164_phone
from app.core.logging import log_structured_event
from app.core.rate_limiter import outbound_rate_limiter
from app.models.consent import ConsentRecord
from app.models.customer import Customer
from app.models.template import MessageTemplate
from app.schemas.campaign import CampaignCreate
from app.schemas.customer import CustomerImportSummaryResponse, EligibleRecipientItem
from app.services.campaign_service import CampaignService

TRUTHY_VALUES = {"true", "yes", "1", "y", "opted_in", "opted-in", "opt_in", "granted"}
OPT_OUT_TRUTHY_VALUES = {"true", "yes", "1", "y", "stop", "unsubscribed", "opted_out", "opted-out", "opt_out"}


def _clean_cell(val: Any) -> str:
    if val is None or pd.isna(val):
        return ""
    text = str(val).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _find_column(columns_map: dict[str, str], candidates: list[str]) -> Optional[str]:
    for cand in candidates:
        if cand in columns_map:
            return columns_map[cand]
    return None


class CustomerImportService:
    """Service for compliant Excel/CSV customer import and campaign preview preparation."""

    @staticmethod
    def parse_dataframe(filename: str, file_bytes: bytes) -> pd.DataFrame:
        """Read .xlsx (via openpyxl) or .csv into a string-typed pandas DataFrame."""
        if not filename:
            raise ValueError("Uploaded file must have a valid filename (.xlsx or .csv).")
        lower_name = filename.lower().strip()
        if not file_bytes:
            raise ValueError("Uploaded file is empty.")

        try:
            if lower_name.endswith(".xlsx"):
                df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl", dtype=str)
            elif lower_name.endswith(".csv"):
                df = pd.read_csv(io.BytesIO(file_bytes), dtype=str)
            else:
                raise ValueError("Unsupported file format. Only .xlsx and .csv files are permitted.")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Failed to parse uploaded file '{filename}': {exc}") from exc

        df = df.dropna(how="all")
        return df

    @classmethod
    def import_customers_and_prepare_campaign(
        cls,
        db: Session,
        filename: str,
        file_bytes: bytes,
        actor: str = "admin",
        prepare_campaign: bool = False,
        campaign_name: Optional[str] = None,
        campaign_purpose: Optional[str] = None,
        template_id: Optional[int] = None,
        default_template_params: Optional[list[str]] = None,
    ) -> CustomerImportSummaryResponse:
        """
        Parse Excel/CSV file, normalize Indian phone numbers to +91XXXXXXXXXX,
        validate consent evidence & opt-outs, store contacts in the database,
        and optionally generate a campaign preview without ever sending messages automatically.
        """
        df = cls.parse_dataframe(filename, file_bytes)

        # Build case-insensitive normalized column lookup
        columns_map: dict[str, str] = {
            str(col).strip().lower().replace(" ", "_"): str(col)
            for col in df.columns
        }

        phone_col = _find_column(
            columns_map,
            ["phone_number", "phone", "mobile", "mobile_number", "whatsapp", "whatsapp_number", "contact"],
        )
        if not phone_col:
            raise ValueError(
                "Missing required phone number column. Expected one of: phone_number, phone, mobile, whatsapp."
            )

        name_col = _find_column(columns_map, ["name", "customer_name", "full_name", "contact_name"])
        consent_col = _find_column(
            columns_map,
            ["marketing_consent", "consent", "opted_in", "whatsapp_consent", "whatsapp_opt_in"],
        )
        utility_col = _find_column(columns_map, ["utility_consent"])
        method_col = _find_column(
            columns_map,
            ["consent_method", "collection_method", "opt_in_method", "method"],
        )
        source_col = _find_column(
            columns_map,
            ["consent_source", "source", "opt_in_source", "lead_source"],
        )
        evidence_col = _find_column(
            columns_map,
            ["consent_evidence", "evidence", "consent_proof", "proof", "opt_in_evidence"],
        )
        opted_out_col = _find_column(
            columns_map,
            ["opted_out", "opt_out", "is_opted_out", "unsubscribed", "opt_out_status"],
        )

        total_imported = len(df)
        valid_phone_numbers = 0
        invalid_phone_numbers = 0
        duplicate_contacts = 0
        missing_consent = 0
        opted_out_contacts = 0
        eligible_contacts = 0

        seen_phones_in_file: set[str] = set()
        imported_customer_ids: list[int] = []
        eligible_items: list[EligibleRecipientItem] = []
        now = datetime.now(timezone.utc)

        for _, row in df.iterrows():
            raw_phone = _clean_cell(row.get(phone_col))
            normalized_phone, is_valid_phone = normalize_indian_e164_phone(raw_phone)

            if not is_valid_phone:
                invalid_phone_numbers += 1
                continue

            # Detect duplicates within the uploaded file
            if normalized_phone in seen_phones_in_file:
                duplicate_contacts += 1
                continue
            seen_phones_in_file.add(normalized_phone)
            valid_phone_numbers += 1

            customer_name = _clean_cell(row.get(name_col)) if name_col else None
            raw_mkt_consent = _clean_cell(row.get(consent_col)).lower() if consent_col else ""
            raw_util_consent = _clean_cell(row.get(utility_col)).lower() if utility_col else ""
            raw_opted_out = _clean_cell(row.get(opted_out_col)).lower() if opted_out_col else ""

            consent_method = (
                _clean_cell(row.get(method_col)) if method_col else ""
            ) or "verified_spreadsheet_optin"
            consent_source = (
                _clean_cell(row.get(source_col)) if source_col else ""
            ) or "maxis_customer_consent_sheet"
            consent_evidence = _clean_cell(row.get(evidence_col)) if evidence_col else ""

            row_opted_out = raw_opted_out in OPT_OUT_TRUTHY_VALUES
            wants_marketing = raw_mkt_consent in TRUTHY_VALUES
            wants_utility = raw_util_consent in TRUTHY_VALUES

            # Validate marketing consent using explicit consent evidence & allowed source
            has_valid_evidence = len(consent_evidence) >= 3
            is_allowed_source = consent_source.lower().strip() not in DISALLOWED_CONSENT_SOURCES
            valid_marketing_consent = bool(
                wants_marketing
                and has_valid_evidence
                and is_allowed_source
                and not row_opted_out
            )
            valid_utility_consent = bool(
                wants_utility
                and has_valid_evidence
                and is_allowed_source
                and not row_opted_out
            )

            existing_customer = (
                db.query(Customer).filter(Customer.phone_number == normalized_phone).first()
            )

            if existing_customer:
                # Never overwrite an existing opted-out or anonymized customer back to opted-in via file import
                if existing_customer.opted_out or existing_customer.is_anonymized or row_opted_out:
                    existing_customer.opted_out = True
                    existing_customer.opted_out_categories = "ALL"
                    existing_customer.marketing_consent = False
                    existing_customer.utility_consent = False
                    existing_customer.opt_out_timestamp = existing_customer.opt_out_timestamp or now
                    existing_customer.opt_out_reason = (
                        existing_customer.opt_out_reason or "Marked opted-out during Excel/CSV import"
                    )
                    existing_customer.updated_at = now
                    opted_out_contacts += 1
                    imported_customer_ids.append(existing_customer.id)
                    continue

                if customer_name and not existing_customer.name:
                    existing_customer.name = customer_name

                if valid_marketing_consent:
                    existing_customer.marketing_consent = True
                    existing_customer.consent_timestamp = now
                    existing_customer.consent_method = consent_method
                    existing_customer.consent_source = consent_source
                    existing_customer.consent_evidence = consent_evidence
                    db.add(
                        ConsentRecord(
                            customer_id=existing_customer.id,
                            consent_type="marketing",
                            action="opt_in",
                            granted=True,
                            collection_method=consent_method,
                            source=consent_source,
                            evidence=consent_evidence,
                            recorded_by=actor,
                            created_at=now,
                        )
                    )
                else:
                    # Ensure missing consent in import does not grant marketing consent
                    if not (
                        existing_customer.marketing_consent
                        and existing_customer.consent_evidence
                        and existing_customer.consent_timestamp
                    ):
                        existing_customer.marketing_consent = False

                if valid_utility_consent:
                    existing_customer.utility_consent = True

                existing_customer.updated_at = now
                customer_obj = existing_customer
            else:
                customer_obj = Customer(
                    phone_number=normalized_phone,
                    name=customer_name or None,
                    marketing_consent=valid_marketing_consent,
                    utility_consent=valid_utility_consent,
                    consent_timestamp=now if (valid_marketing_consent or valid_utility_consent) else None,
                    consent_method=consent_method if (valid_marketing_consent or valid_utility_consent) else None,
                    consent_source=consent_source if (valid_marketing_consent or valid_utility_consent) else None,
                    consent_evidence=consent_evidence if (valid_marketing_consent or valid_utility_consent) else None,
                    opted_out=row_opted_out,
                    opted_out_categories="ALL" if row_opted_out else None,
                    opt_out_timestamp=now if row_opted_out else None,
                    opt_out_reason="Opted out in uploaded contact file" if row_opted_out else None,
                    conversation_status="new",
                )
                db.add(customer_obj)
                db.flush()

                if row_opted_out:
                    db.add(
                        ConsentRecord(
                            customer_id=customer_obj.id,
                            consent_type="all",
                            action="opt_out",
                            granted=False,
                            collection_method="file_import",
                            source=filename,
                            evidence="Marked opted out in import file",
                            recorded_by=actor,
                            created_at=now,
                        )
                    )
                elif valid_marketing_consent:
                    db.add(
                        ConsentRecord(
                            customer_id=customer_obj.id,
                            consent_type="marketing",
                            action="opt_in",
                            granted=True,
                            collection_method=consent_method,
                            source=consent_source,
                            evidence=consent_evidence,
                            recorded_by=actor,
                            created_at=now,
                        )
                    )

            imported_customer_ids.append(customer_obj.id)

            if customer_obj.opted_out:
                opted_out_contacts += 1
            elif (
                customer_obj.marketing_consent
                and customer_obj.consent_evidence
                and customer_obj.consent_timestamp
            ):
                eligible_contacts += 1
                eligible_items.append(
                    EligibleRecipientItem(
                        customer_id=customer_obj.id,
                        phone_number=customer_obj.phone_number,
                        name=customer_obj.name,
                        marketing_consent=customer_obj.marketing_consent,
                        consent_method=customer_obj.consent_method,
                        consent_source=customer_obj.consent_source,
                        consent_evidence=customer_obj.consent_evidence,
                    )
                )
            else:
                missing_consent += 1

        db.commit()

        portfolio_snapshot = outbound_rate_limiter.get_portfolio_capacity(db)
        remaining_capacity = portfolio_snapshot["remaining_portfolio_capacity"]

        campaign_preview_dict: Optional[dict] = None
        if prepare_campaign or template_id is not None:
            # Find the requested template or default approved marketing template
            tpl: Optional[MessageTemplate] = None
            if template_id is not None:
                tpl = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
                if not tpl:
                    raise ValueError(f"Template ID {template_id} not found.")
            else:
                tpl = (
                    db.query(MessageTemplate)
                    .filter(
                        MessageTemplate.category == "MARKETING",
                        MessageTemplate.approval_status == "APPROVED",
                        MessageTemplate.is_active.is_(True),
                    )
                    .first()
                )
            if tpl:
                default_params = (
                    default_template_params
                    if default_template_params is not None
                    else ["Valued Customer" for _ in (tpl.variable_definitions or [])]
                )
                camp_category = (
                    tpl.category.upper()
                    if tpl.category and tpl.category.upper() in {"MARKETING", "UTILITY"}
                    else "MARKETING"
                )
                camp = CampaignService.create_campaign(
                    db=db,
                    payload=CampaignCreate(
                        name=campaign_name or f"Import Campaign - {filename}",
                        purpose=campaign_purpose or f"Prepared from imported file {filename}",
                        category=camp_category,
                        template_id=tpl.id,
                        default_template_params=default_params,
                        customer_ids=imported_customer_ids,
                    ),
                    actor=actor,
                )
                preview_resp = CampaignService.preview_campaign(db=db, campaign=camp)
                campaign_preview_dict = preview_resp.model_dump()

        log_structured_event(
            "customer_file_imported",
            (
                f"Imported '{filename}': total={total_imported}, valid_phones={valid_phone_numbers}, "
                f"duplicates={duplicate_contacts}, missing_consent={missing_consent}, "
                f"opted_out={opted_out_contacts}, eligible={eligible_contacts}"
            ),
            filename=filename,
            actor=actor,
            total_imported=total_imported,
            valid_phone_numbers=valid_phone_numbers,
            invalid_phone_numbers=invalid_phone_numbers,
            duplicate_contacts=duplicate_contacts,
            missing_consent=missing_consent,
            opted_out_contacts=opted_out_contacts,
            eligible_contacts=eligible_contacts,
        )

        return CustomerImportSummaryResponse(
            filename=filename,
            total_imported=total_imported,
            valid_phone_numbers=valid_phone_numbers,
            invalid_phone_numbers=invalid_phone_numbers,
            duplicate_contacts=duplicate_contacts,
            missing_consent=missing_consent,
            opted_out_contacts=opted_out_contacts,
            eligible_contacts=eligible_contacts,
            eligible_recipients=eligible_items,
            remaining_24h_portfolio_capacity=remaining_capacity,
            messages_sent_automatically=0,
            campaign_preview=campaign_preview_dict,
        )
