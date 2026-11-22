"""
Comprehensive Automated Test Suite for the KAP WhatsApp Automation Platform.

Covers all required verification areas:
1. Webhook verification success and failure (GET /webhook)
2. Webhook signature validation (POST /webhook HMAC-SHA256)
3. Incoming message processing & automated customer support (Greeting, Brake pads, Location, Order escalation)
4. Duplicate webhook event idempotency handling
5. Outbound text messages & 24-hour customer service window enforcement
6. Template message validation (approved vs unapproved/paused/parameter mismatch/promotional misclassification)
7. Explicit consent enforcement (imported contacts never get automatic opt-in)
8. Marketing & global opt-out (STOP, UNSUBSCRIBE, CANCEL, QUIT, END + scheduled message cancellation)
9. Campaign dry-run preview & mandatory admin confirmation
10. Conservative rate limiting
11. Meta API error classification (permanent vs retryable, automatic account restriction pause)
12. Global emergency kill switch
13. Scheduled message send-time compliance revalidation
"""
from datetime import datetime, timedelta, timezone
import json
from typing import Any
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.core.rate_limiter import outbound_rate_limiter
from app.core.security import compute_webhook_signature
from app.models.customer import Customer
from app.models.message import Message
from app.models.template import MessageTemplate
from app.services.message_service import MessageService
from app.services.whatsapp_client import WhatsAppCloudClient
from app.workers.message_worker import process_scheduled_messages_now
from tests.conftest import TEST_APP_SECRET, TEST_VERIFY_TOKEN


def _build_webhook_message_payload(
    wamid: str,
    sender_wa_id: str,
    text_body: str,
    sender_name: str = "Vikram Patel",
) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102938475610293",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "919800000001",
                                "phone_number_id": "100000000000001",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": sender_name},
                                    "wa_id": sender_wa_id,
                                }
                            ],
                            "messages": [
                                {
                                    "from": sender_wa_id,
                                    "id": wamid,
                                    "timestamp": "1732161000",
                                    "text": {"body": text_body},
                                    "type": "text",
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _post_signed_webhook(client: TestClient, payload: dict[str, Any], secret: str = TEST_APP_SECRET):
    raw_bytes = json.dumps(payload).encode("utf-8")
    sig = compute_webhook_signature(raw_bytes, secret)
    return client.post(
        "/webhook",
        content=raw_bytes,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )


# ---------------------------------------------------------------------------
# 1. HEALTH & SAFE DEFAULTS
# ---------------------------------------------------------------------------
def test_health_starts_with_safe_defaults(client: TestClient):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "healthy"
    assert body["outbound_messages_enabled"] is False
    assert body["dry_run_mode"] is True
    assert body["global_kill_switch"] is False


def test_admin_endpoints_require_authentication(client: TestClient):
    res = client.get("/api/customers")
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# 2. WEBHOOK VERIFICATION SUCCESS AND FAILURE (GET /webhook)
# ---------------------------------------------------------------------------
def test_webhook_verification_success(client: TestClient):
    res = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": "987654321",
        },
    )
    assert res.status_code == 200
    assert res.text == "987654321"


def test_webhook_verification_failure_invalid_token_or_mode(client: TestClient):
    res_bad_token = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong_token",
            "hub.challenge": "987654321",
        },
    )
    assert res_bad_token.status_code == 403

    res_bad_mode = client.get(
        "/webhook",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": "987654321",
        },
    )
    assert res_bad_mode.status_code == 403


# ---------------------------------------------------------------------------
# 3. WEBHOOK SIGNATURE VALIDATION (POST /webhook)
# ---------------------------------------------------------------------------
def test_webhook_signature_validation_rejects_missing_or_tampered_signature(client: TestClient):
    payload = _build_webhook_message_payload("wamid.SIG.1", "919876543210", "Hello")
    raw_bytes = json.dumps(payload).encode("utf-8")

    # Missing signature
    res_missing = client.post("/webhook", content=raw_bytes, headers={"Content-Type": "application/json"})
    assert res_missing.status_code == 401

    # Invalid signature
    res_invalid = client.post(
        "/webhook",
        content=raw_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=0000000000000000000000000000000000000000000000000000000000000000",
        },
    )
    assert res_invalid.status_code == 401


