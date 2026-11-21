"""Conversation SQLAlchemy ORM model."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Conversation(Base):
    """Tracks customer support threads, 24-hour service windows, and human escalation status."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(50), default="open", nullable=False, index=True
    )  # open, escalated_to_human, resolved, closed
    assigned_agent: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    escalation_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subject_context: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    last_customer_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    window_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    customer = relationship("Customer", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation")

    __table_args__ = (
        Index("ix_conversations_customer_status", "customer_id", "status"),
    )
