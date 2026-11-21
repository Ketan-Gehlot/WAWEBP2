"""MessageTemplate Pydantic v2 schemas."""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class TemplateCreate(BaseModel):
    meta_template_id: Optional[str] = None
    name: str = Field(..., min_length=2, max_length=150, pattern=r"^[a-z0-9_]+$")
    language: str = Field(default="en_US", max_length=20)
    category: Literal["UTILITY", "MARKETING", "AUTHENTICATION"]
    approval_status: Literal["APPROVED", "PENDING", "REJECTED", "PAUSED", "DISABLED"] = "PENDING"
    is_active: bool = True
    quality_score: Literal["GREEN", "YELLOW", "RED", "UNKNOWN"] = "GREEN"
    header_text: Optional[str] = None
    body_text: str = Field(..., min_length=3)
    footer_text: Optional[str] = None
    variable_definitions: list[str] = Field(default_factory=list)


class TemplateUpdate(BaseModel):
    approval_status: Optional[Literal["APPROVED", "PENDING", "REJECTED", "PAUSED", "DISABLED"]] = None
    is_active: Optional[bool] = None
    quality_score: Optional[Literal["GREEN", "YELLOW", "RED", "UNKNOWN"]] = None
    body_text: Optional[str] = None
    variable_definitions: Optional[list[str]] = None


class TemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    meta_template_id: Optional[str] = None
    name: str
    language: str
    category: str
    approval_status: str
    is_active: bool
    quality_score: str
    header_text: Optional[str] = None
    body_text: str
    footer_text: Optional[str] = None
    variable_definitions: list
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
