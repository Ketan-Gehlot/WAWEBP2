"""Conservative, deterministic rate limiter and Meta 24-hour unique-recipient portfolio limit enforcer."""
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models.message import Message


class OutboundRateLimiter:
    """
    Enforces Meta account, phone-number, and rolling 24-hour unique-recipient portfolio limits.
    Never uses random delays to evade enforcement and never automatically increases volume.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._minute_timestamps: deque[datetime] = deque()
        self._daily_timestamps: deque[datetime] = deque()

    def reset(self) -> None:
        """Clear in-memory sliding window counters (used in automated tests)."""
        with self._lock:
            self._minute_timestamps.clear()
            self._daily_timestamps.clear()

    def _prune(self, now: datetime) -> None:
        one_minute_ago = now - timedelta(seconds=60)
        one_day_ago = now - timedelta(hours=24)
        while self._minute_timestamps and self._minute_timestamps[0] <= one_minute_ago:
            self._minute_timestamps.popleft()
        while self._daily_timestamps and self._daily_timestamps[0] <= one_day_ago:
            self._daily_timestamps.popleft()

    def get_portfolio_capacity(
        self,
        db: Session,
        unique_limit: Optional[int] = None,
    ) -> dict:
        """
        Calculate current rolling 24-hour unique-recipient portfolio usage and remaining capacity.
        """
        now = datetime.now(timezone.utc)
        one_day_ago = now - timedelta(hours=24)
        limit = (
            unique_limit
            if unique_limit is not None
            else settings.META_24H_UNIQUE_RECIPIENT_LIMIT
        )
        active_statuses = ("sent", "delivered", "read", "dry_run")

        rows = (
            db.query(Message.recipient_phone)
            .filter(
                Message.direction == "outbound",
                Message.status.in_(active_statuses),
                Message.created_at >= one_day_ago,
            )
            .distinct()
            .all()
        )
        unique_recipients = {r[0] for r in rows if r and r[0]}
        used_count = len(unique_recipients)
        remaining = max(0, limit - used_count)
        return {
            "portfolio_24h_unique_recipient_limit": limit,
            "unique_recipients_last_24h": used_count,
            "remaining_portfolio_capacity": remaining,
            "active_unique_recipients": unique_recipients,
        }

    def check_rolling_24h_unique_recipient_capacity(
        self,
        db: Session,
        recipient_phone: str,
        unique_limit: Optional[int] = None,
    ) -> tuple[bool, int, Optional[str]]:
        """
        Enforce Meta's rolling 24-hour unique-recipient portfolio limit before every send.
        If recipient_phone has already been messaged in the last 24 hours, sending another
        message to the same recipient does not consume an additional unique-recipient slot.
        If recipient_phone is a new unique recipient in the rolling 24h window, requires
        remaining_portfolio_capacity >= 1.
        """
        snapshot = self.get_portfolio_capacity(db=db, unique_limit=unique_limit)
        limit = snapshot["portfolio_24h_unique_recipient_limit"]
        remaining = snapshot["remaining_portfolio_capacity"]
        active_set = snapshot["active_unique_recipients"]

        if recipient_phone in active_set:
            return True, remaining, None

        if remaining <= 0:
            return (
                False,
                0,
                (
                    f"PORTFOLIO_24H_UNIQUE_RECIPIENT_LIMIT_REACHED: Meta rolling 24-hour unique-recipient "
                    f"limit ({limit} unique recipients/24h) has been reached (0 remaining capacity)."
                ),
            )

        return True, remaining - 1, None

    def check_and_reserve(
        self,
        db: Optional[Session] = None,
        recipient_phone: Optional[str] = None,
        max_per_minute: Optional[int] = None,
        max_per_day: Optional[int] = None,
        unique_recipient_limit: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Check whether sending one outbound message is within per-minute, daily,
        and rolling 24-hour unique-recipient portfolio limits.
        If allowed, reserves a slot immediately.
        """
        now = datetime.now(timezone.utc)
        minute_limit = max_per_minute if max_per_minute is not None else settings.MAX_MESSAGES_PER_MINUTE
        daily_limit = max_per_day if max_per_day is not None else settings.MAX_DAILY_MESSAGES

        with self._lock:
            self._prune(now)

            # 1. Check rolling 24-hour unique-recipient portfolio capacity first if db is provided
            if db is not None and recipient_phone:
                unique_ok, _, unique_err = self.check_rolling_24h_unique_recipient_capacity(
                    db=db,
                    recipient_phone=recipient_phone,
                    unique_limit=unique_recipient_limit,
                )
                if not unique_ok:
                    return False, unique_err

            # 2. Check per-minute limit
            if len(self._minute_timestamps) >= minute_limit:
                return (
                    False,
                    f"RATE_LIMIT_EXCEEDED: Per-minute messaging limit ({minute_limit}/min) reached.",
                )

            # 3. Check 24-hour total messages limit
            if len(self._daily_timestamps) >= daily_limit:
                return (
                    False,
                    f"DAILY_LIMIT_EXCEEDED: 24-hour messaging limit ({daily_limit}/day) reached.",
                )

            # 4. Cross-check persisted outbound sends in database if session provided
            if db is not None:
                one_minute_ago = now - timedelta(seconds=60)
                one_day_ago = now - timedelta(hours=24)
                active_statuses = ("sent", "delivered", "read", "dry_run")

                db_minute_count = (
                    db.query(func.count(Message.id))
                    .filter(
                        Message.direction == "outbound",
                        Message.status.in_(active_statuses),
                        Message.created_at >= one_minute_ago,
                    )
                    .scalar()
                    or 0
                )
                if db_minute_count >= minute_limit:
                    return (
                        False,
                        f"RATE_LIMIT_EXCEEDED: Database recorded {db_minute_count} sends in the last minute (limit {minute_limit}).",
                    )

                db_daily_count = (
                    db.query(func.count(Message.id))
                    .filter(
                        Message.direction == "outbound",
                        Message.status.in_(active_statuses),
                        Message.created_at >= one_day_ago,
                    )
                    .scalar()
                    or 0
                )
                if db_daily_count >= daily_limit:
                    return (
                        False,
                        f"DAILY_LIMIT_EXCEEDED: Database recorded {db_daily_count} sends in the last 24h (limit {daily_limit}).",
                    )

            self._minute_timestamps.append(now)
            self._daily_timestamps.append(now)
            return True, None

    def get_usage_snapshot(self, db: Optional[Session] = None) -> dict:
        """Return current rate limiter counters and rolling 24h unique-recipient capacity."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune(now)
            snapshot = {
                "current_minute_sends": len(self._minute_timestamps),
                "max_messages_per_minute": settings.MAX_MESSAGES_PER_MINUTE,
                "current_daily_sends": len(self._daily_timestamps),
                "max_daily_messages": settings.MAX_DAILY_MESSAGES,
                "portfolio_24h_unique_recipient_limit": settings.META_24H_UNIQUE_RECIPIENT_LIMIT,
            }
            if db is not None:
                cap = self.get_portfolio_capacity(db)
                snapshot["unique_recipients_last_24h"] = cap["unique_recipients_last_24h"]
                snapshot["remaining_portfolio_capacity"] = cap["remaining_portfolio_capacity"]
            return snapshot


outbound_rate_limiter = OutboundRateLimiter()
