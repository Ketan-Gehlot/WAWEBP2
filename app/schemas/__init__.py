"""Pydantic v2 request and response schemas."""
from app.schemas.campaign import (
    CampaignCreate,
    CampaignExecuteRequest,
    CampaignPreviewResponse,
    CampaignResponse,
)
from app.schemas.common import (
    AccountQualityUpdateRequest,
    AnalyticsSummaryResponse,
    KillSwitchUpdateRequest,
    LoginRequest,
    TokenResponse,
)
from app.schemas.customer import (
    ConsentRecordResponse,
    ConsentUpdateRequest,
    CustomerCreate,
    CustomerHistoryResponse,
    CustomerResponse,
    CustomerUpdate,
    OptOutRequest,
)
from app.schemas.message import MessageResponse, MessageSendRequest
from app.schemas.template import TemplateCreate, TemplateResponse, TemplateUpdate

__all__ = [
    "CustomerCreate",
    "CustomerUpdate",
    "ConsentUpdateRequest",
    "OptOutRequest",
    "ConsentRecordResponse",
    "CustomerResponse",
    "CustomerHistoryResponse",
    "MessageSendRequest",
    "MessageResponse",
    "TemplateCreate",
    "TemplateUpdate",
    "TemplateResponse",
    "CampaignCreate",
    "CampaignPreviewResponse",
    "CampaignExecuteRequest",
    "CampaignResponse",
    "LoginRequest",
    "TokenResponse",
    "KillSwitchUpdateRequest",
    "AccountQualityUpdateRequest",
    "AnalyticsSummaryResponse",
]
