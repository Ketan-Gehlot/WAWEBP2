"""Conservative, deterministic rate limiter for outbound WhatsApp messaging and API protection."""
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
    Enforces Meta account and phone-number messaging limits conservatively.
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

    def check_and_reserve(
        self,
        db: Optional[Session] = None,
        max_per_minute: Optional[int] = None,
        max_per_day: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Check whether sending one outbound message is within per-minute and daily limits.
        If allowed, reserves a slot immediately.
        """
        now = datetime.now(timezone.utc)
        minute_limit = max_per_minute if max_per_minute is not None else settings.MAX_MESSAGES_PER_MINUTE
        daily_limit = max_per_day if max_per_day is not None else settings.MAX_DAILY_MESSAGES

        with self._lock:
            self._prune(now)

            if len(self._minute_timestamps) >= minute_limit:
                return (
                    False,
                    f"RATE_LIMIT_EXCEEDED: Per-minute messaging limit ({minute_limit}/min) reached.",
                )

            if len(self._daily_timestamps) >= daily_limit:
                return (
                    False,
                    f"DAILY_LIMIT_EXCEEDED: 24-hour messaging limit ({daily_limit}/day) reached.",
                )

            # Also cross-check persisted outbound sends in database if session provided
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

    def get_usage_snapshot(self) -> dict:
        """Return current rate limiter counters."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._prune(now)
            return {
                "current_minute_sends": len(self._minute_timestamps),
                "max_messages_per_minute": settings.MAX_MESSAGES_PER_MINUTE,
                "current_daily_sends": len(self._daily_timestamps),
                "max_daily_messages": settings.MAX_DAILY_MESSAGES,
            }


outbound_rate_limiter = OutboundRateLimiter()
