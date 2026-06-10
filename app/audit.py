"""Audit log helper — record significant admin/system actions."""
from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AuditLog, User, utcnow


def log_action(
    db: Session,
    actor: User | None,
    action: str,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    db.add(AuditLog(
        actor_id=actor.id if actor else None,
        actor_name=actor.name if actor else "system",
        action=action,
        target=target,
        detail=detail,
        created_at=utcnow(),
    ))
    db.commit()
