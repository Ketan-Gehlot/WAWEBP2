"""
Web UI Router & Automation Rule Management Endpoints.

Serves the Maxis Gold Jewellery WhatsApp Automation Dashboard at GET / and provides
endpoints to view, create, toggle, and test automated customer support rules.
"""
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import get_current_admin, record_audit_log, require_roles
from app.database.connection import get_db
from app.models.system import AdminUser, AutomationRule
from app.services.automation_service import AutomationService
from app.services.customer_service import CustomerService

router = APIRouter(tags=["Web UI & Automation Rules"])

STATIC_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "index.html"


class AutomationRuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    rule_type: str = Field(default="keyword", max_length=50)
    keywords: list[str] = Field(..., min_length=1)
    response_text: str = Field(..., min_length=2)
    requires_human_escalation: bool = False
    priority: int = Field(default=50, ge=1, le=1000)
    is_active: bool = True


class AutomationRuleToggleRequest(BaseModel):
    is_active: Optional[bool] = None
    response_text: Optional[str] = None
    keywords: Optional[list[str]] = None
    requires_human_escalation: Optional[bool] = None


class SimulateIncomingRequest(BaseModel):
    phone_number: str = Field(default="+919876543210")
    customer_name: str = Field(default="Test Customer")
    message_text: str = Field(..., min_length=1)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_dashboard_ui() -> HTMLResponse:
    """Serve the simple, automation-focused Web UI dashboard."""
    if not STATIC_HTML_PATH.exists():
        return HTMLResponse("<h1>UI file not found</h1>", status_code=404)
    return HTMLResponse(STATIC_HTML_PATH.read_text(encoding="utf-8"))


@router.get(
    "/api/customers/sample-excel",
    summary="Download the sample Excel (.xlsx) template for customer import",
)
def download_sample_excel() -> FileResponse:
    sample_path = Path("kap_customers_import_template.xlsx").resolve()
    if not sample_path.exists():
        raise HTTPException(status_code=404, detail="Sample Excel template not found.")
    return FileResponse(
        path=str(sample_path),
        filename="kap_customers_import_template.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get(
    "/api/automation/rules",
    summary="List all automated customer support rules",
)
def list_automation_rules(
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> list[dict]:
    rules = db.query(AutomationRule).order_by(AutomationRule.priority.asc(), AutomationRule.id.asc()).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "rule_type": r.rule_type,
            "keywords": r.keywords or [],
            "response_text": r.response_text,
            "requires_human_escalation": r.requires_human_escalation,
            "priority": r.priority,
            "is_active": r.is_active,
        }
        for r in rules
    ]


@router.post(
    "/api/automation/rules",
    status_code=status.HTTP_201_CREATED,
    summary="Create or update a keyword automation rule",
)
def create_automation_rule(
    payload: AutomationRuleCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    clean_keywords = [k.strip().lower() for k in payload.keywords if k.strip()]
    if not clean_keywords:
        raise HTTPException(status_code=400, detail="At least one non-empty keyword is required.")

    existing = db.query(AutomationRule).filter(AutomationRule.name == payload.name.strip()).first()
    if existing:
        existing.rule_type = payload.rule_type
        existing.keywords = clean_keywords
        existing.response_text = payload.response_text
        existing.requires_human_escalation = payload.requires_human_escalation
        existing.priority = payload.priority
        existing.is_active = payload.is_active
        db.commit()
        db.refresh(existing)
        rule = existing
    else:
        rule = AutomationRule(
            name=payload.name.strip(),
            rule_type=payload.rule_type,
            keywords=clean_keywords,
            response_text=payload.response_text,
            requires_human_escalation=payload.requires_human_escalation,
            priority=payload.priority,
            is_active=payload.is_active,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="upsert_automation_rule",
        resource_type="automation_rule",
        resource_id=str(rule.id),
        details={"name": rule.name, "keywords": rule.keywords},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "id": rule.id,
        "name": rule.name,
        "rule_type": rule.rule_type,
        "keywords": rule.keywords,
        "response_text": rule.response_text,
        "requires_human_escalation": rule.requires_human_escalation,
        "priority": rule.priority,
        "is_active": rule.is_active,
    }


@router.patch(
    "/api/automation/rules/{rule_id}",
    summary="Toggle or update an existing automation rule",
)
def update_automation_rule(
    rule_id: int,
    payload: AutomationRuleToggleRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    rule = db.query(AutomationRule).filter(AutomationRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Automation rule not found.")

    if payload.is_active is not None:
        rule.is_active = payload.is_active
    if payload.response_text is not None:
        rule.response_text = payload.response_text
    if payload.keywords is not None:
        rule.keywords = [k.strip().lower() for k in payload.keywords if k.strip()]
    if payload.requires_human_escalation is not None:
        rule.requires_human_escalation = payload.requires_human_escalation

    db.commit()
    db.refresh(rule)
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="update_automation_rule",
        resource_type="automation_rule",
        resource_id=str(rule.id),
        details={"is_active": rule.is_active},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "id": rule.id,
        "name": rule.name,
        "rule_type": rule.rule_type,
        "keywords": rule.keywords,
        "response_text": rule.response_text,
        "requires_human_escalation": rule.requires_human_escalation,
        "priority": rule.priority,
        "is_active": rule.is_active,
    }


@router.post(
    "/api/automation/simulate-incoming",
    summary="Simulate an incoming customer WhatsApp inquiry and trigger the automated response engine",
)
def simulate_incoming_inquiry(
    payload: SimulateIncomingRequest,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> dict:
    import uuid

    customer = CustomerService.get_or_create_from_inbound(
        db=db,
        phone_number=payload.phone_number,
        profile_name=payload.customer_name,
    )
    conv = AutomationService.get_or_create_active_conversation(db, customer)
    reply_text, escalated, escalation_reason = AutomationService.determine_support_response(
        db=db,
        customer=customer,
        conversation=conv,
        incoming_text=payload.message_text,
    )
    sim_wamid = f"wamid.UISIM.{uuid.uuid4().hex[:16]}"
    result = AutomationService.handle_incoming_message(
        db=db,
        customer=customer,
        whatsapp_message_id=sim_wamid,
        message_type="text",
        text_content=payload.message_text,
    )
    return {
        "customer_id": customer.id,
        "phone_number": customer.phone_number,
        "incoming_text": payload.message_text,
        "action": result.get("action"),
        "automated_reply": None if result.get("action") == "opt_out_processed" else reply_text,
        "outbound_status": result.get("outbound_status"),
        "escalated_to_human": escalated,
        "escalation_reason": escalation_reason,
    }
