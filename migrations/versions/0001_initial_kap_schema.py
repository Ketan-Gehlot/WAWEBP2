"""Initial KAP WhatsApp Automation relational database schema.

Revision ID: 0001_initial_kap_schema
Revises: None
Create Date: 2026-11-21
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_kap_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone_number", sa.String(length=20), nullable=False, unique=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inbound_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conversation_status", sa.String(length=50), nullable=False, server_default="new"),
        sa.Column("marketing_consent", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("utility_consent", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("consent_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_method", sa.String(length=100), nullable=True),
        sa.Column("consent_source", sa.String(length=150), nullable=True),
        sa.Column("consent_evidence", sa.Text(), nullable=True),
        sa.Column("opted_out", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("opted_out_categories", sa.String(length=100), nullable=True),
        sa.Column("opt_out_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_reason", sa.String(length=255), nullable=True),
        sa.Column("is_anonymized", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_customers_phone_number", "customers", ["phone_number"], unique=True)
    op.create_index(
        "ix_customers_consent_eligibility",
        "customers",
        ["opted_out", "marketing_consent", "utility_consent"],
    )

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="open"),
        sa.Column("assigned_agent", sa.String(length=120), nullable=True),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("subject_context", sa.String(length=255), nullable=True),
        sa.Column("last_customer_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_conversations_customer_status", "conversations", ["customer_id", "status"])

    op.create_table(
        "consent_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("consent_type", sa.String(length=50), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("collection_method", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=150), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_consent_records_customer_type", "consent_records", ["customer_id", "consent_type", "created_at"])

    op.create_table(
        "message_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("meta_template_id", sa.String(length=120), nullable=True, unique=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=False, server_default="en_US"),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("approval_status", sa.String(length=50), nullable=False, server_default="PENDING"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("quality_score", sa.String(length=30), nullable=False, server_default="GREEN"),
        sa.Column("header_text", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("footer_text", sa.Text(), nullable=True),
        sa.Column("variable_definitions", sa.JSON(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "language", name="uq_template_name_language"),
    )
    op.create_index("ix_templates_lookup", "message_templates", ["name", "language", "approval_status", "is_active"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("message_templates.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("default_template_params", sa.JSON(), nullable=False),
        sa.Column("audience_selection", sa.JSON(), nullable=False),
        sa.Column("total_contacts_selected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("eligible_recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exclusion_reasons", sa.JSON(), nullable=False),
        sa.Column("preview_completed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("previewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admin_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("confirmed_by", sa.String(length=120), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="draft"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("whatsapp_message_id", sa.String(length=150), nullable=True, unique=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Integer(), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("template_id", sa.Integer(), sa.ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("consent_record_id", sa.Integer(), sa.ForeignKey("consent_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("recipient_phone", sa.String(length=20), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("message_type", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("template_name", sa.String(length=150), nullable=True),
        sa.Column("template_language", sa.String(length=20), nullable=True),
        sa.Column("template_params", sa.JSON(), nullable=True),
        sa.Column("media_url", sa.String(length=500), nullable=True),
        sa.Column("media_caption", sa.Text(), nullable=True),
        sa.Column("interactive_payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("compliance_approved", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("compliance_rejection_reason", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_messages_direction_status", "messages", ["direction", "status", "created_at"])
    op.create_index("ix_messages_scheduled_lookup", "messages", ["status", "scheduled_at"])

    op.create_table(
        "campaign_recipients",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("phone_number", sa.String(length=30), nullable=False),
        sa.Column("template_params", sa.JSON(), nullable=True),
        sa.Column("is_eligible", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("exclusion_reason", sa.String(length=255), nullable=True),
        sa.Column("delivery_status", sa.String(length=40), nullable=False, server_default="pending"),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_campaign_recipients_campaign_eligible",
        "campaign_recipients",
        ["campaign_id", "is_eligible", "delivery_status"],
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(length=200), nullable=False, unique=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("whatsapp_id", sa.String(length=150), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("processing_status", sa.String(length=40), nullable=False),
        sa.Column("error_details", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "automation_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("rule_type", sa.String(length=50), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("requires_human_escalation", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="superadmin"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=120), nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=120), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_actor_action", "audit_logs", ["actor", "action", "created_at"])

    op.create_table(
        "compliance_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("global_kill_switch", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("account_quality_status", sa.String(length=30), nullable=False, server_default="GREEN"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=120), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("compliance_state")
    op.drop_table("audit_logs")
    op.drop_table("admin_users")
    op.drop_table("automation_rules")
    op.drop_table("webhook_events")
    op.drop_table("campaign_recipients")
    op.drop_table("messages")
    op.drop_table("campaigns")
    op.drop_table("message_templates")
    op.drop_table("consent_records")
    op.drop_table("conversations")
    op.drop_table("customers")
