"""Message Pydantic v2 schemas."""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field


class MessageSendRequest(BaseModel):
    """Schema for sending or scheduling an outbound WhatsApp message through the Compliance Engine."""

    customer_id: Optional[int] = Field(default=None, description="Target Customer ID")
    recipient_phone: Optional[str] = Field(
        default=None, description="Target E.164 phone number (if customer_id not provided)"
    )
    message_type: Literal["text", "template", "image", "document", "audio", "video", "interactive"] = "text"
    category: Literal["service", "utility", "marketing", "authentication"] = "service"

    content: Optional[str] = Field(default=None, description="Text content for text messages")
    template_name: Optional[str] = Field(default=None, description="Approved Meta template name")
    template_language: str = Field(default="en_US", description="Template language code")
    template_params: Optional[list[str]] = Field(default=None, description="Ordered template body variable values")

    media_url: Optional[str] = Field(default=None, description="HTTPS URL for media messages")
    media_caption: Optional[str] = Field(default=None, description="Optional caption for media messages")
    interactive_payload: Optional[dict] = Field(default=None, description="Interactive message specification")

    scheduled_at: Optional[datetime] = Field(
        default=None,
        description="Optional future UTC timestamp to schedule this message (revalidated at send time)",
    )


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    whatsapp_message_id: Optional[str] = None
    customer_id: int
    conversation_id: Optional[int] = None
    campaign_id: Optional[int] = None
    template_id: Optional[int] = None
    recipient_phone: str
    direction: str
    message_type: str
    category: str
    content: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = None
    template_params: Optional[list] = None
    media_url: Optional[str] = None
    status: str
    compliance_approved: bool
    compliance_rejection_reason: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    read_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    created_at: datetime
