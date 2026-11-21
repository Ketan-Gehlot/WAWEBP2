"""MessageTemplate SQLAlchemy ORM model."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MessageTemplate(Base):
    """Registry of official Meta WhatsApp message templates and their approval/quality status."""

    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    meta_template_id: Mapped[Optional[str]] = mapped_column(String(120), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(20), default="en_US", nullable=False)
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # UTILITY, MARKETING, AUTHENTICATION
    approval_status: Mapped[str] = mapped_column(
        String(50), default="PENDING", nullable=False, index=True
    )  # APPROVED, PENDING, REJECTED, PAUSED, DISABLED
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    quality_score: Mapped[str] = mapped_column(
        String(30), default="GREEN", nullable=False
    )  # GREEN, YELLOW, RED, UNKNOWN

    header_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    footer_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    variable_definitions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("name", "language", name="uq_template_name_language"),
        Index("ix_templates_lookup", "name", "language", "approval_status", "is_active"),
    )
