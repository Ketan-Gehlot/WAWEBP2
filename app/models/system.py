"""System models: WebhookEvent, AutomationRule, AdminUser, AuditLog, and ComplianceState."""
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WebhookEvent(Base):
    """Tracks processed Meta webhook events to ensure strict idempotency."""

    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # message, status, system
    whatsapp_id: Mapped[Optional[str]] = mapped_column(String(150), nullable=True, index=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(40), default="processed", nullable=False)
    error_details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AutomationRule(Base):
    """Configurable keyword and intent routing rules for automated customer support."""

    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    rule_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # greeting, product_inquiry, location, order_inquiry, faq, escalation
    keywords: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    requires_human_escalation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class AdminUser(Base):
    """Administrative user with hashed credentials and role-based permissions."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(50), default="superadmin", nullable=False
    )  # superadmin, manager, support_agent, auditor
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(Base):
    """Immutable audit log recording administrative actions and critical security events."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_logs_actor_action", "actor", "action", "created_at"),
    )


class ComplianceState(Base):
    """Persisted runtime compliance state for emergency kill switch and account quality status."""

    __tablename__ = "compliance_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    global_kill_switch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    account_quality_status: Mapped[str] = mapped_column(String(30), default="GREEN", nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(120), default="system", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
