"""Authentication, password hashing, webhook HMAC verification, RBAC, and audit logging."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.config import settings
from app.core.logging import log_structured_event
from app.database.connection import get_db
from app.models.system import AdminUser, AuditLog

bearer_scheme = HTTPBearer(auto_error=False)
basic_scheme = HTTPBasic(auto_error=False)


def hash_password(plain_password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    password_bytes = plain_password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        return False


def create_access_token(
    subject: str,
    role: str = "superadmin",
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Generate a signed JWT token for an authenticated admin user."""
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(to_encode, settings.ADMIN_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token."""
    try:
        return jwt.decode(
            token,
            settings.ADMIN_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication token has expired.",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials.",
        ) from exc


def verify_webhook_token(mode: Optional[str], token: Optional[str]) -> bool:
    """Securely verify Meta GET /webhook challenge request."""
    if mode != "subscribe" or not token:
        return False
    expected = settings.META_WEBHOOK_VERIFY_TOKEN
    if not expected:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def compute_webhook_signature(raw_body: bytes, app_secret: str) -> str:
    """Compute X-Hub-Signature-256 header value for a raw webhook payload."""
    digest = hmac.new(
        app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Validate Meta X-Hub-Signature-256 header using HMAC-SHA256 and constant-time comparison."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    app_secret = settings.META_APP_SECRET
    if not app_secret:
        return False
    expected_signature = compute_webhook_signature(raw_body, app_secret)
    return hmac.compare_digest(
        signature_header.strip().encode("utf-8"),
        expected_signature.encode("utf-8"),
    )


def ensure_default_admin(db: Session) -> AdminUser:
    """Ensure the configured default administrator account exists in the database."""
    admin = db.query(AdminUser).filter(AdminUser.username == settings.ADMIN_USERNAME).first()
    if not admin:
        admin = AdminUser(
            username=settings.ADMIN_USERNAME,
            password_hash=hash_password(settings.ADMIN_PASSWORD),
            role="superadmin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
    return admin


def record_audit_log(
    db: Session,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    """Persist an administrative audit log entry and emit a structured log."""
    entry = AuditLog(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        details=details or {},
        ip_address=ip_address,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    log_structured_event(
        "admin_action",
        f"Admin '{actor}' performed '{action}' on {resource_type}",
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
    )
    return entry


def get_current_admin(
    request: Request,
    bearer_creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    basic_creds: Optional[HTTPBasicCredentials] = Depends(basic_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    """Authenticate an administrator via Bearer JWT token or HTTP Basic credentials."""
    ensure_default_admin(db)

    # 1. Check Bearer JWT token
    if bearer_creds and bearer_creds.credentials:
        payload = decode_access_token(bearer_creds.credentials)
        username = payload.get("sub")
        if not username:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload.",
            )
        user = (
            db.query(AdminUser)
            .filter(AdminUser.username == username, AdminUser.is_active.is_(True))
            .first()
        )
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Admin user not found or inactive.",
            )
        return user

    # 2. Check HTTP Basic credentials
    if basic_creds and basic_creds.username and basic_creds.password:
        user = (
            db.query(AdminUser)
            .filter(AdminUser.username == basic_creds.username, AdminUser.is_active.is_(True))
            .first()
        )
        if user and verify_password(basic_creds.password, user.password_hash):
            return user

        # Also check environment fallback if username matches settings.ADMIN_USERNAME
        if (
            secrets.compare_digest(basic_creds.username, settings.ADMIN_USERNAME)
            and secrets.compare_digest(basic_creds.password, settings.ADMIN_PASSWORD)
        ):
            user = db.query(AdminUser).filter(AdminUser.username == settings.ADMIN_USERNAME).first()
            if user and user.is_active:
                return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required to access administrative endpoints.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_roles(*allowed_roles: str) -> Callable:
    """FastAPI dependency enforcing Role-Based Access Control (RBAC)."""

    def _role_checker(current_admin: AdminUser = Depends(get_current_admin)) -> AdminUser:
        if current_admin.role not in allowed_roles and current_admin.role != "superadmin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required roles: {', '.join(allowed_roles)}",
            )
        return current_admin

    return _role_checker
