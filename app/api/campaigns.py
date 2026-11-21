"""Campaign Management, Mandatory Dry-Run Preview, and Confirmed Execution Endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.security import get_current_admin, record_audit_log, require_roles
from app.database.connection import get_db
from app.models.campaign import Campaign
from app.models.system import AdminUser
from app.schemas.campaign import (
    CampaignCreate,
    CampaignExecuteRequest,
    CampaignPreviewResponse,
    CampaignResponse,
)
from app.services.campaign_service import CampaignService

router = APIRouter(prefix="/api/campaigns", tags=["Campaigns"])


@router.get(
    "",
    response_model=list[CampaignResponse],
    summary="List all campaigns and their compliance/delivery metrics",
)
def list_campaigns(
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> list[CampaignResponse]:
    campaigns = db.query(Campaign).order_by(Campaign.id.desc()).all()
    return [CampaignResponse.model_validate(c) for c in campaigns]


@router.get(
    "/{campaign_id}",
    response_model=CampaignResponse,
    summary="Get campaign details",
)
def get_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(get_current_admin),
) -> CampaignResponse:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    return CampaignResponse.model_validate(campaign)


@router.post(
    "",
    response_model=CampaignResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new campaign draft (requires preview and explicit confirmation before sending)",
)
def create_campaign(
    payload: CampaignCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> CampaignResponse:
    try:
        campaign = CampaignService.create_campaign(
            db=db,
            payload=payload,
            actor=current_admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="create_campaign",
        resource_type="campaign",
        resource_id=str(campaign.id),
        details={"name": campaign.name, "category": campaign.category, "template_id": campaign.template_id},
        ip_address=request.client.host if request.client else None,
    )
    return CampaignResponse.model_validate(campaign)


@router.post(
    "/{campaign_id}/preview",
    response_model=CampaignPreviewResponse,
    summary="Mandatory campaign dry-run and consent eligibility check",
)
def preview_campaign(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> CampaignPreviewResponse:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")

    try:
        preview = CampaignService.preview_campaign(db=db, campaign=campaign)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="preview_campaign",
        resource_type="campaign",
        resource_id=str(campaign.id),
        details=preview.model_dump(),
        ip_address=request.client.host if request.client else None,
    )
    return preview


@router.post(
    "/{campaign_id}/execute",
    response_model=CampaignResponse,
    summary="Execute a previewed campaign after explicit administrator confirmation",
)
def execute_campaign(
    campaign_id: int,
    payload: CampaignExecuteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_admin: AdminUser = Depends(require_roles("superadmin", "manager")),
) -> CampaignResponse:
    campaign = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")

    try:
        executed = CampaignService.execute_campaign(
            db=db,
            campaign=campaign,
            request=payload,
            actor=current_admin.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    record_audit_log(
        db=db,
        actor=current_admin.username,
        action="execute_campaign",
        resource_type="campaign",
        resource_id=str(campaign.id),
        details={
            "sent_count": executed.sent_count,
            "failed_count": executed.failed_count,
            "blocked_count": executed.blocked_count,
        },
        ip_address=request.client.host if request.client else None,
    )
    return CampaignResponse.model_validate(executed)
