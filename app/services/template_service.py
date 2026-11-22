"""
WhatsApp Message Template Registry Service.

Manages approved, pending, paused, and rejected templates and synchronizes
with the official Meta Graph API when WABA credentials are configured.
"""
from datetime import datetime, timezone
import re
from typing import Optional
import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import log_structured_event
from app.models.template import MessageTemplate
from app.schemas.template import TemplateCreate, TemplateUpdate

DEFAULT_KAP_TEMPLATES = [
    {
        "meta_template_id": "maxis_tpl_order_update_001",
        "name": "kap_order_status_update",
        "language": "en_US",
        "category": "UTILITY",
        "approval_status": "APPROVED",
        "is_active": True,
        "quality_score": "GREEN",
        "header_text": "Maxis Gold Jewellery - Order Update",
        "body_text": "Hello {{1}}, your Maxis Gold Jewellery order #{{2}} status has been updated to: {{3}}.",
        "footer_text": "Reply STOP to opt out of notifications.",
        "variable_definitions": ["customer_name", "order_id", "order_status"],
    },
    {
        "meta_template_id": "maxis_tpl_service_reminder_002",
        "name": "kap_service_reminder",
        "language": "en_US",
        "category": "UTILITY",
        "approval_status": "APPROVED",
        "is_active": True,
        "quality_score": "GREEN",
        "header_text": "Maxis Gold Jewellery - Requested Reminder",
        "body_text": "Hi {{1}}, this is your requested reminder from Maxis Gold Jewellery regarding {{2}}.",
        "footer_text": "Reply STOP to opt out.",
        "variable_definitions": ["customer_name", "reminder_topic"],
    },
    {
        "meta_template_id": "maxis_tpl_festive_collection_003",
        "name": "kap_seasonal_parts_campaign",
        "language": "en_US",
        "category": "MARKETING",
        "approval_status": "APPROVED",
        "is_active": True,
        "quality_score": "GREEN",
        "header_text": "Maxis Gold Jewellery - Festive & Bridal Collection",
        "body_text": (
            "Hello {{1}}, explore our new BIS Hallmarked 22K Gold and Bridal Diamond collection "
            "at Maxis Gold Jewellery. Reply to book a private showroom viewing with our jewellery specialist."
        ),
        "footer_text": "Reply STOP to unsubscribe from marketing messages.",
        "variable_definitions": ["customer_name"],
    },
]


