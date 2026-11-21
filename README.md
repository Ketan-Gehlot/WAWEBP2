# KAP WhatsApp Automation Platform
### Production-Ready Python Backend for Kanti Auto Parts (KAP) Using Official Meta WhatsApp Cloud API

A centralized, compliance-first WhatsApp Business Platform backend engineered for **Kanti Auto Parts (KAP)**. It integrates **exclusively** with the official **Meta WhatsApp Cloud API** (`https://graph.facebook.com/{version}/{phone_number_id}/messages`).

> **Non-Negotiable Compliance Principle**: Every outbound message—whether initiated by an administrative endpoint, automated customer support rule, scheduled task, or campaign—passes through the centralized `ComplianceEngine` (`app/core/compliance.py`). Any message lacking verifiable consent, violating the 24-hour customer service window, referencing an unapproved template, targeting an opted-out customer, or exceeding conservative sending limits is **blocked immediately** and logged with its rejection reason.

---

## 1. Project Folder Structure

```text
kap-whatsapp-automation/
├── app/
│   ├── __init__.py
│   ├── main.py                      # FastAPI application entrypoint & security middleware
│   ├── config.py                    # Validated environment settings with safe defaults
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py                # GET /health, POST /api/auth/login
│   │   ├── webhook.py               # GET /webhook, POST /webhook (HMAC-SHA256 verified)
│   │   ├── customers.py             # Customer CRUD, history, export, consent, opt-out, anonymization
│   │   ├── messages.py              # Outbound message dispatch & scheduled message trigger
│   │   ├── templates.py             # Approved template registry & Meta Graph API sync
│   │   ├── campaigns.py             # Campaign creation, mandatory preview/dry-run, confirmed execution
│   │   └── analytics.py             # Dashboard analytics, kill switch, account quality, audit logs
│   ├── services/
│   │   ├── __init__.py
│   │   ├── whatsapp_client.py       # Official Meta WhatsApp Cloud API HTTPX client
│   │   ├── message_service.py       # Outbound dispatch, scheduler revalidation, delivery tracking
│   │   ├── automation_service.py    # KAP customer support engine & human agent escalation
│   │   ├── consent_service.py       # Explicit opt-in audit logging & instant opt-out enforcement
│   │   ├── template_service.py      # Template registry management & synchronization
│   │   ├── campaign_service.py      # Campaign eligibility dry-run & execution
│   │   └── customer_service.py      # Customer lifecycle & data privacy anonymization
│   ├── models/
│   │   ├── __init__.py
│   │   ├── customer.py              # customers table
│   │   ├── conversation.py          # conversations table
│   │   ├── message.py               # messages table
│   │   ├── consent.py               # consent_records table
│   │   ├── template.py              # message_templates table
│   │   ├── campaign.py              # campaigns & campaign_recipients tables
│   │   └── system.py                # webhook_events, automation_rules, admin_users, audit_logs, compliance_state
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── customer.py
│   │   ├── message.py
│   │   ├── template.py
│   │   ├── campaign.py
│   │   └── common.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── security.py              # Bcrypt hashing, JWT auth, RBAC, HMAC-SHA256 webhook verification
│   │   ├── compliance.py            # Centralized WhatsApp Policy Compliance Engine
│   │   ├── rate_limiter.py          # Conservative per-minute and daily outbound rate limiter
│   │   └── logging.py               # Structured JSON logger with secret & PII redaction
│   ├── workers/
│   │   ├── __init__.py
│   │   └── message_worker.py        # Celery worker, idempotent webhook processor, scheduler
│   └── database/
│       ├── __init__.py
│       ├── base.py
│       └── connection.py
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial_kap_schema.py
├── scripts/
│   ├── sample_webhook_payloads.json
│   └── simulate_webhook.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── test_kap_whatsapp_platform.py
├── .env.example
├── .gitignore
├── alembic.ini
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## 2. Local Windows Installation & Setup Guide

### Step 1: Create and Activate a Python Virtual Environment (PowerShell)
```powershell
cd C:\Users\DELL\Desktop\bkendKAP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Step 2: Configure Environment Variables
Copy `.env.example` to `.env`:
```powershell
Copy-Item .env.example .env
```
By default, the application starts in **safe mode**:
- `ENABLE_OUTBOUND_MESSAGES=false`
- `DRY_RUN=true`
- `DATABASE_URL=sqlite:///./kap_whatsapp.db`