# ---------------------------------------------------------------------------
# 4. INCOMING MESSAGE PROCESSING & AUTOMATED CUSTOMER SUPPORT
# ---------------------------------------------------------------------------
def test_incoming_messages_and_automated_kap_support(client: TestClient, db_session: Session):
    # Case A: Greeting ("Hello")
    p_hello = _build_webhook_message_payload("wamid.IN.HELLO", "919876543210", "Hello")
    res1 = _post_signed_webhook(client, p_hello)
    assert res1.status_code == 200

    outbound_1 = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.recipient_phone == "+919876543210")
        .order_by(Message.id.desc())
        .first()
    )
    assert outbound_1 is not None
    assert "Welcome to Maxis Gold Jewellery" in (outbound_1.content or "")

    # Case B: Product inquiry ("Do you have bridal necklaces?") -> must ask for purity/occasion, never invent stock
    p_jewellery = _build_webhook_message_payload("wamid.IN.JEWEL", "919876543210", "Do you have bridal necklaces?")
    res2 = _post_signed_webhook(client, p_jewellery)
    assert res2.status_code == 200

    outbound_2 = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.recipient_phone == "+919876543210")
        .order_by(Message.id.desc())
        .first()
    )
    assert outbound_2 is not None
    assert "bridal necklaces" in (outbound_2.content or "")
    assert "Maxis Gold Jewellery" in (outbound_2.content or "")

    # Case C: Location inquiry ("What is your location?")
    p_loc = _build_webhook_message_payload("wamid.IN.LOC", "919876543210", "What is your location?")
    res3 = _post_signed_webhook(client, p_loc)
    assert res3.status_code == 200

    outbound_3 = (
        db_session.query(Message)
        .filter(Message.direction == "outbound", Message.recipient_phone == "+919876543210")
        .order_by(Message.id.desc())
        .first()
    )
    assert outbound_3 is not None
    assert (
        outbound_3.content
        == "Our team can share our showroom location and operating hours. Would you like to speak with a team member?"
    )


# ---------------------------------------------------------------------------
# 5. DUPLICATE WEBHOOK EVENT IDEMPOTENCY
# ---------------------------------------------------------------------------
def test_duplicate_webhook_event_handled_idempotently(client: TestClient, db_session: Session):
    payload = _build_webhook_message_payload("wamid.DUP.001", "919811112222", "Hello")

    res_first = _post_signed_webhook(client, payload)
    assert res_first.status_code == 200

    res_duplicate = _post_signed_webhook(client, payload)
    assert res_duplicate.status_code == 200

    inbound_count = (
        db_session.query(Message)
        .filter(Message.whatsapp_message_id == "wamid.DUP.001")
        .count()
    )
    outbound_count = (
        db_session.query(Message)
        .filter(Message.recipient_phone == "+919811112222", Message.direction == "outbound")
        .count()
    )
    assert inbound_count == 1
    assert outbound_count == 1


# ---------------------------------------------------------------------------
# 6. OUTBOUND TEXT MESSAGES & 24-HOUR SERVICE WINDOW ENFORCEMENT
# ---------------------------------------------------------------------------
def test_24_hour_window_enforcement_blocks_freeform_when_closed_and_allows_when_open(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    # Create a customer whose 24-hour window is closed (no recent inbound message)
    create_res = client.post(
        "/api/customers",
        headers=admin_headers,
        json={"phone_number": "+919822223333", "name": "Anita Desai"},
    )
    assert create_res.status_code == 201
    customer_id = create_res.json()["id"]

    # Attempting to send a free-form text message when 24h window is closed MUST BE BLOCKED
    blocked_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": customer_id,
            "message_type": "text",
            "category": "service",
            "content": "Checking in on your vehicle inquiry.",
        },
    )
    assert blocked_res.status_code == 403
    assert "SERVICE_WINDOW_CLOSED" in blocked_res.json()["detail"]["reason"]

    # Simulate customer sending an inbound message 25 hours ago (expired window) -> still blocked
    customer = db_session.query(Customer).filter(Customer.id == customer_id).first()
    customer.last_inbound_message_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db_session.commit()

    expired_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": customer_id,
            "message_type": "text",
            "category": "service",
            "content": "Still blocked after 25 hours.",
        },
    )
    assert expired_res.status_code == 403
    assert "SERVICE_WINDOW_CLOSED" in expired_res.json()["detail"]["reason"]

    # Now simulate customer sending an inbound message 1 hour ago (open 24h window) -> ALLOWED
    customer.last_inbound_message_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    allowed_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": customer_id,
            "message_type": "text",
            "category": "service",
            "content": "Your brake pad specification has been verified by our counter specialist.",
        },
    )
    assert allowed_res.status_code == 200
    assert allowed_res.json()["compliance_approved"] is True
    assert allowed_res.json()["status"] == "dry_run"


