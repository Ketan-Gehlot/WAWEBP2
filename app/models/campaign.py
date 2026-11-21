"""Campaign and CampaignRecipient SQLAlchemy ORM models."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Campaign(Base):
    """Represents a consent-verified WhatsApp template campaign with mandatory dry-run preview."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)  # MARKETING, UTILITY

    template_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("message_templates.id", ondelete="RESTRICT"), nullable=False
    )
    default_template_params: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    audience_selection: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # Preview / Dry-Run metrics
    total_contacts_selected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    eligible_recipient_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    excluded_recipient_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exclusion_reasons: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    preview_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    previewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    admin_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(40), default="draft", nullable=False, index=True
    )  # draft, previewed, scheduled, executing, completed, cancelled, halted_compliance

    # Delivery results
    sent_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    read_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_by: Mapped[str] = mapped_column(String(120), default="admin", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    recipients = relationship(
        "CampaignRecipient", back_populates="campaign", cascade="all, delete-orphan"
    )
    template = relationship("MessageTemplate")


class CampaignRecipient(Base):
    """Per-recipient eligibility evaluation and delivery tracking for a campaign."""

    __tablename__ = "campaign_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    campaign_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    phone_number: Mapped[str] = mapped_column(String(30), nullable=False)
    template_params: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    is_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    delivery_status: Mapped[str] = mapped_column(
        String(40), default="pending", nullable=False, index=True
    )  # pending, scheduled, sent, delivered, read, failed, blocked_compliance, cancelled_opt_out, dry_run
    message_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign = relationship("Campaign", back_populates="recipients")

    __table_args__ = (
        Index("ix_campaign_recipients_campaign_eligible", "campaign_id", "is_eligible", "delivery_status"),
    )
