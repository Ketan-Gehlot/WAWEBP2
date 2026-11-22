"""Application configuration and environment validation."""
from functools import lru_cache
from typing import Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized, validated application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    APP_ENV: Literal["development", "testing", "staging", "production"] = "development"
    APP_NAME: str = "Maxis Gold Jewellery WhatsApp Automation"
    APP_DEBUG: bool = False

    DATABASE_URL: str = "sqlite:///./kap_whatsapp.db"

    # Official Meta WhatsApp Cloud API Settings
    META_GRAPH_BASE_URL: str = "https://graph.facebook.com"
    META_GRAPH_API_VERSION: str = "v21.0"
    META_PHONE_NUMBER_ID: str = ""
    META_WABA_ID: str = ""
    META_ACCESS_TOKEN: str = ""
    META_APP_SECRET: str = ""
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    META_HTTP_TIMEOUT_SECONDS: float = 15.0

    # Background Tasks & Caching
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_TASK_ALWAYS_EAGER: bool = False

    # Safety & Compliance Controls (Safe Defaults: Outbound Disabled & Dry Run Enabled)
    ENABLE_OUTBOUND_MESSAGES: bool = False
    DRY_RUN: bool = True
    GLOBAL_KILL_SWITCH: bool = False
    ACCOUNT_QUALITY_STATUS: Literal["GREEN", "YELLOW", "RED", "RESTRICTED", "PAUSED"] = "GREEN"

    # Conservative Rate & Volume Limits
    MAX_MESSAGES_PER_MINUTE: int = Field(default=30, ge=1, le=1000)
    MAX_DAILY_MESSAGES: int = Field(default=250, ge=1, le=100000)
    META_24H_UNIQUE_RECIPIENT_LIMIT: int = Field(default=250, ge=1, le=1000000)
    MAX_CAMPAIGN_RECIPIENTS: int = Field(default=500, ge=1, le=10000)

    # Maxis Gold Jewellery Business Configuration
    BUSINESS_NAME: str = "Maxis Gold Jewellery"
    BUSINESS_GREETING: str = (
        "Hello! Welcome to Maxis Gold Jewellery. How can we help you today?"
    )
    BUSINESS_HOURS_START: str = "09:00"
    BUSINESS_HOURS_END: str = "18:00"
    BUSINESS_TIMEZONE: str = "Asia/Kolkata"

    # Administrative Authentication & Security
    ADMIN_SECRET_KEY: str = "kap-dev-only-secret-key-change-in-production-32chars"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "KAP_Admin_Secure_2026!"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    @field_validator("META_GRAPH_API_VERSION", mode="before")
    @classmethod
    def ensure_api_version(cls, value: str) -> str:
        if not value or not str(value).strip():
            return "v21.0"
        cleaned = str(value).strip()
        return cleaned if cleaned.startswith("v") else f"v{cleaned}"

    @field_validator("ADMIN_SECRET_KEY", mode="before")
    @classmethod
    def ensure_admin_secret(cls, value: str) -> str:
        if not value or not str(value).strip():
            return "kap-dev-only-secret-key-change-in-production-32chars"
        return str(value).strip()

    @field_validator("ADMIN_USERNAME", mode="before")
    @classmethod
    def ensure_admin_username(cls, value: str) -> str:
        if not value or not str(value).strip():
            return "admin"
        return str(value).strip()

    @field_validator("ADMIN_PASSWORD", mode="before")
    @classmethod
    def ensure_admin_password(cls, value: str) -> str:
        if not value or not str(value).strip():
            return "KAP_Admin_Secure_2026!"
        return str(value).strip()

    def masked_dict(self) -> dict:
        """Return settings dictionary with all sensitive secrets redacted."""
        sensitive_keys = {
            "META_ACCESS_TOKEN",
            "META_APP_SECRET",
            "META_WEBHOOK_VERIFY_TOKEN",
            "ADMIN_SECRET_KEY",
            "ADMIN_PASSWORD",
            "DATABASE_URL",
        }
        data = self.model_dump()
        for key in sensitive_keys:
            if key in data and data[key]:
                data[key] = "***REDACTED***"
        return data


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()


settings = get_settings()
