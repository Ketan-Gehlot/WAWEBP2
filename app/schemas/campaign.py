"""Campaign Pydantic v2 schemas."""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=200)
    purpose: str = Field(..., min_length=5)
    category: Literal["MARKETING", "UTILITY"]
    template_id: int
    default_template_params: list[str] = Field(default_factory=list)
    customer_ids: Optional[list[int]] = Field(
        default=None,
        description="Explicit list of target customer IDs (if omitted, evaluates all active customers)",
    )
    raw_phone_numbers: Optional[list[str]] = Field(
        default=None,
        description="Optional list of candidate phone numbers to evaluate against consent records",
    )
    scheduled_at: Optional[datetime] = None


class CampaignPreviewResponse(BaseModel):
    campaign_id: int
    campaign_name: str
    category: str
    template_name: str
    template_approval_status: str
    preview_completed: bool
    total_contacts_selected: int
    contacts_with_valid_consent: int
    contacts_without_consent: int
    opted_out_contacts: int
    contacts_with_invalid_phone_numbers: int
    contacts_exceeding_restrictions: int
    eligible_recipient_count: int
    excluded_recipient_count: int
    exclusion_reasons: dict[str, int]
    eligible_recipients: list[dict] = Field(default_factory=list)
    remaining_24h_portfolio_capacity: int = 250
    requires_admin_confirmation: bool = True


class CampaignExecuteRequest(BaseModel):
    confirm_execution: bool = Field(
        ...,
        description="Administrator must explicitly set confirm_execution=True after reviewing preview results.",
    )
    dry_run: bool = Field(
        default=True,
        description="Defaults to True (dry-run mode). Live messages are never sent unless dry_run=False AND authorize_live_send=True.",
    )
    authorize_live_send: bool = Field(
        default=False,
        description="Explicit administrator authorization required when dry_run=False to send live WhatsApp messages.",
    )
    dry_run_override: Optional[bool] = Field(
        default=None,
        description="Optional override to force dry-run execution without live delivery.",
    )


class CampaignResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    purpose: str
    category: str
    template_id: int
    default_template_params: list
    total_contacts_selected: int
    eligible_recipient_count: int
    excluded_recipient_count: int
    exclusion_reasons: dict
    preview_completed: bool
    previewed_at: Optional[datetime] = None
    admin_confirmed: bool
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    scheduled_at: Optional[datetime] = None
    status: str
    sent_count: int
    delivered_count: int
    read_count: int
    failed_count: int
    blocked_count: int
    created_by: str
    created_at: datetime
    updated_at: datetime
