import csv
import hashlib
import io
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, selectinload

from ..auth import get_current_user, require_user
from ..config import settings
from ..database import get_db
from ..gpu_metrics import fetch_gpu_metrics
from ..models import (
    ApiToken, Gpu, GpuAccess, GpuStatus, GpuVisibility, Notification,
    Reservation, ReservationStatus, Role, User, Watch, utcnow,
)
from ..queue_logic import estimated_wait_seconds, user_can_access_gpu
from ..settings_store import get_setting

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


def _dashboard_context(request: Request, db: Session, user) -> dict:
    now = utcnow()
    all_gpus = db.query(Gpu).order_by(Gpu.name).all()

    if user.role == Role.PI:
        gpus = all_gpus
    else:
        grant_gpu_ids = {
            gid for (gid,) in db.query(GpuAccess.gpu_id).filter(GpuAccess.user_id == user.id).all()
        }
        gpus = [
            g for g in all_gpus
            if g.visibility == GpuVisibility.PUBLIC or g.id in grant_gpu_ids
        ]

    gpu_ids = [g.id for g in gpus]

    # Single query for all live reservations across every accessible GPU
    live_res = (
        db.query(Reservation)
        .options(selectinload(Reservation.user))
        .filter(
            Reservation.gpu_id.in_(gpu_ids),
            Reservation.status.in_([
                ReservationStatus.ACTIVE, ReservationStatus.OFFERED,
                ReservationStatus.QUEUED, ReservationStatus.SCHEDULED,
            ]),
        )
        .order_by(Reservation.priority.asc(), Reservation.queue_sort_key.asc())
        .all()
    ) if gpu_ids else []

    gpu_active: dict[int, Reservation] = {}
    gpu_offered: dict[int, Reservation] = {}
    gpu_queued: dict[int, list] = defaultdict(list)
    gpu_scheduled: dict[int, list] = defaultdict(list)

    for r in live_res:
        if r.status == ReservationStatus.ACTIVE:
            gpu_active.setdefault(r.gpu_id, r)
        elif r.status == ReservationStatus.OFFERED:
            gpu_offered.setdefault(r.gpu_id, r)
        elif r.status == ReservationStatus.QUEUED:
            gpu_queued[r.gpu_id].append(r)
        elif r.status == ReservationStatus.SCHEDULED:
            gpu_scheduled[r.gpu_id].append(r)

    for gid in gpu_scheduled:
        gpu_scheduled[gid].sort(key=lambda r: (
            r.scheduled_start_at.replace(tzinfo=timezone.utc)
            if r.scheduled_start_at and r.scheduled_start_at.tzinfo is None
            else (r.scheduled_start_at or datetime(9999, 1, 1, tzinfo=timezone.utc))
        ))

    watched_ids = {
        w.gpu_id for w in db.query(Watch).filter(Watch.user_id == user.id).all()
    }

    gpu_views = [
        {
            "gpu": g,
            "active": gpu_active.get(g.id),
            "offered": gpu_offered.get(g.id),
            "queue": gpu_queued[g.id],
            "scheduled": gpu_scheduled[g.id],
            "watched": g.id in watched_ids,
            "tags": [t.strip() for t in g.tags.split(",")] if g.tags else [],
        }
        for g in gpus
    ]

    my_reservations = (
        db.query(Reservation)
        .options(selectinload(Reservation.gpu))
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
    my_scheduled = [r for r in my_reservations if r.status == ReservationStatus.SCHEDULED]
    my_queued = [r for r in my_reservations if r.status == ReservationStatus.QUEUED]

    # Compute wait estimates from already-loaded data — zero extra queries
    wait_info: dict[int, int] = {}
    for r in my_queued:
        current = gpu_active.get(r.gpu_id) or gpu_offered.get(r.gpu_id)
        if current is None:
            wait_info[r.id] = 0
            continue
        base_secs = 0.0
        if current.expected_end_at:
            end_dt = current.expected_end_at
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            base_secs = max(0.0, (end_dt - now).total_seconds())
        for qr in gpu_queued[r.gpu_id]:
            if qr.id == r.id:
                break
            base_secs += qr.requested_hours * 3600
        wait_info[r.id] = int(base_secs)

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

    announcement = get_setting(db, "announcement_text") or ""

    return {
        "request": request,
        "user": user,
        "gpu_views": gpu_views,
        "my_reservations": my_reservations,
        "my_active": my_active,
        "my_pending": my_pending,
        "my_scheduled": my_scheduled,
        "my_queued": my_queued,
        "wait_info": wait_info,
        "notifications": notifications,
        "unread_count": unread_count,
        "settings": settings,
        "Role": Role,
        "ReservationStatus": ReservationStatus,
        "now_iso": utcnow().isoformat(),
        "announcement": announcement,
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
    if not user.is_approved:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "partials/dashboard_live.html", _dashboard_context(request, db, user)
    )


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    if not user.is_approved:
        return RedirectResponse(url="/dashboard", status_code=302)

    all_gpus = db.query(Gpu).order_by(Gpu.name).all()
    gpus = [g for g in all_gpus if user_can_access_gpu(db, user, g)]

    now = utcnow()
    window_hours = 7 * 24  # 7-day window
    window_end = now + timedelta(hours=window_hours)

    # Fetch reservations in the window
    relevant_statuses = [
        ReservationStatus.ACTIVE,
        ReservationStatus.OFFERED,
        ReservationStatus.SCHEDULED,
        ReservationStatus.QUEUED,
    ]
    all_res = (
        db.query(Reservation)
        .filter(Reservation.status.in_(relevant_statuses))
        .all()
    )

    # Build per-GPU blocks for the timeline
    gpu_blocks = {}
    for gpu in gpus:
        blocks = []
        gpu_res = [r for r in all_res if r.gpu_id == gpu.id]

        for r in gpu_res:
            if r.status in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED):
                start = r.started_at or now
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                end = r.expected_end_at or (start + timedelta(hours=r.requested_hours))
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                if end < now or start > window_end:
                    continue
                blocks.append({
                    "status": r.status.value,
                    "user": r.user.name,
                    "note": r.note or "",
                    "start_ts": max(start, now).timestamp(),
                    "end_ts": min(end, window_end).timestamp(),
                    "hours": r.requested_hours,
                })
            elif r.status == ReservationStatus.SCHEDULED and r.scheduled_start_at:
                start = r.scheduled_start_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                end = start + timedelta(hours=r.requested_hours)
                if end < now or start > window_end:
                    continue
                blocks.append({
                    "status": "SCHEDULED",
                    "user": r.user.name,
                    "note": r.note or "",
                    "start_ts": max(start, now).timestamp(),
                    "end_ts": min(end, window_end).timestamp(),
                    "hours": r.requested_hours,
                })
            elif r.status == ReservationStatus.QUEUED:
                # Estimate position after current session
                secs = estimated_wait_seconds(db, r)
                if secs is None:
                    continue
                est_start = now + timedelta(seconds=secs)
                est_end = est_start + timedelta(hours=r.requested_hours)
                if est_end < now or est_start > window_end:
                    continue
                blocks.append({
                    "status": "QUEUED",
                    "user": r.user.name,
                    "note": r.note or "(estimated)",
                    "start_ts": max(est_start, now).timestamp(),
                    "end_ts": min(est_end, window_end).timestamp(),
                    "hours": r.requested_hours,
                })
        gpu_blocks[gpu.id] = blocks

    return templates.TemplateResponse("calendar.html", {
        "request": request,
        "user": user,
        "gpus": gpus,
        "gpu_blocks": gpu_blocks,
        "now_ts": now.timestamp(),
        "window_end_ts": window_end.timestamp(),
        "window_hours": window_hours,
        "now": now,
        "window_end": window_end,
        "timedelta": timedelta,
        "settings": settings,
        "unread_count": db.query(Notification)
            .filter(Notification.user_id == user.id, Notification.read.is_(False))
            .count(),
    })


