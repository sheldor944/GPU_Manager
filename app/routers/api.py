"""REST API v1 — programmatic access via Bearer tokens.

Endpoints:
  GET  /api/v1/me                   — current user info
  GET  /api/v1/gpus                 — list all GPUs accessible to the user
  GET  /api/v1/me/reservations      — user's reservations (recent 50)
  POST /api/v1/reservations         — create a reservation
  DELETE /api/v1/reservations/{id}  — cancel a queued/scheduled reservation

Token management (web UI required):
  GET  /profile -> API Tokens section
"""
from __future__ import annotations

import secrets
import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..auth import require_api_user
from ..config import settings
from ..database import get_db
from ..models import ApiToken, Gpu, Reservation, ReservationStatus, User, utcnow
from ..queue_logic import (
    add_notification,
    cancel_queued,
    request_gpu,
    user_can_access_gpu,
)

router = APIRouter(prefix="/api/v1", tags=["api"])


def _user_json(u: User) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "role": u.role.value,
        "quota_hours": u.quota_hours,
        "used_hours": round(u.used_hours, 4),
        "remaining_hours": round(u.remaining_hours, 4),
        "over_quota": u.over_quota,
    }


def _gpu_json(g: Gpu) -> dict:
    return {
        "id": g.id,
        "name": g.name,
        "model": g.model,
        "host": g.host,
        "tags": [t.strip() for t in g.tags.split(",")] if g.tags else [],
        "status": g.status.value,
        "visibility": g.visibility.value,
        "max_hours": g.max_hours,
        "notes": g.notes,
    }


def _reservation_json(r: Reservation) -> dict:
    return {
        "id": r.id,
        "gpu_id": r.gpu_id,
        "gpu_name": r.gpu.name if r.gpu else None,
        "status": r.status.value,
        "requested_hours": r.requested_hours,
        "priority": r.priority,
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "scheduled_start_at": r.scheduled_start_at.isoformat() if r.scheduled_start_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "expected_end_at": r.expected_end_at.isoformat() if r.expected_end_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
    }


@router.get("/me")
def api_me(user: User = Depends(require_api_user)):
    return _user_json(user)


@router.get("/gpus")
def api_gpus(db: Session = Depends(get_db), user: User = Depends(require_api_user)):
    gpus = db.query(Gpu).order_by(Gpu.name).all()
    return [_gpu_json(g) for g in gpus if user_can_access_gpu(db, user, g)]


@router.get("/me/reservations")
def api_my_reservations(db: Session = Depends(get_db), user: User = Depends(require_api_user)):
    rows = (
        db.query(Reservation)
        .filter(Reservation.user_id == user.id)
        .order_by(Reservation.created_at.desc())
        .limit(50)
        .all()
    )
    return [_reservation_json(r) for r in rows]


@router.post("/reservations", status_code=201)
def api_create_reservation(
    body: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_api_user),
):
    """
    Body fields:
      gpu_id: int (required)
      hours: float (required)
      note: str (optional)
      scheduled_start_at: ISO8601 datetime string (optional — for future booking)

    Returns the created reservation.
    """
    gpu_id = body.get("gpu_id")
    hours = body.get("hours")
    if not gpu_id or not hours:
        raise HTTPException(400, "gpu_id and hours are required")
    gpu = db.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU not found")

    scheduled_start_at: Optional[datetime] = None
    raw_start = body.get("scheduled_start_at")
    if raw_start:
        try:
            scheduled_start_at = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
            if scheduled_start_at.tzinfo is None:
                scheduled_start_at = scheduled_start_at.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, "scheduled_start_at must be a valid ISO8601 datetime")

    try:
        res, msg = request_gpu(
            db, user, gpu, float(hours),
            note=body.get("note", ""),
            scheduled_start_at=scheduled_start_at,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    add_notification(db, user.id, msg, link="/dashboard")
    return _reservation_json(res)


@router.delete("/reservations/{rid}", status_code=200)
def api_cancel_reservation(
    rid: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_user),
):
    res = db.get(Reservation, rid)
    if res is None or res.user_id != user.id:
        raise HTTPException(404, "Reservation not found")
    try:
        cancel_queued(db, res)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "message": "Reservation cancelled"}


# ── Token management endpoints (also usable from API) ──────────────────────

@router.get("/tokens")
def api_list_tokens(db: Session = Depends(get_db), user: User = Depends(require_api_user)):
    tokens = db.query(ApiToken).filter(ApiToken.user_id == user.id).all()
    return [
        {
            "id": t.id,
            "name": t.name,
            "created_at": t.created_at.isoformat(),
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        }
        for t in tokens
    ]


@router.delete("/tokens/{tid}")
def api_delete_token(tid: int, db: Session = Depends(get_db), user: User = Depends(require_api_user)):
    t = db.get(ApiToken, tid)
    if t is None or t.user_id != user.id:
        raise HTTPException(404, "Token not found")
    db.delete(t)
    db.commit()
    return {"ok": True}
