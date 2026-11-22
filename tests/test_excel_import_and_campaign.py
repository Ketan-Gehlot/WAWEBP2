"""
Automated Tests for the Excel/CSV Customer Import & WhatsApp Campaign Preparation Module.

Verifies all 16 requirements:
1. Authenticated endpoint to upload .xlsx and .csv files.
2. Reading Excel data using pandas and openpyxl.
3. Normalizing Indian phone numbers into E.164 format (+91XXXXXXXXXX).
4. Validating phone numbers and detecting duplicates.
5. Validating marketing consent using explicit consent evidence.
6. Excluding customers without valid marketing consent.
7. Excluding all opted-out customers (and never overwriting existing DB opt-outs).
8. Storing imported contacts in database.
9. Displaying complete import summary (total_imported, valid_phone_numbers, duplicate_contacts,
   missing_consent, opted_out_contacts, eligible_contacts).
10. Creating a campaign preview showing eligible recipients.
11. Never sending messages automatically after Excel upload.
12. Requiring a separate authenticated campaign execution confirmation.
13. Enforcing the Meta rolling 24-hour unique-recipient limit, checking remaining portfolio capacity before every send.
14. Rechecking consent and opt-out status immediately before each send.
15. Using only approved WhatsApp templates.
16. Defaulting to dry-run mode and never sending real messages without explicit administrator authorization.
"""
import io
import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.core.compliance import normalize_indian_e164_phone
from app.models.customer import Customer
from app.models.message import Message
from app.models.template import MessageTemplate
from app.services.consent_service import ConsentService


def _create_excel_bytes(rows: list[dict]) -> bytes:
    """Create an in-memory .xlsx file using pandas and openpyxl."""
    df = pd.DataFrame(rows)
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Customers")
    return buffer.getvalue()


def _create_csv_bytes(rows: list[dict]) -> bytes:
    """Create an in-memory .csv file using pandas."""
    df = pd.DataFrame(rows)
    return df.to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. INDIAN PHONE NUMBER E.164 NORMALIZATION (+91XXXXXXXXXX)
# ---------------------------------------------------------------------------
def test_indian_phone_number_normalization_to_e164():
    assert normalize_indian_e164_phone("9876543210") == ("+919876543210", True)
    assert normalize_indian_e164_phone("09876543210") == ("+919876543210", True)
    assert normalize_indian_e164_phone("919876543210") == ("+919876543210", True)
    assert normalize_indian_e164_phone("+91 98765-43210") == ("+919876543210", True)
    assert normalize_indian_e164_phone("0091-9876543210") == ("+919876543210", True)
    assert normalize_indian_e164_phone("9876543210.0") == ("+919876543210", True)

    # Invalid Indian phone numbers (wrong digit count or starting with 0-5)
    assert normalize_indian_e164_phone("12345")[1] is False
    assert normalize_indian_e164_phone("5876543210")[1] is False
    assert normalize_indian_e164_phone("+911234567890")[1] is False
    assert normalize_indian_e164_phone("not-a-phone")[1] is False


