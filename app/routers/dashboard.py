import csv
import io
from collections import defaultdict
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import get_current_user, require_user
from ..config import settings
from ..database import get_db
from ..models import Gpu, Notification, Reservation, ReservationStatus, Role, Watch, utcnow
from ..queue_logic import queue_for_gpu, user_can_access_gpu

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _gpu_view(db: Session, gpu: Gpu, watched_ids: set[int]) -> dict:
    active = next(
        (r for r in gpu.reservations if r.status == ReservationStatus.ACTIVE), None
    )
    offered = next(
        (r for r in gpu.reservations if r.status == ReservationStatus.OFFERED), None
    )
    q = queue_for_gpu(db, gpu.id)
    return {
        "gpu": gpu,
        "active": active,
        "offered": offered,
        "queue": q,
        "watched": gpu.id in watched_ids,
    }


def _dashboard_context(request: Request, db: Session, user) -> dict:
    all_gpus = db.query(Gpu).order_by(Gpu.name).all()
    gpus = [g for g in all_gpus if user_can_access_gpu(db, user, g)]
    watched_ids = {
        w.gpu_id for w in db.query(Watch).filter(Watch.user_id == user.id).all()
    }
    gpu_views = [_gpu_view(db, g, watched_ids) for g in gpus]
    my_reservations = (
        db.query(Reservation)
        .filter(Reservation.user_id == user.id)
        .order_by(Reservation.created_at.desc())
        .limit(20)
        .all()
    )
    my_active = next(
        (r for r in my_reservations if r.status in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED)),
        None,
    )
    my_pending = next(
        (r for r in my_reservations if r.status == ReservationStatus.PENDING_APPROVAL),
        None,
    )
    notifications = (
        db.query(Notification)
        .filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(10)
        .all()
    )
    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read.is_(False))
        .count()
    )
    return {
        "request": request,
        "user": user,
        "gpu_views": gpu_views,
        "my_reservations": my_reservations,
        "my_active": my_active,
        "my_pending": my_pending,
        "notifications": notifications,
        "unread_count": unread_count,
        "settings": settings,
        "Role": Role,
        "ReservationStatus": ReservationStatus,
        "now_iso": utcnow().isoformat(),
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "user": None, "settings": settings})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    if not user.is_approved:
        return templates.TemplateResponse(
            "pending_account.html",
            {"request": request, "user": user, "settings": settings},
        )
    return templates.TemplateResponse("dashboard.html", _dashboard_context(request, db, user))


@router.get("/dashboard/partial", response_class=HTMLResponse)
def dashboard_partial(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    """HTMX polling target — re-renders the live-data section of the dashboard."""
    if not user.is_approved:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "partials/dashboard_live.html", _dashboard_context(request, db, user)
    )


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    if not user.is_approved:
        return RedirectResponse(url="/dashboard", status_code=302)

    rows = (
        db.query(Reservation)
        .filter(Reservation.user_id == user.id)
        .order_by(Reservation.created_at.desc())
        .all()
    )

    # Per-day usage for the last 14 days, for a tiny sparkline.
    days_back = 14
    today = utcnow().date()
    day_keys = [(today - timedelta(days=i)) for i in range(days_back - 1, -1, -1)]
    per_day = defaultdict(float)
    for r in rows:
        if r.started_at is None or r.ended_at is None:
            continue
        if r.status not in (ReservationStatus.COMPLETED, ReservationStatus.EXPIRED, ReservationStatus.ACTIVE):
            continue
        start = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=timezone.utc)
        end = r.ended_at if r.ended_at.tzinfo else r.ended_at.replace(tzinfo=timezone.utc)
        per_day[start.date()] += max(0.0, (end - start).total_seconds() / 3600.0)
    chart = [(d.strftime("%a %d"), round(per_day[d], 2)) for d in day_keys]
    max_h = max([v for _, v in chart] + [1.0])

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "user": user,
            "rows": rows,
            "chart": chart,
            "chart_max": max_h,
            "settings": settings,
            "ReservationStatus": ReservationStatus,
        },
    )


@router.get("/history.csv")
def history_csv(db: Session = Depends(get_db), user=Depends(require_user)):
    if not user.is_approved:
        return RedirectResponse(url="/dashboard", status_code=302)
    rows = (
        db.query(Reservation)
        .filter(Reservation.user_id == user.id)
        .order_by(Reservation.created_at.desc())
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "gpu", "status", "requested_hours", "created_at", "started_at", "ended_at", "note"])
    for r in rows:
        w.writerow([
            r.id,
            r.gpu.name,
            r.status.value,
            f"{r.requested_hours:g}",
            r.created_at.isoformat() if r.created_at else "",
            r.started_at.isoformat() if r.started_at else "",
            r.ended_at.isoformat() if r.ended_at else "",
            (r.note or "").replace("\n", " "),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="my-gpu-history.csv"'},
    )


@router.post("/notifications/{nid}/read")
def mark_read(nid: int, db: Session = Depends(get_db), user=Depends(require_user)):
    n = db.get(Notification, nid)
    if n and n.user_id == user.id:
        n.read = True
        db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/notifications/read-all")
def mark_all_read(db: Session = Depends(get_db), user=Depends(require_user)):
    (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read.is_(False))
        .update({Notification.read: True})
    )
    db.commit()
    return RedirectResponse(url="/dashboard", status_code=303)
