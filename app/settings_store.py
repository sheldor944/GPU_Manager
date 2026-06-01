"""Runtime, admin-toggleable settings stored in the AppSetting table."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AppSetting

REQUIRE_REQUEST_APPROVAL = "require_request_approval"
QUOTA_RESET_PERIOD = "quota_reset_period"          # "off" | "weekly" | "monthly"
QUOTA_LAST_RESET_AT = "quota_last_reset_at"        # ISO-8601 timestamp


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def get_bool(db: Session, key: str, default: bool = False) -> bool:
    v = get_setting(db, key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def set_bool(db: Session, key: str, value: bool) -> None:
    set_setting(db, key, "true" if value else "false")
