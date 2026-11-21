"""ConsentRecord SQLAlchemy ORM model."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConsentRecord(Base):
    """Immutable audit log of every customer consent opt-in, withdrawal, or admin review."""

    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consent_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # marketing, utility, all
    action: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # opt_in, opt_out, preference_update, admin_review
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    collection_method: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(150), nullable=False)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recorded_by: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    customer = relationship("Customer", back_populates="consent_records")

    __table_args__ = (
        Index("ix_consent_records_customer_type", "customer_id", "consent_type", "created_at"),
    )