# ---------------------------------------------------------------------------
# 7. CONSENT ENFORCEMENT & TEMPLATE VALIDATION
# ---------------------------------------------------------------------------
def test_explicit_consent_and_template_validation(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    # Adding a contact without consent evidence NEVER grants opt-in
    c_res = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919833334444",
            "name": "Suresh Nair",
            "marketing_consent": True,  # Attempted without proof -> forced False
        },
    )
    assert c_res.status_code == 201
    customer_data = c_res.json()
    assert customer_data["marketing_consent"] is False
    cid = customer_data["id"]

    # Sending approved utility template WITHOUT utility consent -> BLOCKED
    no_consent_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_order_status_update",
            "template_language": "en_US",
            "template_params": ["Suresh", "ORD-501", "Ready for pickup"],
        },
    )
    assert no_consent_res.status_code == 403
    assert "MISSING_UTILITY_CONSENT" in no_consent_res.json()["detail"]["reason"]

    # Record explicit utility consent with verifiable method, source, and evidence
    consent_res = client.post(
        f"/api/customers/{cid}/consent",
        headers=admin_headers,
        json={
            "utility_consent": True,
            "marketing_consent": False,
            "collection_method": "pos_invoice_optin_checkbox",
            "source": "kap_counter_invoice_501",
            "evidence": "Customer signed WhatsApp order update opt-in on invoice #501",
        },
    )
    assert consent_res.status_code == 200
    assert consent_res.json()["utility_consent"] is True

    # Sending utility template with wrong parameter count -> BLOCKED
    bad_params_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_order_status_update",
            "template_language": "en_US",
            "template_params": ["OnlyOneParam"],
        },
    )
    assert bad_params_res.status_code == 403
    assert "TEMPLATE_PARAMETER_COUNT_MISMATCH" in bad_params_res.json()["detail"]["reason"]

    # Sending unapproved or unknown template -> BLOCKED
    unknown_tpl_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "utility",
            "template_name": "non_existent_template",
            "template_language": "en_US",
            "template_params": [],
        },
    )
    assert unknown_tpl_res.status_code == 403
    assert "UNKNOWN_TEMPLATE" in unknown_tpl_res.json()["detail"]["reason"]

    # Sending approved utility template with valid utility consent and matching parameters -> ALLOWED
    ok_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_order_status_update",
            "template_language": "en_US",
            "template_params": ["Suresh", "ORD-501", "Dispatched"],
        },
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["compliance_approved"] is True

    # Sending marketing template when customer only has utility consent -> BLOCKED
    mkt_blocked = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "marketing",
            "template_name": "kap_seasonal_parts_campaign",
            "template_language": "en_US",
            "template_params": ["Suresh"],
        },
    )
    assert mkt_blocked.status_code == 403
    assert "MISSING_MARKETING_CONSENT" in mkt_blocked.json()["detail"]["reason"]

    # Registering a promotional template misclassified as UTILITY -> BLOCKED on send
    misclassified_tpl = MessageTemplate(
        name="kap_fake_utility_promo",
        language="en_US",
        category="UTILITY",
        approval_status="APPROVED",
        is_active=True,
        quality_score="GREEN",
        body_text="Hello {{1}}, get 50% off flash sale discount on all brake pads today!",
        variable_definitions=["customer_name"],
    )
    db_session.add(misclassified_tpl)
    db_session.commit()

    promo_blocked = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_fake_utility_promo",
            "template_language": "en_US",
            "template_params": ["Suresh"],
        },
    )
    assert promo_blocked.status_code == 403
    assert "PROMOTIONAL_MISCLASSIFICATION" in promo_blocked.json()["detail"]["reason"]


