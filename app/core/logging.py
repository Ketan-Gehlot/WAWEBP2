"""Structured logging with automatic secret redaction and PII minimization."""
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings

SENSITIVE_FIELD_NAMES = {
    "access_token",
    "meta_access_token",
    "app_secret",
    "meta_app_secret",
    "verify_token",
    "meta_webhook_verify_token",
    "password",
    "admin_password",
    "secret_key",
    "admin_secret_key",
    "authorization",
}

BEARER_REGEX = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)


def mask_phone_number(phone: Optional[str]) -> str:
    """Mask phone number for logs to avoid retaining unnecessary personal information."""
    if not phone:
        return "unknown"
    cleaned = str(phone).strip()
    if len(cleaned) <= 6:
        return "***"
    return f"{cleaned[:3]}***{cleaned[-4:]}"


def redact_sensitive_data(data: Any) -> Any:
    """Recursively redact sensitive fields and tokens from dictionaries, lists, and strings."""
    if isinstance(data, dict):
        sanitized = {}
        for k, v in data.items():
            if str(k).lower() in SENSITIVE_FIELD_NAMES:
                sanitized[k] = "***REDACTED***"
            elif str(k).lower() in {"phone", "phone_number", "recipient_phone", "wa_id", "from", "to"}:
                sanitized[k] = mask_phone_number(str(v)) if v else v
            else:
                sanitized[k] = redact_sensitive_data(v)
        return sanitized
    if isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    if isinstance(data, str):
        text = BEARER_REGEX.sub(r"\1***REDACTED***", data)
        for secret_val in (
            settings.META_ACCESS_TOKEN,
            settings.META_APP_SECRET,
            settings.META_WEBHOOK_VERIFY_TOKEN,
            settings.ADMIN_SECRET_KEY,
            settings.ADMIN_PASSWORD,
        ):
            if secret_val and len(secret_val) >= 6 and secret_val in text:
                text = text.replace(secret_val, "***REDACTED***")
        return text
    return data


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line structured JSON with secret scrubbing."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event_type", "application_log"),
            "message": redact_sensitive_data(record.getMessage()),
        }
        extra_context = getattr(record, "context", None)
        if extra_context:
            payload["context"] = redact_sensitive_data(extra_context)
        if record.exc_info:
            payload["exception"] = redact_sensitive_data(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def get_logger(name: str = "kap.whatsapp") -> logging.Logger:
    """Configure and return a structured logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG if settings.APP_DEBUG else logging.INFO)
        logger.propagate = False
    return logger


logger = get_logger()


def log_structured_event(
    event_type: str,
    message: str,
    level: int = logging.INFO,
    **context: Any,
) -> None:
    """Emit a structured, scrubbed log entry for compliance, security, and operational monitoring."""
    logger.log(
        level,
        message,
        extra={
            "event_type": event_type,
            "context": redact_sensitive_data(context),
        },
    )