class TemplateService:
    """Service managing the template registry and synchronization."""

    @staticmethod
    def seed_default_templates(db: Session) -> None:
        """Seed or refresh baseline Maxis Gold Jewellery message templates."""
        now = datetime.now(timezone.utc)
        for tpl_data in DEFAULT_KAP_TEMPLATES:
            existing = (
                db.query(MessageTemplate)
                .filter(
                    MessageTemplate.name == tpl_data["name"],
                    MessageTemplate.language == tpl_data["language"],
                )
                .first()
            )
            if not existing:
                db.add(
                    MessageTemplate(
                        **tpl_data,
                        last_synced_at=now,
                    )
                )
            elif existing.body_text and "Kanti Auto Parts" in existing.body_text:
                existing.header_text = tpl_data["header_text"]
                existing.body_text = tpl_data["body_text"]
                existing.footer_text = tpl_data["footer_text"]
        db.commit()

    @staticmethod
    def list_templates(
        db: Session,
        category: Optional[str] = None,
        approval_status: Optional[str] = None,
        active_only: bool = False,
    ) -> list[MessageTemplate]:
        query = db.query(MessageTemplate)
        if category:
            query = query.filter(MessageTemplate.category == category.upper())
        if approval_status:
            query = query.filter(MessageTemplate.approval_status == approval_status.upper())
        if active_only:
            query = query.filter(MessageTemplate.is_active.is_(True))
        return query.order_by(MessageTemplate.name.asc()).all()

    @staticmethod
    def create_template(db: Session, payload: TemplateCreate) -> MessageTemplate:
        existing = (
            db.query(MessageTemplate)
            .filter(
                MessageTemplate.name == payload.name,
                MessageTemplate.language == payload.language,
            )
            .first()
        )
        if existing:
            raise ValueError(f"Template '{payload.name}' ({payload.language}) already exists.")

        template = MessageTemplate(
            meta_template_id=payload.meta_template_id,
            name=payload.name,
            language=payload.language,
            category=payload.category.upper(),
            approval_status=payload.approval_status.upper(),
            is_active=payload.is_active,
            quality_score=payload.quality_score.upper(),
            header_text=payload.header_text,
            body_text=payload.body_text,
            footer_text=payload.footer_text,
            variable_definitions=payload.variable_definitions,
            last_synced_at=datetime.now(timezone.utc),
        )
        db.add(template)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def update_template(
        db: Session,
        template: MessageTemplate,
        payload: TemplateUpdate,
    ) -> MessageTemplate:
        if payload.approval_status is not None:
            template.approval_status = payload.approval_status.upper()
        if payload.is_active is not None:
            template.is_active = payload.is_active
        if payload.quality_score is not None:
            template.quality_score = payload.quality_score.upper()
        if payload.body_text is not None:
            template.body_text = payload.body_text
        if payload.variable_definitions is not None:
            template.variable_definitions = payload.variable_definitions
        template.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(template)
        return template

    @staticmethod
    def sync_from_meta(db: Session) -> dict:
        """
        Synchronize message templates from the official Meta Graph API
        GET /{WABA_ID}/message_templates when credentials are provided.
        """
        if not settings.META_WABA_ID or not settings.META_ACCESS_TOKEN:
            return {
                "synced": False,
                "reason": "META_WABA_ID or META_ACCESS_TOKEN not configured; using local template registry.",
                "templates_updated": 0,
            }

        url = (
            f"{settings.META_GRAPH_BASE_URL.rstrip('/')}/"
            f"{settings.META_GRAPH_API_VERSION.strip('/')}/"
            f"{settings.META_WABA_ID.strip('/')}/message_templates"
        )
        headers = {"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"}
        now = datetime.now(timezone.utc)
        updated_count = 0

        with httpx.Client(timeout=settings.META_HTTP_TIMEOUT_SECONDS) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()

        for item in payload.get("data", []):
            name = item.get("name")
            language = item.get("language", "en_US")
            status = (item.get("status") or "PENDING").upper()
            category = (item.get("category") or "UTILITY").upper()
            meta_id = str(item.get("id", ""))
            quality = (item.get("quality_score", {}) or {}).get("score", "GREEN").upper()

            body_text = ""
            for comp in item.get("components", []):
                if comp.get("type") == "BODY":
                    body_text = comp.get("text", "")

            placeholder_matches = sorted(set(re.findall(r"\{\{(\d+)\}\}", body_text)))
            inferred_vars = [f"param_{idx}" for idx in placeholder_matches]

            existing = (
                db.query(MessageTemplate)
                .filter(MessageTemplate.name == name, MessageTemplate.language == language)
                .first()
            )
            if existing:
                existing.meta_template_id = meta_id or existing.meta_template_id
                existing.approval_status = status
                existing.category = category
                existing.quality_score = quality
                if body_text:
                    existing.body_text = body_text
                    existing.variable_definitions = inferred_vars
                existing.is_active = status == "APPROVED" and quality not in {"RED", "DISABLED"}
                existing.last_synced_at = now
            else:
                db.add(
                    MessageTemplate(
                        meta_template_id=meta_id,
                        name=name,
                        language=language,
                        category=category,
                        approval_status=status,
                        is_active=(status == "APPROVED"),
                        quality_score=quality,
                        body_text=body_text or f"Template {name}",
                        variable_definitions=inferred_vars,
                        last_synced_at=now,
                    )
                )
            updated_count += 1

        db.commit()
        log_structured_event(
            "templates_synced",
            f"Synchronized {updated_count} templates from Meta Graph API",
            templates_updated=updated_count,
        )
        return {
            "synced": True,
            "templates_updated": updated_count,
        }
