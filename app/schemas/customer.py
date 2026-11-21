"""Customer and Consent Pydantic v2 schemas."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.compliance import is_valid_e164_phone, normalize_e164_phone


class CustomerCreate(BaseModel):
    """
    Schema for creating a customer.
    Note: Importing or adding a contact NEVER grants automatic consent;
    marketing_consent and utility_consent default to False unless explicit consent evidence is provided.
    """

    phone_number: str = Field(..., description="Customer phone number in international E.164 format (+919876543210)")
    name: Optional[str] = Field(default=None, max_length=255)
    marketing_consent: bool = Field(default=False)
    utility_consent: bool = Field(default=False)
    consent_method: Optional[str] = Field(default=None, max_length=100)
    consent_source: Optional[str] = Field(default=None, max_length=150)
    consent_evidence: Optional[str] = Field(default=None)

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        normalized = normalize_e164_phone(value)
        if not is_valid_e164_phone(normalized):
            raise ValueError("Phone number must be in valid international E.164 format (e.g. +919876543210).")
        return normalized


class CustomerUpdate(BaseModel):
    """Schema for updating basic customer details."""

    name: Optional[str] = Field(default=None, max_length=255)
    conversation_status: Optional[str] = Field(default=None, max_length=50)


class ConsentUpdateRequest(BaseModel):
    """Schema for recording explicit customer WhatsApp opt-in or preference updates."""

    marketing_consent: Optional[bool] = None
    utility_consent: Optional[bool] = None
    collection_method: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="How consent was collected (e.g. whatsapp_inbound_optin, web_checkout_checkbox, signed_pos_form)",
    )
    source: str = Field(
        ...,
        min_length=2,
        max_length=150,
        description="Where consent originated (e.g. kap_website_order_4821, store_counter_delhi)",
    )
    evidence: str = Field(
        ...,
        min_length=3,
        description="Audit proof of consent (e.g. timestamped form payload, customer opt-in message ID)",
    )


class OptOutRequest(BaseModel):
    """Schema for recording a customer opt-out request."""

    categories: str = Field(
        default="ALL",
        description="Categories to opt out from: ALL, MARKETING, or UTILITY",
    )
    reason: Optional[str] = Field(default="Customer requested opt-out", max_length=255)
    source: str = Field(default="admin_api", max_length=100)


class ConsentRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    consent_type: str
    action: str
    granted: bool
    collection_method: str
    source: str
    evidence: Optional[str] = None
    recorded_by: str
    created_at: datetime


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone_number: str
    name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    last_interaction_at: Optional[datetime] = None
    last_inbound_message_at: Optional[datetime] = None
    conversation_status: str
    marketing_consent: bool
    utility_consent: bool
    consent_timestamp: Optional[datetime] = None
    consent_method: Optional[str] = None
    consent_source: Optional[str] = None
    consent_evidence: Optional[str] = None
    opted_out: bool
    opted_out_categories: Optional[str] = None
    opt_out_timestamp: Optional[datetime] = None
    opt_out_reason: Optional[str] = None
    is_anonymized: bool
    service_window_open: bool = False


class CustomerHistoryResponse(BaseModel):
    customer: CustomerResponse
    consent_history: list[ConsentRecordResponse]
    messages: list[dict]
    conversations: list[dict]
