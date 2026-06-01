"""Periodic quota reset: zero everyone's used_hours on a configurable cadence."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .models import User, utcnow
from .settings_store import (
    QUOTA_LAST_RESET_AT,
    QUOTA_RESET_PERIOD,
    get_setting,
    set_setting,
)

logger = logging.getLogger(__name__)

PERIOD_DAYS = {"weekly": 7, "monthly": 30}


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_status(db: Session) -> dict:
    period = (get_setting(db, QUOTA_RESET_PERIOD, "off") or "off").lower()
    last = _parse_iso(get_setting(db, QUOTA_LAST_RESET_AT))
    next_at = None
    if period in PERIOD_DAYS and last is not None:
        next_at = last + timedelta(days=PERIOD_DAYS[period])
    return {"period": period, "last": last, "next": next_at}


def reset_now(db: Session) -> int:
    """Zero used_hours for every active user. Returns the row count."""
    count = db.query(User).filter(User.is_active.is_(True)).update({User.used_hours: 0.0})
    set_setting(db, QUOTA_LAST_RESET_AT, utcnow().isoformat())
    db.commit()
    return count


def set_period(db: Session, period: str) -> None:
    period = period.lower()
    if period not in ("off", "weekly", "monthly"):
        raise ValueError("period must be off, weekly, or monthly")
    set_setting(db, QUOTA_RESET_PERIOD, period)
    # If turning it on for the first time, anchor "last reset" to now so the
    # first auto-reset fires exactly one period from now (not immediately).
    if period in PERIOD_DAYS and get_setting(db, QUOTA_LAST_RESET_AT) is None:
        set_setting(db, QUOTA_LAST_RESET_AT, utcnow().isoformat())


def maybe_run(db: Session) -> bool:
    """Called periodically by the scheduler. Returns True if a reset happened."""
    status = get_status(db)
    if status["period"] not in PERIOD_DAYS:
        return False
    last = status["last"]
    if last is None:
        # Period is on but anchor missing; set to now so we don't reset right away.
        set_setting(db, QUOTA_LAST_RESET_AT, utcnow().isoformat())
        db.commit()
        return False
    if utcnow() - last >= timedelta(days=PERIOD_DAYS[status["period"]]):
        n = reset_now(db)
        logger.info("Auto quota reset zeroed %d users (period=%s)", n, status["period"])
        return True
    return False
