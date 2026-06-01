import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import require_pi
from ..config import settings
from ..database import get_db
from ..models import Gpu, GpuAccess, GpuStatus, GpuVisibility, Reservation, ReservationStatus, Role, User, utcnow
from ..email_utils import send_email
from ..queue_logic import (
    add_notification,
    approve_request,
    move_in_queue,
    queue_for_gpu,
    reject_request,
    release_reservation,
)
from ..quota_reset import get_status as quota_reset_status, reset_now as quota_reset_now, set_period as quota_reset_set_period
from ..settings_store import REQUIRE_REQUEST_APPROVAL, get_bool, set_bool

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _gpu_utilization(db: Session, gpus: list[Gpu], days: int = 7) -> list[dict]:
    """Return per-GPU utilization stats for the last `days` days."""
    cutoff = utcnow() - timedelta(days=days)
    out = []
    for gpu in gpus:
        used = 0.0
        rows = (
            db.query(Reservation)
            .filter(
                Reservation.gpu_id == gpu.id,
                Reservation.started_at.is_not(None),
                Reservation.started_at >= cutoff,
                Reservation.status.in_([
                    ReservationStatus.ACTIVE,
                    ReservationStatus.COMPLETED,
                    ReservationStatus.EXPIRED,
                ]),
            )
            .all()
        )
        for r in rows:
            if r.ended_at:
                start = r.started_at
                if start.tzinfo is None:
                    from datetime import timezone as _tz
                    start = start.replace(tzinfo=_tz.utc)
                end = r.ended_at
                if end.tzinfo is None:
                    end = end.replace(tzinfo=_tz.utc)
                used += max(0.0, (end - start).total_seconds() / 3600.0)
            else:
                # Active right now — count up to "now"
                start = r.started_at
                if start.tzinfo is None:
                    from datetime import timezone as _tz
                    start = start.replace(tzinfo=_tz.utc)
                used += max(0.0, (utcnow() - start).total_seconds() / 3600.0)
        total = days * 24.0
        out.append({
            "gpu": gpu,
            "used_h": round(used, 1),
            "total_h": total,
            "pct": round(100.0 * used / total, 1) if total else 0.0,
        })
    return out


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, db: Session = Depends(get_db), user=Depends(require_pi)):
    pending_users = (
        db.query(User)
        .filter(User.is_approved.is_(False), User.is_active.is_(True))
        .order_by(User.created_at.asc())
        .all()
    )
    users = (
        db.query(User)
        .filter(User.is_approved.is_(True))
        .order_by(User.role.desc(), User.name)
        .all()
    )
    gpus = db.query(Gpu).order_by(Gpu.name).all()
    pending = (
        db.query(Reservation)
        .filter(Reservation.status == ReservationStatus.PENDING_APPROVAL)
        .order_by(Reservation.created_at.asc())
        .all()
    )
    recent = (
        db.query(Reservation)
        .order_by(Reservation.created_at.desc())
        .limit(50)
        .all()
    )
    queues = []  # [(gpu, active_or_offered, [queued...]), ...]
    for gpu in gpus:
        active = next(
            (r for r in gpu.reservations if r.status in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED)),
            None,
        )
        q = queue_for_gpu(db, gpu.id)
        if active or q:
            queues.append((gpu, active, q))

    utilization = _gpu_utilization(db, gpus, days=7)
    require_approval = get_bool(db, REQUIRE_REQUEST_APPROVAL, default=False)
    quota_reset = quota_reset_status(db)

    # Build gpu_access_map: gpu_id → list of User objects granted access
    access_rows = db.query(GpuAccess).all()
    gpu_access_map: dict[int, list[User]] = {}
    for row in access_rows:
        u = db.get(User, row.user_id)
        if u:
            gpu_access_map.setdefault(row.gpu_id, []).append(u)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": user,
            "pending_users": pending_users,
            "users": users,
            "gpus": gpus,
            "queues": queues,
            "utilization": utilization,
            "pending": pending,
            "recent": recent,
            "require_approval": require_approval,
            "quota_reset": quota_reset,
            "gpu_access_map": gpu_access_map,
            "settings": settings,
            "Role": Role,
            "GpuStatus": GpuStatus,
            "GpuVisibility": GpuVisibility,
            "ReservationStatus": ReservationStatus,
        },
    )