@router.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request, db: Session = Depends(get_db), user=Depends(require_user)):
    if not user.is_approved:
        return RedirectResponse(url="/dashboard", status_code=302)
    tokens = db.query(ApiToken).filter(ApiToken.user_id == user.id).all()
    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read.is_(False))
        .count()
    )
    return templates.TemplateResponse("profile.html", {
        "request": request,
        "user": user,
        "tokens": tokens,
        "settings": settings,
        "unread_count": unread_count,
        "new_token": request.query_params.get("new_token"),
    })


@router.post("/profile")
def profile_post(
    name: str = Form(""),
    email_on_offer: str = Form("off"),
    email_on_warning: str = Form("off"),
    email_on_watch: str = Form("off"),
    email_on_queue_move: str = Form("off"),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    if name.strip():
        user.name = name.strip()
    user.email_on_offer = email_on_offer == "on"
    user.email_on_warning = email_on_warning == "on"
    user.email_on_watch = email_on_watch == "on"
    user.email_on_queue_move = email_on_queue_move == "on"
    db.commit()
    return RedirectResponse(url="/profile?msg=Preferences+saved", status_code=303)


@router.post("/profile/tokens/create")
def create_token(
    token_name: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    db.add(ApiToken(user_id=user.id, name=token_name.strip() or "My Token", token_hash=token_hash))
    db.commit()
    return RedirectResponse(url=f"/profile?new_token={raw}", status_code=303)


@router.post("/profile/tokens/{tid}/delete")
def delete_token(tid: int, db: Session = Depends(get_db), user=Depends(require_user)):
    t = db.get(ApiToken, tid)
    if t and t.user_id == user.id:
        db.delete(t)
        db.commit()
    return RedirectResponse(url="/profile", status_code=303)


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
            "unread_count": db.query(Notification)
                .filter(Notification.user_id == user.id, Notification.read.is_(False))
                .count(),
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
            r.id, r.gpu.name, r.status.value, f"{r.requested_hours:g}",
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
