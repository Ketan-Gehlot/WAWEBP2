"""Common authentication, compliance control, and analytics Pydantic v2 schemas."""
from typing import Literal
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_in_minutes: int


class KillSwitchUpdateRequest(BaseModel):
    enabled: bool = Field(..., description="Set True to immediately halt all outbound WhatsApp messaging")
    reason: str = Field(default="Emergency administrator override", min_length=3)


class AccountQualityUpdateRequest(BaseModel):
    account_quality_status: Literal["GREEN", "YELLOW", "RED", "RESTRICTED", "PAUSED"]
    reason: str = Field(default="Account quality update", min_length=3)


class AnalyticsSummaryResponse(BaseModel):
    total_inbound_messages: int
    total_outbound_messages: int
    sent: int
    delivered: int
    read: int
    failed: int
    dry_run: int
    scheduled: int
    opt_outs: int
    blocked_sends_due_to_compliance: int
    compliance_block_breakdown: dict[str, int]
    active_customers: int
    marketing_opted_in_customers: int
    utility_opted_in_customers: int
    escalated_conversations: int
    campaign_performance: list[dict]
    compliance_status: dict