### Step 3: Run Database Migrations (Optional with SQLite as startup auto-initializes schema, recommended for PostgreSQL)
```powershell
alembic upgrade head
```

### Step 4: Run the Automated Test Suite
```powershell
pytest -v
```

### Step 5: Start the FastAPI Server Locally
```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
- Interactive Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc Reference: `http://127.0.0.1:8000/redoc`
- Health Check: `http://127.0.0.1:8000/health`

### Step 6: Simulate Signed Meta Webhook Payloads Locally
In a second terminal while Uvicorn is running:
```powershell
python scripts/simulate_webhook.py --event incoming_greeting
python scripts/simulate_webhook.py --event incoming_brake_pads_inquiry
python scripts/simulate_webhook.py --event incoming_location_inquiry
python scripts/simulate_webhook.py --event incoming_opt_out_stop
```

---

## 3. PostgreSQL, Redis, and Docker Deployment

### Using Docker Compose (PostgreSQL 16 + Redis 7 + FastAPI + Celery Worker + Celery Beat)
```powershell
docker compose up --build -d
docker compose logs -f api
```

### Standalone PostgreSQL & Redis Setup
1. **PostgreSQL**:
   ```sql
   CREATE USER kap_admin WITH PASSWORD 'your_strong_db_password';
   CREATE DATABASE kap_whatsapp OWNER kap_admin;
   ```
   Update `DATABASE_URL=postgresql+psycopg2://kap_admin:your_strong_db_password@localhost:5432/kap_whatsapp` in `.env` and run `alembic upgrade head`.
2. **Redis & Celery**:
   Start Redis on `redis://localhost:6379/0`, then launch the worker and scheduler:
   ```powershell
   celery -A app.workers.message_worker.celery_app worker --loglevel=info --pool=solo
   celery -A app.workers.message_worker.celery_app beat --loglevel=info
   ```

---

## 4. Official Meta WhatsApp Cloud API & Webhook Configuration Guide

> **Important Transparency Note**: Live WhatsApp delivery requires an approved Meta Developer App, a verified WhatsApp Business Account (WABA), a registered business phone number, and approved templates in Meta Business Manager. Do not set `ENABLE_OUTBOUND_MESSAGES=true` and `DRY_RUN=false` until your live credentials are configured.

### A. System User Access Token Setup
1. Open **Meta Business Settings** -> **Users** -> **System Users**.
2. Create or select a **System User** with Admin role.
3. Click **Assign Assets** -> assign your **WhatsApp Business Account** with full control (`whatsapp_business_messaging` and `whatsapp_business_management` permissions).
4. Generate a permanent System User Access Token and store it securely in `.env` as `META_ACCESS_TOKEN`.
5. Copy your **Phone Number ID** (`META_PHONE_NUMBER_ID`), **WhatsApp Business Account ID** (`META_WABA_ID`), and **App Secret** (`META_APP_SECRET`, found in Meta App Dashboard -> App Settings -> Basic).

### B. Webhook Configuration & HTTPS Requirement
Meta requires a publicly accessible **HTTPS** endpoint with a valid TLS certificate before it will verify your webhook:
1. Deploy the backend behind an HTTPS reverse proxy (such as Nginx, Caddy, Cloudflare Tunnel, or AWS ALB) terminating TLS 1.2/1.3 and forwarding to `http://127.0.0.1:8000`.
2. Set a strong random `META_WEBHOOK_VERIFY_TOKEN` in `.env`.
3. In **Meta App Dashboard** -> **WhatsApp** -> **Configuration** -> **Webhook**:
   - **Callback URL**: `https://whatsapp.kantiautoparts.com/webhook`
   - **Verify Token**: Exact value of `META_WEBHOOK_VERIFY_TOKEN`
4. Click **Verify and Save** (Meta will send `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`).
5. Subscribe to the `messages` webhook field.

