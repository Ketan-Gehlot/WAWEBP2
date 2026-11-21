"""Health check and Administrator Authentication API endpoints."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.core.compliance import ComplianceEngine
from app.core.security import (
    create_access_token,
    ensure_default_admin,
    record_audit_log,
    verify_password,
)
from app.database.connection import get_db
from app.models.system import AdminUser
from app.schemas.common import LoginRequest, TokenResponse

router = APIRouter(tags=["Health & Authentication"])


@router.get("/health", summary="System health and compliance readiness status")
def health_check(db: Session = Depends(get_db)) -> dict:
    """Return service health, database connectivity, and safety flag statuses."""
    db_healthy = False
    try:
        db.execute(text("SELECT 1"))
        db_healthy = True
    except Exception:
        db_healthy = False

    state = ComplianceEngine.get_or_create_state(db)
    return {
        "status": "healthy" if db_healthy else "degraded",
        "service": settings.APP_NAME,
        "environment": settings.APP_ENV,
        "database_connected": db_healthy,
        "outbound_messages_enabled": settings.ENABLE_OUTBOUND_MESSAGES,
        "dry_run_mode": settings.DRY_RUN,
        "global_kill_switch": bool(settings.GLOBAL_KILL_SWITCH or state.global_kill_switch),
        "account_quality_status": state.account_quality_status,
        "meta_api_version": settings.META_GRAPH_API_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.post(
    "/api/auth/login",
    response_model=TokenResponse,
    summary="Authenticate administrator and obtain JWT bearer token",
)
def admin_login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """Authenticate an administrator and return a signed JWT access token."""
    ensure_default_admin(db)
    user = (
        db.query(AdminUser)
        .filter(AdminUser.username == payload.username, AdminUser.is_active.is_(True))
        .first()
    )
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrator username or password.",
        )

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    token = create_access_token(subject=user.username, role=user.role)
    record_audit_log(
        db=db,
        actor=user.username,
        action="admin_login",
        resource_type="auth",
        ip_address=request.client.host if request.client else None,
    )
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        role=user.role,
        expires_in_minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES,
    )
