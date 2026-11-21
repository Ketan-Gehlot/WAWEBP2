"""Outbound Messaging, Message History, and Scheduled Message Processing Endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.security import get_current_admin, record_audit_log, require_roles
from app.database.connection import get_db
from app.models.message import Message
from app.models.system import AdminUser
from app.schemas.message import MessageResponse, MessageSendRequest
from app.services.message_service import MessageService
from app.workers.message_worker import process_scheduled_messages_now

router = APIRouter(prefix="/api/messages", tags=["Messages"])


@router.get(
    "",
    response_model=list[MessageResponse],
    summary="List message records with filtering by direction, status, customer, or category",
)
def list_messages(
    customer_id: Optional[int] = Query(default=None),
    direction: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    category: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> list[MessageResponse]:
    query = db.query(Message)
    if customer_id is not None:
        query = query.filter(Message.customer_id == customer_id)
    if direction:
        query = query.filter(Message.direction == direction.lower())
    if status_filter:
        query = query.filter(Message.status == status_filter.lower())
    if category:
        query = query.filter(Message.category == category.lower())

    messages = query.order_by(Message.id.desc()).offset(skip).limit(limit).all()
    return [MessageResponse.model_validate(m) for m in messages]


@router.post(
    "/send",
    response_model=MessageResponse,
    summary="Send or schedule an outbound WhatsApp message through the centralized Compliance Engine",
)
def send_outbound_message(
    payload: MessageSendRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager", "support_agent")),
) -> MessageResponse:
    """
    Send or schedule an outbound WhatsApp message.
    Every request passes through ComplianceEngine.evaluate_outbound().
    If a message violates policy (no consent, opted out, 24h window closed for free-form text,
    unapproved template, rate limit, or global kill switch), it is BLOCKED and the rejection reason is recorded.
    """
    try:
        msg = MessageService.send_or_schedule_message(db=db, request=payload)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="send_message",
        resource_type="message",
        resource_id=str(msg.id),
        details={
            "status": msg.status,
            "compliance_approved": msg.compliance_approved,
            "rejection_reason": msg.compliance_rejection_reason,
            "category": msg.category,
            "message_type": msg.message_type,
        },
        ip_address=request.client.host if request.client else None,
    )

    if not msg.compliance_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message_id": msg.id,
                "status": msg.status,
                "compliance_approved": False,
                "reason": msg.compliance_rejection_reason,
            },
        )

    return MessageResponse.model_validate(msg)


@router.post(
    "/process-scheduled",
    summary="Trigger execution of due scheduled messages with send-time compliance revalidation",
)
def trigger_scheduled_messages(
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    summary = process_scheduled_messages_now(db=db)
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="process_scheduled_messages",
        resource_type="message_scheduler",
        details=summary,
        ip_address=request.client.host if request.client else None,
    )
    return summary