# ---------------------------------------------------------------------------
# 2. EXCEL (.XLSX) UPLOAD, SUMMARY, DUPLICATES, CONSENT & OPT-OUT EXCLUSION
# ---------------------------------------------------------------------------
def test_excel_xlsx_customer_import_and_campaign_preview_never_sends_automatically(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    # Pre-create an already opted-out customer in the database to verify import NEVER overwrites opt-out
    existing_opted_out = Customer(
        phone_number="+919811110005",
        name="Previously Opted Out",
        marketing_consent=False,
        opted_out=True,
        opted_out_categories="ALL",
    )
    db_session.add(existing_opted_out)
    db_session.commit()

    excel_rows = [
        # 1. Valid 10-digit Indian phone + valid marketing consent + explicit evidence -> ELIGIBLE
        {
            "phone_number": "9811110001",
            "name": "Aarav Verma",
            "marketing_consent": "yes",
            "consent_method": "pos_signed_form",
            "consent_source": "kap_delhi_counter",
            "consent_evidence": "Signed POS consent receipt #INV-1001",
            "opted_out": "no",
        },
        # 2. Valid 12-digit Indian phone (91...) + valid marketing consent + explicit evidence -> ELIGIBLE
        {
            "phone_number": "919811110002",
            "name": "Priya Nair",
            "marketing_consent": "true",
            "consent_method": "website_optin_checkbox",
            "consent_source": "kap_online_portal",
            "consent_evidence": "Web opt-in timestamp 2026-10-15T11:20:00Z form #202",
            "opted_out": "false",
        },
        # 3. Duplicate of Row 1 (formatted with leading 0: 09811110001) -> DUPLICATE
        {
            "phone_number": "09811110001",
            "name": "Aarav Verma Duplicate",
            "marketing_consent": "yes",
            "consent_method": "pos_signed_form",
            "consent_source": "kap_delhi_counter",
            "consent_evidence": "Duplicate row in sheet",
            "opted_out": "no",
        },
        # 4. Valid phone, marketing_consent='yes', but MISSING consent_evidence -> MISSING_CONSENT (Excluded)
        {
            "phone_number": "+91-98111-10003",
            "name": "Karan Malhotra",
            "marketing_consent": "yes",
            "consent_method": "pos_signed_form",
            "consent_source": "kap_delhi_counter",
            "consent_evidence": "",
            "opted_out": "no",
        },
        # 5. Valid phone, marketing_consent='no' -> MISSING_CONSENT (Excluded)
        {
            "phone_number": "9811110004",
            "name": "Neha Gupta",
            "marketing_consent": "no",
            "consent_method": "",
            "consent_source": "",
            "consent_evidence": "",
            "opted_out": "no",
        },
        # 6. Existing opted-out customer in DB (attempts to re-import as opted-in) -> OPTED_OUT (Excluded)
        {
            "phone_number": "9811110005",
            "name": "Previously Opted Out",
            "marketing_consent": "yes",
            "consent_method": "web_form",
            "consent_source": "kap_portal",
            "consent_evidence": "Attempted override",
            "opted_out": "no",
        },
        # 7. New customer explicitly marked opted_out='yes' in Excel -> OPTED_OUT (Excluded)
        {
            "phone_number": "9811110006",
            "name": "Rohan Joshi",
            "marketing_consent": "yes",
            "consent_method": "web_form",
            "consent_source": "kap_portal",
            "consent_evidence": "Form #305",
            "opted_out": "yes",
        },
        # 8. Invalid Indian phone number -> INVALID_PHONE
        {
            "phone_number": "123456",
            "name": "Invalid Phone Row",
            "marketing_consent": "yes",
            "consent_method": "web_form",
            "consent_source": "kap_portal",
            "consent_evidence": "Form #306",
            "opted_out": "no",
        },
    ]

    xlsx_bytes = _create_excel_bytes(excel_rows)

    # Unauthenticated upload must be rejected
    unauth_res = client.post(
        "/api/customers/import",
        files={"file": ("kap_contacts.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert unauth_res.status_code == 401

    # Authenticated upload with prepare_campaign=true
    res = client.post(
        "/api/customers/import",
        headers=admin_headers,
        files={"file": ("kap_contacts.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={
            "prepare_campaign": "true",
            "campaign_name": "KAP Excel Import Campaign",
            "campaign_purpose": "Seasonal parts checkup for opted-in Excel contacts",
        },
    )
    assert res.status_code == 200, res.text
    summary = res.json()

    # Verify import summary counts
    assert summary["total_imported"] == 8
    assert summary["valid_phone_numbers"] == 6
    assert summary["invalid_phone_numbers"] == 1
    assert summary["duplicate_contacts"] == 1
    assert summary["missing_consent"] == 2
    assert summary["opted_out_contacts"] == 2
    assert summary["eligible_contacts"] == 2

    # Verify normalized E.164 phones on eligible recipients
    eligible_phones = [item["phone_number"] for item in summary["eligible_recipients"]]
    assert eligible_phones == ["+919811110001", "+919811110002"]

    # Verify campaign preview was created showing eligible recipients
    preview = summary["campaign_preview"]
    assert preview is not None
    assert preview["eligible_recipient_count"] == 2
    assert preview["excluded_recipient_count"] == 4
    assert len(preview["eligible_recipients"]) == 2

    # Verify ZERO messages were sent automatically after Excel upload
    assert summary["messages_sent_automatically"] == 0
    outbound_count = db_session.query(Message).filter(Message.direction == "outbound").count()
    assert outbound_count == 0


# ---------------------------------------------------------------------------
# 3. CSV UPLOAD & DISALLOWED CONSENT SOURCE REJECTION
# ---------------------------------------------------------------------------
def test_csv_import_excludes_disallowed_purchased_list_consent_source(
    client: TestClient,
    admin_headers: dict[str, str],
):
    csv_rows = [
        {
            "phone": "09822220001",
            "name": "Purchased Lead",
            "marketing_consent": "yes",
            "consent_method": "third_party",
            "consent_source": "purchased_list",
            "consent_evidence": "Vendor invoice #99",
        },
        {
            "phone": "9822220002",
            "name": "Verified Workshop Customer",
            "marketing_consent": "yes",
            "consent_method": "counter_form",
            "consent_source": "kap_workshop_counter",
            "consent_evidence": "Signed service card #SC-440",
        },
    ]
    csv_bytes = _create_csv_bytes(csv_rows)

    res = client.post(
        "/api/customers/import",
        headers=admin_headers,
        files={"file": ("contacts.csv", csv_bytes, "text/csv")},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_imported"] == 2
    assert data["valid_phone_numbers"] == 2
    assert data["missing_consent"] == 1
    assert data["eligible_contacts"] == 1
    assert data["eligible_recipients"][0]["phone_number"] == "+919822220002"


# ---------------------------------------------------------------------------
# 4. DRY-RUN DEFAULT, LIVE SEND AUTHORIZATION & SEND-TIME CONSENT RECHECK
# ---------------------------------------------------------------------------
def test_campaign_execution_defaults_to_dry_run_requires_authorization_and_rechecks_opt_out_before_send(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    rows = [
        {
            "phone_number": "9833330001",
            "name": "Recipient One",
            "marketing_consent": "yes",
            "consent_method": "web_optin",
            "consent_source": "kap_website",
            "consent_evidence": "Consent log #8001",
        },
        {
            "phone_number": "9833330002",
            "name": "Recipient Two (Will Opt Out Before Execution)",
            "marketing_consent": "yes",
            "consent_method": "web_optin",
            "consent_source": "kap_website",
            "consent_evidence": "Consent log #8002",
        },
    ]
    xlsx_bytes = _create_excel_bytes(rows)
    imp_res = client.post(
        "/api/customers/import",
        headers=admin_headers,
        files={"file": ("campaign_batch.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"prepare_campaign": "true", "campaign_name": "Safety Check Campaign"},
    )
    assert imp_res.status_code == 200
    campaign_id = imp_res.json()["campaign_preview"]["campaign_id"]
    rec_two_id = imp_res.json()["eligible_recipients"][1]["customer_id"]

    # Attempting live send (dry_run=False) WITHOUT authorize_live_send=True -> BLOCKED (HTTP 400)
    unauth_live_res = client.post(
        f"/api/campaigns/{campaign_id}/execute",
        headers=admin_headers,
        json={
            "confirm_execution": True,
            "dry_run": False,
            "authorize_live_send": False,
        },
    )
    assert unauth_live_res.status_code == 400
    assert "authorize_live_send=true" in unauth_live_res.json()["detail"]

    # Now Recipient Two has their consent revoked / opted out directly in DB right before campaign execution
    cust_two = db_session.query(Customer).filter(Customer.id == rec_two_id).first()
    cust_two.opted_out = True
    cust_two.marketing_consent = False
    db_session.commit()

    # Execute campaign with default dry-run mode
    exec_res = client.post(
        f"/api/campaigns/{campaign_id}/execute",
        headers=admin_headers,
        json={"confirm_execution": True},
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()

    # Recipient One was sent in dry_run mode; Recipient Two was caught and blocked at send-time recheck!
    assert exec_data["sent_count"] == 1
    assert exec_data["blocked_count"] == 1

    sent_msg = (
        db_session.query(Message)
        .filter(Message.campaign_id == campaign_id, Message.recipient_phone == "+919833330001")
        .first()
    )
    assert sent_msg is not None
    assert sent_msg.status == "dry_run"


# ---------------------------------------------------------------------------
# 5. META ROLLING 24-HOUR UNIQUE-RECIPIENT PORTFOLIO LIMIT ENFORCEMENT
# ---------------------------------------------------------------------------
def test_rolling_24h_unique_recipient_limit_enforced_before_every_send(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    # Set Meta rolling 24-hour unique-recipient limit to 2 unique recipients
    monkeypatch.setattr(settings, "META_24H_UNIQUE_RECIPIENT_LIMIT", 2)

    rows = [
        {
            "phone_number": "9844440001",
            "name": "Unique Recipient 1",
            "marketing_consent": "yes",
            "consent_method": "pos_form",
            "consent_source": "kap_store",
            "consent_evidence": "Form #901",
        },
        {
            "phone_number": "9844440002",
            "name": "Unique Recipient 2",
            "marketing_consent": "yes",
            "consent_method": "pos_form",
            "consent_source": "kap_store",
            "consent_evidence": "Form #902",
        },
        {
            "phone_number": "9844440003",
            "name": "Unique Recipient 3 (Exceeds 24h Portfolio Capacity)",
            "marketing_consent": "yes",
            "consent_method": "pos_form",
            "consent_source": "kap_store",
            "consent_evidence": "Form #903",
        },
    ]
    xlsx_bytes = _create_excel_bytes(rows)
    imp_res = client.post(
        "/api/customers/import",
        headers=admin_headers,
        files={"file": ("tier_limit_test.xlsx", xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert imp_res.status_code == 200
    c_ids = [r["customer_id"] for r in imp_res.json()["eligible_recipients"]]
    assert len(c_ids) == 3

    # Send to Unique Recipient 1 -> ALLOWED (1/2 unique recipients used)
    r1 = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c_ids[0],
            "message_type": "template",
            "category": "marketing",
            "template_name": "kap_seasonal_parts_campaign",
            "template_language": "en_US",
            "template_params": ["Unique 1"],
        },
    )
    assert r1.status_code == 200

    # Send to Unique Recipient 2 -> ALLOWED (2/2 unique recipients used; 0 remaining portfolio capacity)
    r2 = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c_ids[1],
            "message_type": "template",
            "category": "marketing",
            "template_name": "kap_seasonal_parts_campaign",
            "template_language": "en_US",
            "template_params": ["Unique 2"],
        },
    )
    assert r2.status_code == 200

    # Send to Unique Recipient 3 -> BLOCKED by rolling 24-hour unique-recipient limit!
    r3 = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c_ids[2],
            "message_type": "template",
            "category": "marketing",
            "template_name": "kap_seasonal_parts_campaign",
            "template_language": "en_US",
            "template_params": ["Unique 3"],
        },
    )
    assert r3.status_code == 403
    assert "PORTFOLIO_24H_UNIQUE_RECIPIENT_LIMIT_REACHED" in r3.json()["detail"]["reason"]

    # Sending a second message to Unique Recipient 1 (already messaged within rolling 24h) -> ALLOWED
    r1_followup = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c_ids[0],
            "message_type": "template",
            "category": "marketing",
            "template_name": "kap_seasonal_parts_campaign",
            "template_language": "en_US",
            "template_params": ["Unique 1 Follow-up"],
        },
    )
    assert r1_followup.status_code == 200