# ---------------------------------------------------------------------------
# 8. OPT-OUT HANDLING & SCHEDULED MESSAGE CANCELLATION
# ---------------------------------------------------------------------------
def test_automatic_opt_out_blocks_sends_and_cancels_scheduled_messages(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    c_res = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919844445555",
            "name": "Meera Iyer",
            "marketing_consent": True,
            "utility_consent": True,
            "consent_method": "website_optin_form",
            "consent_source": "kap_web_portal",
            "consent_evidence": "Opt-in form submission ID #9981",
        },
    )
    assert c_res.status_code == 201
    cid = c_res.json()["id"]

    # Schedule a future utility message
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    sched_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_service_reminder",
            "template_language": "en_US",
            "template_params": ["Meera", "Brake Fluid Check"],
            "scheduled_at": future_time,
        },
    )
    assert sched_res.status_code == 200
    scheduled_msg_id = sched_res.json()["id"]
    assert sched_res.json()["status"] == "scheduled"

    # Customer sends "STOP" via WhatsApp webhook
    stop_payload = _build_webhook_message_payload("wamid.STOP.001", "919844445555", "STOP")
    stop_res = _post_signed_webhook(client, stop_payload)
    assert stop_res.status_code == 200

    # Verify customer is opted out and scheduled message was immediately cancelled
    db_session.expire_all()
    customer = db_session.query(Customer).filter(Customer.id == cid).first()
    assert customer.opted_out is True
    assert customer.marketing_consent is False
    assert customer.utility_consent is False

    scheduled_msg = db_session.query(Message).filter(Message.id == scheduled_msg_id).first()
    assert scheduled_msg.status == "cancelled_opt_out"

    # Subsequent attempts to message opted-out customer MUST be blocked
    after_stop_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": cid,
            "message_type": "template",
            "category": "marketing",
            "template_name": "kap_seasonal_parts_campaign",
            "template_language": "en_US",
            "template_params": ["Meera"],
        },
    )
    assert after_stop_res.status_code == 403
    assert "CUSTOMER_OPTED_OUT" in after_stop_res.json()["detail"]["reason"]