@router.post("/settings/quota-reset-period")
def set_quota_reset_period(
    period: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    try:
        quota_reset_set_period(db, period)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/settings/quota-reset-now")
def quota_reset_now_endpoint(db: Session = Depends(get_db), user=Depends(require_pi)):
    n = quota_reset_now(db)
    return RedirectResponse(url=f"/admin?msg=Reset+used+hours+for+{n}+users", status_code=303)


@router.post("/users/{uid}/approve")
async def approve_user(uid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.is_approved:
        return RedirectResponse(url="/admin", status_code=303)
    target.is_approved = True
    target.is_active = True
    db.commit()
    add_notification(db, target.id, "Your account has been approved. Welcome!", link="/dashboard")
    await send_email(
        target.email,
        "[GPU Manager] Account approved",
        f"Hi {target.name},\n\nYour account on the lab GPU manager has been approved. "
        f"You can now request GPUs.\n\n{settings.BASE_URL}/dashboard\n",
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/reject")
async def reject_user(uid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.is_approved:
        raise HTTPException(400, "User is already approved; disable instead")
    target.is_active = False
    db.commit()
    await send_email(
        target.email,
        "[GPU Manager] Account not approved",
        f"Hi {target.name},\n\nYour account on the lab GPU manager was not "
        f"approved by the PI. If you think this is a mistake, please contact them directly.\n",
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/settings/require-approval")
def toggle_require_approval(
    enabled: str = Form("false"),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    set_bool(db, REQUIRE_REQUEST_APPROVAL, enabled.lower() in ("1", "true", "on", "yes"))
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/reservations/{rid}/approve")
async def approve(rid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    res = db.get(Reservation, rid)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    try:
        approve_request(db, res)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if res.status == ReservationStatus.ACTIVE:
        msg = f"Your request for {res.gpu.name} was approved — the GPU is yours now until {res.expected_end_at:%b %d %H:%M}."
    else:
        msg = f"Your request for {res.gpu.name} was approved — you're now in the queue."
    add_notification(db, res.user_id, msg, link="/dashboard")
    await send_email(
        res.user.email,
        f"[GPU Manager] Request approved for {res.gpu.name}",
        f"Hi {res.user.name},\n\n{msg}\n\n{settings.BASE_URL}/dashboard\n",
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/reservations/{rid}/reject")
async def reject(
    rid: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    res = db.get(Reservation, rid)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    try:
        reject_request(db, res, reason=reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    msg = f"Your request for {res.gpu.name} was rejected" + (f": {reason}" if reason else ".")
    add_notification(db, res.user_id, msg, link="/dashboard")
    await send_email(
        res.user.email,
        f"[GPU Manager] Request rejected for {res.gpu.name}",
        f"Hi {res.user.name},\n\n{msg}\n",
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/quota")
def set_quota(uid: int, quota: float = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    if quota < 0:
        raise HTTPException(400, "Quota cannot be negative")
    target.quota_hours = round(quota, 4)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/add-hours")
def add_hours(uid: int, hours: float = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    target.quota_hours = round(target.quota_hours + hours, 4)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/reset-used")
def reset_used(uid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    target.used_hours = 0.0
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/role")
def set_role(uid: int, role: str = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    if role not in Role.__members__:
        raise HTTPException(400, "Invalid role")
    target.role = Role[role]
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/toggle-active")
def toggle_active(uid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/add")
def add_gpu(
    name: str = Form(...),
    model: str = Form(""),
    host: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    if not name.strip():
        raise HTTPException(400, "GPU name required")
    if db.query(Gpu).filter(Gpu.name == name.strip()).first():
        raise HTTPException(400, "A GPU with that name already exists")
    gpu = Gpu(name=name.strip(), model=model or None, host=host or None, notes=notes or None)
    db.add(gpu)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/status")
def set_gpu_status(gid: int, status: str = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    if status not in GpuStatus.__members__:
        raise HTTPException(400, "Invalid status")
    # If forcing maintenance while in use, release active session.
    if status == "MAINTENANCE":
        active = next((r for r in gpu.reservations if r.status == ReservationStatus.ACTIVE), None)
        if active:
            release_reservation(db, active, by_admin=True)
    gpu.status = GpuStatus[status]
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/delete")
def delete_gpu(gid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    # Cancel queued + release active.
    for r in list(gpu.reservations):
        if r.status == ReservationStatus.ACTIVE:
            release_reservation(db, r, by_admin=True)
        elif r.status in (ReservationStatus.QUEUED, ReservationStatus.OFFERED):
            r.status = ReservationStatus.CANCELLED
    db.delete(gpu)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/visibility")
def set_gpu_visibility(
    gid: int,
    visibility: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    if visibility not in GpuVisibility.__members__:
        raise HTTPException(400, "Invalid visibility")
    gpu.visibility = GpuVisibility[visibility]
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/access/add")
def grant_gpu_access(
    gid: int,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    gpu = db.get(Gpu, gid)
    target = db.get(User, user_id)
    if gpu is None or target is None:
        raise HTTPException(404, "Not found")
    existing = (
        db.query(GpuAccess)
        .filter(GpuAccess.gpu_id == gid, GpuAccess.user_id == user_id)
        .first()
    )
    if not existing:
        db.add(GpuAccess(gpu_id=gid, user_id=user_id))
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/access/remove")
def revoke_gpu_access(
    gid: int,
    user_id: int = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    existing = (
        db.query(GpuAccess)
        .filter(GpuAccess.gpu_id == gid, GpuAccess.user_id == user_id)
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/reservations/{rid}/force-release")
def force_release(rid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    res = db.get(Reservation, rid)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    if res.status not in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED):
        raise HTTPException(400, "Not an active or offered reservation")
    release_reservation(db, res, by_admin=True)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/queue/{rid}/move")
def queue_move(
    rid: int,
    direction: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    res = db.get(Reservation, rid)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    d = -1 if direction == "up" else 1 if direction == "down" else 0
    if d == 0:
        raise HTTPException(400, "direction must be 'up' or 'down'")
    try:
        move_in_queue(db, res, d)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(url="/admin/activity", status_code=303)


@router.get("/activity", response_class=HTMLResponse)
def admin_activity(
    request: Request,
    user_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    users = db.query(User).order_by(User.name).all()
    gpus = db.query(Gpu).order_by(Gpu.name).all()
    q = db.query(Reservation).order_by(Reservation.created_at.desc())
    if user_id:
        q = q.filter(Reservation.user_id == user_id)
    history = q.limit(500).all()

    queues = []
    for gpu in gpus:
        active = next(
            (r for r in gpu.reservations if r.status in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED)),
            None,
        )
        qd = queue_for_gpu(db, gpu.id)
        queues.append((gpu, active, qd))

    return templates.TemplateResponse(
        "admin_activity.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "gpus": gpus,
            "selected_user_id": user_id,
            "history": history,
            "queues": queues,
            "settings": settings,
            "ReservationStatus": ReservationStatus,
        },
    )


def _reservation_csv(rows: list[Reservation]) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "user", "user_email", "gpu", "status", "requested_hours",
        "priority", "created_at", "started_at", "ended_at", "expected_end_at", "note",
    ])
    for r in rows:
        w.writerow([
            r.id,
            r.user.name if r.user else "",
            r.user.email if r.user else "",
            r.gpu.name if r.gpu else "",
            r.status.value,
            f"{r.requested_hours:g}",
            r.priority,
            r.created_at.isoformat() if r.created_at else "",
            r.started_at.isoformat() if r.started_at else "",
            r.ended_at.isoformat() if r.ended_at else "",
            r.expected_end_at.isoformat() if r.expected_end_at else "",
            (r.note or "").replace("\n", " "),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="activity.csv"'},
    )


@router.get("/activity.csv")
def admin_activity_csv(
    user_id: int | None = None,
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    q = db.query(Reservation).order_by(Reservation.created_at.desc())
    if user_id:
        q = q.filter(Reservation.user_id == user_id)
    return _reservation_csv(q.all())
