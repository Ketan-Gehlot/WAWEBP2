"""Analytics Dashboard, Compliance Kill Switch, Account Quality Controls, and Audit Logs API."""
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.core.compliance import ComplianceEngine
from app.core.rate_limiter import outbound_rate_limiter
from app.core.security import get_current_admin, record_audit_log, require_roles
from app.database.connection import get_db
from app.models.campaign import Campaign
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.system import AdminUser, AuditLog
from app.schemas.common import (
    AccountQualityUpdateRequest,
    AnalyticsSummaryResponse,
    KillSwitchUpdateRequest,
)

router = APIRouter(prefix="/api", tags=["Analytics & Compliance Controls"])


@router.get(
    "/analytics",
    response_model=AnalyticsSummaryResponse,
    summary="Dashboard-ready messaging, compliance, and campaign analytics",
)
def get_analytics_dashboard(
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> AnalyticsSummaryResponse:
    total_inbound = (
        db.query(func.count(Message.id)).filter(Message.direction == "inbound").scalar() or 0
    )
    total_outbound = (
        db.query(func.count(Message.id)).filter(Message.direction == "outbound").scalar() or 0
    )

    status_counts = dict(
        db.query(Message.status, func.count(Message.id))
        .filter(Message.direction == "outbound")
        .group_by(Message.status)
        .all()
    )

    sent_count = status_counts.get("sent", 0)
    delivered_count = status_counts.get("delivered", 0)
    read_count = status_counts.get("read", 0)
    failed_count = status_counts.get("failed", 0)
    dry_run_count = status_counts.get("dry_run", 0)
    scheduled_count = status_counts.get("scheduled", 0)
    blocked_count = status_counts.get("blocked_compliance", 0) + status_counts.get("cancelled_opt_out", 0)

    # Breakdown of compliance rejection reasons
    blocked_rows = (
        db.query(Message.compliance_rejection_reason, func.count(Message.id))
        .filter(
            Message.direction == "outbound",
            Message.status.in_(("blocked_compliance", "cancelled_opt_out")),
        )
        .group_by(Message.compliance_rejection_reason)
        .all()
    )
    block_breakdown = {
        (reason.split(":")[0] if reason else "UNKNOWN"): count
        for reason, count in blocked_rows
    }

    opt_outs_count = (
        db.query(func.count(Customer.id)).filter(Customer.opted_out.is_(True)).scalar() or 0
    )
    active_customers = (
        db.query(func.count(Customer.id))
        .filter(Customer.is_anonymized.is_(False), Customer.opted_out.is_(False))
        .scalar()
        or 0
    )
    marketing_opted_in = (
        db.query(func.count(Customer.id))
        .filter(
            Customer.is_anonymized.is_(False),
            Customer.opted_out.is_(False),
            Customer.marketing_consent.is_(True),
        )
        .scalar()
        or 0
    )
    utility_opted_in = (
        db.query(func.count(Customer.id))
        .filter(
            Customer.is_anonymized.is_(False),
            Customer.opted_out.is_(False),
            Customer.utility_consent.is_(True),
        )
        .scalar()
        or 0
    )
    escalated_convs = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.status == "escalated_to_human")
        .scalar()
        or 0
    )

    campaigns = db.query(Campaign).order_by(Campaign.id.desc()).limit(25).all()
    campaign_perf = [
        {
            "id": c.id,
            "name": c.name,
            "category": c.category,
            "status": c.status,
            "total_selected": c.total_contacts_selected,
            "eligible": c.eligible_recipient_count,
            "excluded": c.excluded_recipient_count,
            "sent": c.sent_count,
            "delivered": c.delivered_count,
            "read": c.read_count,
            "failed": c.failed_count,
            "blocked": c.blocked_count,
        }
        for c in campaigns
    ]

    state = ComplianceEngine.get_or_create_state(db)
    compliance_status = {
        "global_kill_switch": bool(settings.GLOBAL_KILL_SWITCH or state.global_kill_switch),
        "account_quality_status": state.account_quality_status,
        "enable_outbound_messages": settings.ENABLE_OUTBOUND_MESSAGES,
        "dry_run": settings.DRY_RUN,
        "rate_limits": outbound_rate_limiter.get_usage_snapshot(db=db),
    }

    return AnalyticsSummaryResponse(
        total_inbound_messages=total_inbound,
        total_outbound_messages=total_outbound,
        sent=sent_count,
        delivered=delivered_count,
        read=read_count,
        failed=failed_count,
        dry_run=dry_run_count,
        scheduled=scheduled_count,
        opt_outs=opt_outs_count,
        blocked_sends_due_to_compliance=blocked_count,
        compliance_block_breakdown=block_breakdown,
        active_customers=active_customers,
        marketing_opted_in_customers=marketing_opted_in,
        utility_opted_in_customers=utility_opted_in,
        escalated_conversations=escalated_convs,
        campaign_performance=campaign_perf,
        compliance_status=compliance_status,
    )


@router.post(
    "/compliance/kill-switch",
    summary="Activate or deactivate the Global Emergency Kill Switch",
)
def toggle_global_kill_switch(
    payload: KillSwitchUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    state = ComplianceEngine.set_kill_switch(
        db=db,
        enabled=payload.enabled,
        actor=current_admin.username,
        reason=payload.reason,
    )
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="toggle_global_kill_switch",
        resource_type="compliance_state",
        resource_id="1",
        details={"enabled": state.global_kill_switch, "reason": state.reason},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "global_kill_switch": state.global_kill_switch,
        "reason": state.reason,
        "updated_by": state.updated_by,
        "updated_at": state.updated_at.isoformat(),
    }


@router.post(
    "/compliance/account-quality",
    summary="Update WhatsApp Business Account quality status (pauses sending on warnings/restrictions)",
)
def update_account_quality_status(
    payload: AccountQualityUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    state = ComplianceEngine.set_account_quality_status(
        db=db,
        quality_status=payload.account_quality_status,
        actor=current_admin.username,
        reason=payload.reason,
    )
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="update_account_quality_status",
        resource_type="compliance_state",
        resource_id="1",
        details={"account_quality_status": state.account_quality_status, "reason": state.reason},
        ip_address=request.client.host if request.client else None,
    )
    return {
        "account_quality_status": state.account_quality_status,
        "reason": state.reason,
        "updated_by": state.updated_by,
        "updated_at": state.updated_at.isoformat(),
    }


@router.get(
    "/audit-logs",
    summary="Retrieve administrative and security audit logs",
)
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "auditor")),
) -> list[dict]:
    logs = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return [
        {
            "id": entry.id,
            "actor": entry.actor,
            "action": entry.action,
            "resource_type": entry.resource_type,
            "resource_id": entry.resource_id,
            "details": entry.details,
            "ip_address": entry.ip_address,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
        for entry in logs
    ]