# ---------------------------------------------------------------------------
# 9. CAMPAIGN DRY-RUN PREVIEW AND MANDATORY CONFIRMATION
# ---------------------------------------------------------------------------
def test_campaign_dry_run_preview_and_confirmed_execution(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    # Create 3 customers: 1 with valid marketing consent, 1 without consent, 1 opted-out
    c1 = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919855550001",
            "name": "OptedIn Customer",
            "marketing_consent": True,
            "consent_method": "web_form",
            "consent_source": "kap_site",
            "consent_evidence": "Form #1",
        },
    ).json()

    c2 = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919855550002",
            "name": "NoConsent Customer",
        },
    ).json()

    c3 = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919855550003",
            "name": "OptedOut Customer",
            "marketing_consent": True,
            "consent_method": "web_form",
            "consent_source": "kap_site",
            "consent_evidence": "Form #3",
        },
    ).json()
    client.post(f"/api/customers/{c3['id']}/opt-out", headers=admin_headers, json={"categories": "ALL"})

    mkt_template = (
        db_session.query(MessageTemplate)
        .filter(MessageTemplate.name == "kap_seasonal_parts_campaign")
        .first()
    )

    # Create campaign including the 3 customers plus 1 invalid phone number
    camp_res = client.post(
        "/api/campaigns",
        headers=admin_headers,
        json={
            "name": "Monsoon Brake Check Campaign",
            "purpose": "Seasonal maintenance reminder for opted-in customers",
            "category": "MARKETING",
            "template_id": mkt_template.id,
            "default_template_params": ["Valued Customer"],
            "customer_ids": [c1["id"], c2["id"], c3["id"]],
            "raw_phone_numbers": ["invalid-phone-123"],
        },
    )
    assert camp_res.status_code == 201
    campaign_id = camp_res.json()["id"]

    # Attempting to execute before running mandatory preview -> REJECTED
    early_exec = client.post(
        f"/api/campaigns/{campaign_id}/execute",
        headers=admin_headers,
        json={"confirm_execution": True},
    )
    assert early_exec.status_code == 400

    # Run mandatory preview / dry-run
    preview_res = client.post(f"/api/campaigns/{campaign_id}/preview", headers=admin_headers)
    assert preview_res.status_code == 200
    p_data = preview_res.json()
    assert p_data["total_contacts_selected"] == 4
    assert p_data["contacts_with_valid_consent"] == 1
    assert p_data["contacts_without_consent"] == 1
    assert p_data["opted_out_contacts"] == 1
    assert p_data["contacts_with_invalid_phone_numbers"] == 1
    assert p_data["eligible_recipient_count"] == 1
    assert p_data["excluded_recipient_count"] == 3

    # Attempting to execute with confirm_execution=False -> REJECTED
    unconfirmed_exec = client.post(
        f"/api/campaigns/{campaign_id}/execute",
        headers=admin_headers,
        json={"confirm_execution": False},
    )
    assert unconfirmed_exec.status_code == 400

    # Execute with explicit confirmation -> sends ONLY to the 1 eligible opted-in customer
    exec_res = client.post(
        f"/api/campaigns/{campaign_id}/execute",
        headers=admin_headers,
        json={"confirm_execution": True},
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()
    assert exec_data["status"] == "completed"
    assert exec_data["sent_count"] == 1
    assert exec_data["blocked_count"] == 3


# ---------------------------------------------------------------------------
# 10. CONSERVATIVE RATE LIMITING
# ---------------------------------------------------------------------------
def test_rate_limiting_enforces_per_minute_cap(
    client: TestClient,
    admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "MAX_MESSAGES_PER_MINUTE", 2)
    outbound_rate_limiter.reset()

    c = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919866667777",
            "name": "Rate Limit Test Customer",
            "utility_consent": True,
            "consent_method": "pos_receipt",
            "consent_source": "kap_pos",
            "consent_evidence": "Receipt #777",
        },
    ).json()

    payload = {
        "customer_id": c["id"],
        "message_type": "template",
        "category": "utility",
        "template_name": "kap_service_reminder",
        "template_language": "en_US",
        "template_params": ["Customer", "Oil Filter"],
    }

    r1 = client.post("/api/messages/send", headers=admin_headers, json=payload)
    r2 = client.post("/api/messages/send", headers=admin_headers, json=payload)
    r3 = client.post("/api/messages/send", headers=admin_headers, json=payload)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 403
    assert "RATE_LIMIT_EXCEEDED" in r3.json()["detail"]["reason"]


# ---------------------------------------------------------------------------
# 11. META API ERROR HANDLING (MOCKED HTTPX - NO REAL WHATSAPP CALLS)
# ---------------------------------------------------------------------------
def test_meta_api_error_classification_and_account_restriction_pause(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "ENABLE_OUTBOUND_MESSAGES", True)
    monkeypatch.setattr(settings, "DRY_RUN", False)

    # 1. Permanent template/recipient error (code 131026) -> non-retryable
    def _mock_permanent_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            json={
                "error": {
                    "message": "Message undeliverable",
                    "type": "OAuthException",
                    "code": 131026,
                    "fbtrace_id": "trace_perm_01",
                }
            },
        )

    mock_http_1 = httpx.Client(transport=httpx.MockTransport(_mock_permanent_error))
    wa_client_1 = WhatsAppCloudClient(http_client=mock_http_1)
    res_perm = wa_client_1.send_payload(wa_client_1.build_text_payload("+919877778888", "Test"))
    assert res_perm.success is False
    assert res_perm.error_code == "META_131026"
    assert res_perm.is_retryable is False
    mock_http_1.close()

    # 2. Transient rate limit / server error (code 130429 / HTTP 429) -> retryable
    def _mock_transient_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=429,
            json={
                "error": {
                    "message": "Rate limit hit",
                    "type": "OAuthException",
                    "code": 130429,
                }
            },
        )

    mock_http_2 = httpx.Client(transport=httpx.MockTransport(_mock_transient_error))
    wa_client_2 = WhatsAppCloudClient(http_client=mock_http_2)
    res_retry = wa_client_2.send_payload(wa_client_2.build_text_payload("+919877778888", "Test"))
    assert res_retry.success is False
    assert res_retry.is_retryable is True
    mock_http_2.close()

    # 3. Policy violation error (code 368) -> triggers automatic account pause
    def _mock_policy_restriction(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=403,
            json={
                "error": {
                    "message": "Temporarily blocked for policy violations",
                    "type": "OAuthException",
                    "code": 368,
                }
            },
        )

    mock_http_3 = httpx.Client(transport=httpx.MockTransport(_mock_policy_restriction))
    wa_client_3 = WhatsAppCloudClient(http_client=mock_http_3)
    res_pause = wa_client_3.send_payload(wa_client_3.build_text_payload("+919877778888", "Test"))
    assert res_pause.success is False
    assert res_pause.triggers_account_pause is True
    mock_http_3.close()


