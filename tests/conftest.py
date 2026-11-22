"""Pytest fixtures for KAP WhatsApp Automation Platform automated tests."""
from collections.abc import Generator
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.compliance import ComplianceEngine
from app.core.rate_limiter import outbound_rate_limiter
from app.core.security import create_access_token, ensure_default_admin
from app.database.base import Base
from app.database.connection import get_db
from app.main import app
from app.services.automation_service import AutomationService
from app.services.template_service import TemplateService

TEST_APP_SECRET = "test_meta_app_secret_key_for_hmac_256"
TEST_VERIFY_TOKEN = "test_meta_webhook_verify_token_2026"


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch) -> Generator[Session, None, None]:
    """Create an isolated in-memory SQLite database for each test function."""
    monkeypatch.setattr(settings, "APP_ENV", "testing")
    monkeypatch.setattr(settings, "META_APP_SECRET", TEST_APP_SECRET)
    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", TEST_VERIFY_TOKEN)
    monkeypatch.setattr(settings, "META_PHONE_NUMBER_ID", "100000000000001")
    monkeypatch.setattr(settings, "META_WABA_ID", "200000000000002")
    monkeypatch.setattr(settings, "META_ACCESS_TOKEN", "test_system_user_token_redacted")
    monkeypatch.setattr(settings, "ENABLE_OUTBOUND_MESSAGES", False)
    monkeypatch.setattr(settings, "DRY_RUN", True)
    monkeypatch.setattr(settings, "GLOBAL_KILL_SWITCH", False)
    monkeypatch.setattr(settings, "ACCOUNT_QUALITY_STATUS", "GREEN")
    monkeypatch.setattr(settings, "MAX_MESSAGES_PER_MINUTE", 30)
    monkeypatch.setattr(settings, "MAX_DAILY_MESSAGES", 250)
    monkeypatch.setattr(settings, "META_24H_UNIQUE_RECIPIENT_LIMIT", 250)

    outbound_rate_limiter.reset()

    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    @event.listens_for(test_engine, "connect")
    def _enable_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine, class_=Session)

    session = TestingSessionLocal()
    ensure_default_admin(session)
    ComplianceEngine.get_or_create_state(session)
    TemplateService.seed_default_templates(session)
    AutomationService.seed_default_rules(session)

    # Also patch SessionLocal in worker/main modules so background tasks use the same test DB
    monkeypatch.setattr("app.database.connection.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.workers.message_worker.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.main.SessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.main.init_db", lambda: None)

    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Provide an HTTPX-backed FastAPI TestClient wired to the isolated test DB session."""

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    """Return Bearer Authorization headers for an authenticated superadmin."""
    token = create_access_token(subject=settings.ADMIN_USERNAME, role="superadmin")
    return {"Authorization": f"Bearer {token}"}
