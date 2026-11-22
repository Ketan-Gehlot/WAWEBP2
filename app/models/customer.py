"""Customer SQLAlchemy ORM model."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    """Represents a Maxis Gold Jewellery customer and their explicit WhatsApp consent profile."""

    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    last_interaction_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation_status: Mapped[str] = mapped_column(
        String(50), default="new", nullable=False, index=True
    )  # new, active, escalated, closed, anonymized

    # Explicit consent tracking (default False - never assume imported contact is opted in)
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    utility_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    consent_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_method: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    consent_source: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    consent_evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Opt-out enforcement
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    opted_out_categories: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # ALL, MARKETING, UTILITY
    opt_out_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    is_anonymized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    conversations = relationship("Conversation", back_populates="customer", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="customer", cascade="all, delete-orphan")
    consent_records = relationship("ConsentRecord", back_populates="customer", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_customers_consent_eligibility", "opted_out", "marketing_consent", "utility_consent"),
    )