---

## 5. API Overview

| Method | Path | Auth | Description |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Public | System health, DB connectivity, and compliance safety state |
| `POST` | `/api/auth/login` | Public | Obtain administrator JWT Bearer token |
| `GET` | `/webhook` | Meta Verify Token | Meta webhook challenge verification |
| `POST` | `/webhook` | HMAC-SHA256 | Receive incoming messages & delivery/read status updates |
| `GET` | `/api/customers` | Admin | Search and filter customers |
| `POST` | `/api/customers` | Admin | Create customer (never grants automatic opt-in without proof) |
| `GET` | `/api/customers/export` | Admin (Manager+) | Export only opted-in, non-opted-out customer records |
| `GET` | `/api/customers/{customer_id}` | Admin | Full customer profile, 24h window state, consent & message history |
| `DELETE` | `/api/customers/{customer_id}` | Admin (Manager+) | Anonymize customer PII and cancel queued communications |
| `POST` | `/api/customers/{customer_id}/consent` | Admin | Record verifiable marketing/utility opt-in consent |
| `POST` | `/api/customers/{customer_id}/opt-out` | Admin | Immediately opt out customer & cancel queued/scheduled sends |
| `GET` | `/api/messages` | Admin | Filter inbound/outbound message records |
| `POST` | `/api/messages/send` | Admin | Send or schedule message via `ComplianceEngine` |
| `POST` | `/api/messages/process-scheduled` | Admin (Manager+) | Execute due scheduled messages with send-time revalidation |
| `GET` | `/api/templates` | Admin | List registered WhatsApp message templates |
| `POST` | `/api/templates` | Admin (Manager+) | Register template in compliance registry |
| `POST` | `/api/templates/sync` | Admin (Manager+) | Sync templates from Meta Graph API |
| `POST` | `/api/campaigns` | Admin (Manager+) | Create campaign draft |
| `POST` | `/api/campaigns/{campaign_id}/preview` | Admin (Manager+) | Mandatory dry-run eligibility check & exclusion report |
| `POST` | `/api/campaigns/{campaign_id}/execute` | Admin (Manager+) | Execute previewed campaign after explicit confirmation |
| `GET` | `/api/analytics` | Admin | Dashboard analytics for messages, opt-outs, compliance blocks |
| `POST` | `/api/compliance/kill-switch` | Admin (Manager+) | Toggle global emergency outbound kill switch |
| `POST` | `/api/compliance/account-quality` | Admin (Manager+) | Update account quality status (`GREEN`, `YELLOW`, `RED`, `RESTRICTED`) |

---

## 6. Security & Compliance Checklist

- [x] **Official Meta Cloud API Only**: Uses `https://graph.facebook.com/{version}/{phone_number_id}/messages` over HTTPS with HTTPX.
- [x] **Centralized Compliance Engine**: All outbound sends pass through `ComplianceEngine.evaluate_outbound()`.
- [x] **Explicit Consent Verification**: Separate `marketing_consent` and `utility_consent` with timestamp, collection method, source, and evidence; imported contacts default to `False`.
- [x] **24-Hour Customer Service Window**: Free-form replies are blocked when `now - last_inbound_message_at > 24h`.
- [x] **Approved Templates Only**: Blocks unapproved, paused, rejected, low-quality, or misclassified promotional templates.
- [x] **Automatic Opt-Out Enforcement**: Detects `STOP`, `UNSUBSCRIBE`, `CANCEL`, `QUIT`, `END`; blocks future messages and cancels scheduled sends immediately.
- [x] **Mandatory Campaign Dry-Run**: Campaigns cannot execute without `/preview` and `confirm_execution=true`.
- [x] **Webhook Security**: Constant-time `hmac.compare_digest` on `hub.verify_token` and `X-Hub-Signature-256` (`HMAC-SHA256`).
- [x] **Global Emergency Kill Switch**: Instant halt of all outbound traffic via environment setting or `/api/compliance/kill-switch`.
- [x] **Secret & PII Redaction**: Structured JSON logs automatically redact tokens, secrets, passwords, and mask phone numbers.