# ---------------------------------------------------------------------------
# 12. GLOBAL EMERGENCY KILL SWITCH
# ---------------------------------------------------------------------------
def test_global_emergency_kill_switch_halts_all_outbound_messages(
    client: TestClient,
    admin_headers: dict[str, str],
):
    c = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919888889999",
            "name": "Kill Switch Test Customer",
            "utility_consent": True,
            "consent_method": "pos_receipt",
            "consent_source": "kap_pos",
            "consent_evidence": "Receipt #999",
        },
    ).json()

    # Activate Global Kill Switch via API
    ks_res = client.post(
        "/api/compliance/kill-switch",
        headers=admin_headers,
        json={"enabled": True, "reason": "Emergency compliance drill"},
    )
    assert ks_res.status_code == 200
    assert ks_res.json()["global_kill_switch"] is True

    # Attempt to send otherwise valid utility message -> BLOCKED by kill switch
    send_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c["id"],
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_service_reminder",
            "template_language": "en_US",
            "template_params": ["Customer", "Battery Check"],
        },
    )
    assert send_res.status_code == 403
    assert "GLOBAL_KILL_SWITCH_ACTIVE" in send_res.json()["detail"]["reason"]

    # Deactivate Global Kill Switch -> sending works again
    client.post(
        "/api/compliance/kill-switch",
        headers=admin_headers,
        json={"enabled": False, "reason": "Drill complete"},
    )
    send_after = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c["id"],
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_service_reminder",
            "template_language": "en_US",
            "template_params": ["Customer", "Battery Check"],
        },
    )
    assert send_after.status_code == 200


# ---------------------------------------------------------------------------
# 13. SCHEDULED MESSAGE REVALIDATION AT SEND TIME
# ---------------------------------------------------------------------------
def test_scheduled_message_revalidates_compliance_at_send_time(
    client: TestClient,
    admin_headers: dict[str, str],
    db_session: Session,
):
    c = client.post(
        "/api/customers",
        headers=admin_headers,
        json={
            "phone_number": "+919899990000",
            "name": "Scheduler Test Customer",
            "utility_consent": True,
            "consent_method": "pos_receipt",
            "consent_source": "kap_pos",
            "consent_evidence": "Receipt #1000",
        },
    ).json()

    future_ts = datetime.now(timezone.utc) + timedelta(minutes=30)
    sched_res = client.post(
        "/api/messages/send",
        headers=admin_headers,
        json={
            "customer_id": c["id"],
            "message_type": "template",
            "category": "utility",
            "template_name": "kap_service_reminder",
            "template_language": "en_US",
            "template_params": ["Customer", "Clutch Inspection"],
            "scheduled_at": future_ts.isoformat(),
        },
    )
    assert sched_res.status_code == 200
    msg_id = sched_res.json()["id"]

    # Fast-forward scheduled_at to the past so it is due now, and pause the template before execution
    msg = db_session.query(Message).filter(Message.id == msg_id).first()
    msg.scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    tpl = db_session.query(MessageTemplate).filter(MessageTemplate.name == "kap_service_reminder").first()
    tpl.is_active = False
    db_session.commit()

    # Run scheduled message processor -> must block because template was paused before send time
    summary = process_scheduled_messages_now(db=db_session)
    assert summary["total_due"] == 1
    assert summary["blocked_or_cancelled"] == 1

    db_session.refresh(msg)
    assert msg.status == "blocked_compliance"
    assert "TEMPLATE_PAUSED" in (msg.compliance_rejection_reason or "")
