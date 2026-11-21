"""WhatsApp Message Templates Registry API Endpoints."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.security import get_current_admin, record_audit_log, require_roles
from app.database.connection import get_db
from app.models.system import AdminUser
from app.models.template import MessageTemplate
from app.schemas.template import TemplateCreate, TemplateResponse, TemplateUpdate
from app.services.template_service import TemplateService

router = APIRouter(prefix="/api/templates", tags=["Message Templates"])


@router.get(
    "",
    response_model=list[TemplateResponse],
    summary="List registered WhatsApp message templates",
)
def list_templates(
    category: Optional[str] = Query(default=None),
    approval_status: Optional[str] = Query(default=None),
    active_only: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> list[TemplateResponse]:
    templates = TemplateService.list_templates(
        db=db,
        category=category,
        approval_status=approval_status,
        active_only=active_only,
    )
    return [TemplateResponse.model_validate(t) for t in templates]


@router.post(
    "",
    response_model=TemplateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a WhatsApp message template in the compliance registry",
)
def create_template(
    payload: TemplateCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> TemplateResponse:
    try:
        tpl = TemplateService.create_template(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="create_template",
        resource_type="message_template",
        resource_id=str(tpl.id),
        details={"name": tpl.name, "category": tpl.category, "approval_status": tpl.approval_status},
        ip_address=request.client.host if request.client else None,
    )
    return TemplateResponse.model_validate(tpl)


@router.patch(
    "/{template_id}",
    response_model=TemplateResponse,
    summary="Update template approval, active, or quality status",
)
def update_template(
    template_id: int,
    payload: TemplateUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> TemplateResponse:
    template = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")

    updated = TemplateService.update_template(db, template, payload)
    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="update_template",
        resource_type="message_template",
        resource_id=str(template.id),
        details=payload.model_dump(exclude_none=True),
        ip_address=request.client.host if request.client else None,
    )
    return TemplateResponse.model_validate(updated)


@router.post(
    "/sync",
    summary="Synchronize templates from official Meta Graph API",
)
def sync_templates_from_meta(
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> dict:
    try:
        result = TemplateService.sync_from_meta(db)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to synchronize templates from Meta Graph API: {exc}",
        ) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="sync_templates",
        resource_type="message_template",
        details=result,
        ip_address=request.client.host if request.client else None,
    )
    return result
