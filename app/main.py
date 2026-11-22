"""
Maxis Gold Jewellery WhatsApp Automation Platform - FastAPI Application Entrypoint.

Production-ready backend for Maxis Gold Jewellery using the official Meta WhatsApp Cloud API.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request, Response
import pandas as pd

from app.api import analytics, campaigns, customers, health, messages, templates, ui, webhook
from app.config import settings
from app.core.compliance import ComplianceEngine
from app.core.logging import log_structured_event
from app.core.security import ensure_default_admin
from app.database.connection import SessionLocal, init_db
from app.services.automation_service import AutomationService
from app.services.template_service import TemplateService


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database schema, default admin account, baseline templates, and automation rules."""
    init_db()
    db = SessionLocal()
    try:
        ensure_default_admin(db)
        ComplianceEngine.get_or_create_state(db)
        TemplateService.seed_default_templates(db)
        AutomationService.seed_default_rules(db)
        sample_xlsx = Path("kap_customers_import_template.xlsx")
        if not sample_xlsx.exists():
            sample_rows = [
                {
                    "phone_number": "9876543210",
                    "name": "Rajesh Sharma",
                    "marketing_consent": "yes",
                    "utility_consent": "yes",
                    "consent_method": "pos_signed_form",
                    "consent_source": "kap_delhi_counter",
                    "consent_evidence": "Signed POS invoice opt-in #INV-2026-101",
                    "opted_out": "no",
                },
                {
                    "phone_number": "+91 98111-22334",
                    "name": "Priya Nair",
                    "marketing_consent": "yes",
                    "utility_consent": "yes",
                    "consent_method": "website_optin_checkbox",
                    "consent_source": "kap_website",
                    "consent_evidence": "Website form opt-in ID #WEB-882",
                    "opted_out": "no",
                },
                {
                    "phone_number": "09822233445",
                    "name": "Amit Verma (No Consent Proof)",
                    "marketing_consent": "yes",
                    "utility_consent": "no",
                    "consent_method": "",
                    "consent_source": "",
                    "consent_evidence": "",
                    "opted_out": "no",
                },
                {
                    "phone_number": "919833344556",
                    "name": "Suresh Patel (Opted Out)",
                    "marketing_consent": "no",
                    "utility_consent": "no",
                    "consent_method": "pos_signed_form",
                    "consent_source": "kap_delhi_counter",
                    "consent_evidence": "Customer requested opt-out",
                    "opted_out": "yes",
                },
            ]
            pd.DataFrame(sample_rows).to_excel(sample_xlsx, index=False, engine="openpyxl")
        log_structured_event(
            "application_startup",
            f"Started {settings.APP_NAME} (outbound_enabled={settings.ENABLE_OUTBOUND_MESSAGES}, dry_run={settings.DRY_RUN})",
            environment=settings.APP_ENV,
            outbound_enabled=settings.ENABLE_OUTBOUND_MESSAGES,
            dry_run=settings.DRY_RUN,
        )
    finally:
        db.close()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Compliance-first WhatsApp Business Platform automation backend for **Maxis Gold Jewellery**, "
        "integrating exclusively with the official Meta WhatsApp Cloud API."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next) -> Response:
    """Attach defensive security headers to all HTTP responses."""
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


# Register API routers
app.include_router(ui.router)
app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(customers.router)
app.include_router(messages.router)
app.include_router(templates.router)
app.include_router(campaigns.router)
app.include_router(analytics.router)
