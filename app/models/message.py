"""Message SQLAlchemy ORM model."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(Base):
    """Stores inbound and outbound WhatsApp messages, compliance verdicts, and delivery statuses."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    whatsapp_message_id: Mapped[Optional[str]] = mapped_column(
        String(150), unique=True, nullable=True, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    template_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True
    )
    consent_record_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("consent_records.id", ondelete="SET NULL"), nullable=True
    )

    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # inbound, outbound
    message_type: Mapped[str] = mapped_column(
        String(30), default="text", nullable=False
    )  # text, template, image, document, audio, video, interactive
    category: Mapped[str] = mapped_column(
        String(30), default="service", nullable=False, index=True
    )  # service, utility, marketing, authentication

    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    template_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    template_language: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    template_params: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    media_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    media_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    interactive_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(
        String(40), default="queued", nullable=False, index=True
    )
    # Status values: received, queued, scheduled, sent, delivered, read, failed,
    # blocked_compliance, cancelled_opt_out, dry_run

    compliance_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    compliance_rejection_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    error_code: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    customer = relationship("Customer", back_populates="messages")
    conversation = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        Index("ix_messages_direction_status", "direction", "status", "created_at"),
        Index("ix_messages_scheduled_lookup", "status", "scheduled_at"),
    )
