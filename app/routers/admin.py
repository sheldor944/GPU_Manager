import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..audit import log_action
from ..auth import require_pi
from ..config import settings
from ..database import get_db
from ..email_utils import send_email
from ..gpu_metrics import check_host_reachable, fetch_gpu_metrics, fetch_live_stats, fetch_server_specs
from ..models import (
    AuditLog, Gpu, GpuAccess, GpuMetricSample, GpuStatus, GpuVisibility,
    Notification, Reservation, ReservationStatus, Role, User, utcnow,
)
from ..queue_logic import (
    add_notification,
    approve_request,
    move_in_queue,
    queue_for_gpu,
    reject_request,
    release_reservation,
)
from ..quota_reset import get_status as quota_reset_status, reset_now as quota_reset_now, set_period as quota_reset_set_period
from ..settings_store import REQUIRE_REQUEST_APPROVAL, get_bool, get_setting, set_bool, set_setting
from ..webhooks import WEBHOOK_URL_KEY, send_webhook

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _gpu_utilization(db: Session, gpus: list[Gpu], days: int = 7) -> list[dict]:
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
                from datetime import timezone as _tz
                start = r.started_at
                if start.tzinfo is None:
                    start = start.replace(tzinfo=_tz.utc)
                end = r.ended_at
                if end.tzinfo is None:
                    end = end.replace(tzinfo=_tz.utc)
                used += max(0.0, (end - start).total_seconds() / 3600.0)
            else:
                from datetime import timezone as _tz
                start = r.started_at
                if start.tzinfo is None:
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
    queues = []
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

    gpu_metrics: dict = {}

    # GPU access map
    access_rows = db.query(GpuAccess).all()
    gpu_access_map: dict[int, list[User]] = {}
    for row in access_rows:
        u = db.get(User, row.user_id)
        if u:
            gpu_access_map.setdefault(row.gpu_id, []).append(u)

    webhook_url = get_setting(db, WEBHOOK_URL_KEY) or ""
    announcement = get_setting(db, "announcement_text") or ""

    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read.is_(False))
        .count()
    )

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
            "gpu_metrics": gpu_metrics,
            "webhook_url": webhook_url,
            "announcement": announcement,
            "settings": settings,
            "Role": Role,
            "GpuStatus": GpuStatus,
            "GpuVisibility": GpuVisibility,
            "ReservationStatus": ReservationStatus,
            "unread_count": unread_count,
        },
    )


# ── Settings ──────────────────────────────────────────────────────────────

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
    log_action(db, user, "settings.quota_reset_period", detail=f"period={period}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/settings/quota-reset-now")
def quota_reset_now_endpoint(db: Session = Depends(get_db), user=Depends(require_pi)):
    n = quota_reset_now(db)
    log_action(db, user, "settings.quota_reset_now", detail=f"reset {n} users")
    return RedirectResponse(url=f"/admin?msg=Reset+used+hours+for+{n}+users", status_code=303)


@router.post("/settings/require-approval")
def toggle_require_approval(
    enabled: str = Form("false"),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    val = enabled.lower() in ("1", "true", "on", "yes")
    set_bool(db, REQUIRE_REQUEST_APPROVAL, val)
    log_action(db, user, "settings.require_approval", detail=f"enabled={val}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/settings/webhook")
def set_webhook(
    webhook_url: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    set_setting(db, WEBHOOK_URL_KEY, webhook_url.strip())
    log_action(db, user, "settings.webhook", detail=f"url={'set' if webhook_url.strip() else 'cleared'}")
    return RedirectResponse(url="/admin?msg=Webhook+URL+saved", status_code=303)


@router.post("/settings/announcement")
def set_announcement(
    announcement_text: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    set_setting(db, "announcement_text", announcement_text.strip())
    log_action(db, user, "settings.announcement", detail=announcement_text[:100])
    return RedirectResponse(url="/admin?msg=Announcement+updated", status_code=303)


# ── Broadcast ──────────────────────────────────────────────────────────────

@router.post("/broadcast")
async def broadcast(
    message: str = Form(...),
    send_email_flag: str = Form("off"),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    all_users = db.query(User).filter(User.is_approved.is_(True), User.is_active.is_(True)).all()
    count = 0
    for u in all_users:
        add_notification(db, u.id, f"[Announcement] {message}", link="/dashboard")
        count += 1
    log_action(db, user, "broadcast", detail=message[:200])
    await send_webhook(f"📢 Announcement from {user.name}: {message}", db=db)
    if send_email_flag == "on":
        for u in all_users:
            try:
                await send_email(
                    u.email,
                    "[GPU Manager] Announcement",
                    f"Message from the PI ({user.name}):\n\n{message}\n",
                )
            except Exception:
                pass
    return RedirectResponse(url=f"/admin?msg=Sent+to+{count}+users", status_code=303)


# ── Users ──────────────────────────────────────────────────────────────────

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
    log_action(db, user, "user.approve", target=f"user:{uid} ({target.email})")
    await send_email(
        target.email,
        "[GPU Manager] Account approved",
        f"Hi {target.name},\n\nYour account on the lab GPU manager has been approved. "
        f"You can now request GPUs.\n\n{settings.BASE_URL}/dashboard\n",
    )
    await send_webhook(f"✅ New user approved: {target.name} ({target.email})", db=db)
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
    log_action(db, user, "user.reject", target=f"user:{uid} ({target.email})")
    await send_email(
        target.email,
        "[GPU Manager] Account not approved",
        f"Hi {target.name},\n\nYour account on the lab GPU manager was not "
        f"approved by the PI. If you think this is a mistake, please contact them directly.\n",
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/quota")
def set_quota(uid: int, quota: float = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    if quota < 0:
        raise HTTPException(400, "Quota cannot be negative")
    old = target.quota_hours
    target.quota_hours = round(quota, 4)
    db.commit()
    log_action(db, user, "user.set_quota", target=f"user:{uid} ({target.email})", detail=f"{old} → {quota}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/add-hours")
def add_hours(uid: int, hours: float = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    target.quota_hours = round(target.quota_hours + hours, 4)
    db.commit()
    log_action(db, user, "user.add_hours", target=f"user:{uid} ({target.email})", detail=f"+{hours}h")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/reset-used")
def reset_used(uid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    old = target.used_hours
    target.used_hours = 0.0
    db.commit()
    log_action(db, user, "user.reset_used", target=f"user:{uid} ({target.email})", detail=f"was {old:.2f}h")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/role")
def set_role(uid: int, role: str = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    if role not in Role.__members__:
        raise HTTPException(400, "Invalid role")
    old = target.role.value
    target.role = Role[role]
    db.commit()
    log_action(db, user, "user.set_role", target=f"user:{uid} ({target.email})", detail=f"{old} → {role}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/toggle-active")
def toggle_active(uid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404, "User not found")
    target.is_active = not target.is_active
    db.commit()
    log_action(db, user, "user.toggle_active", target=f"user:{uid} ({target.email})", detail=f"active={target.is_active}")
    return RedirectResponse(url="/admin", status_code=303)


# ── GPUs ──────────────────────────────────────────────────────────────────

@router.post("/gpus/probe")
def probe_gpu_server(
    host: str = Form(""),
    ssh_user: str = Form(""),
    ssh_port: str = Form(""),
    ssh_password: str = Form(""),
    user=Depends(require_pi),
):
    """SSH into a server and return its GPU/CPU/RAM specs as JSON."""
    if not host.strip():
        raise HTTPException(400, "Host is required")
    sp = None
    if ssh_port.strip():
        try:
            sp = int(ssh_port.strip())
        except ValueError:
            raise HTTPException(400, "SSH port must be an integer")
    specs = fetch_server_specs(
        host.strip(),
        ssh_user=ssh_user.strip() or None,
        ssh_port=sp,
        ssh_password=ssh_password.strip() or None,
    )
    return specs


@router.post("/gpus/add")
def add_gpu(
    name: str = Form(...),
    model: str = Form(""),
    host: str = Form(""),
    ssh_user: str = Form(""),
    ssh_port: str = Form(""),
    ssh_password: str = Form(""),
    notes: str = Form(""),
    tags: str = Form(""),
    max_hours: str = Form(""),
    gpu_index: str = Form("0"),
    cpu_model: str = Form(""),
    ram_gb: str = Form(""),
    connect_instructions: str = Form(""),
    is_remote: str = Form("false"),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    if not name.strip():
        raise HTTPException(400, "GPU name required")
    if db.query(Gpu).filter(Gpu.name == name.strip()).first():
        raise HTTPException(400, "A GPU with that name already exists")
    mh = None
    if max_hours.strip():
        try:
            mh = float(max_hours.strip())
        except ValueError:
            raise HTTPException(400, "max_hours must be a number")
    sp = None
    if ssh_port.strip():
        try:
            sp = int(ssh_port.strip())
        except ValueError:
            raise HTTPException(400, "SSH port must be an integer")
    gi = 0
    if gpu_index.strip():
        try:
            gi = int(gpu_index.strip())
        except ValueError:
            raise HTTPException(400, "gpu_index must be an integer")
    rb = None
    if ram_gb.strip():
        try:
            rb = int(ram_gb.strip())
        except ValueError:
            raise HTTPException(400, "ram_gb must be an integer")
    gpu = Gpu(
        name=name.strip(),
        model=model or None,
        host=host or None,
        ssh_user=ssh_user or None,
        ssh_port=sp,
        ssh_password=ssh_password or None,
        notes=notes or None,
        tags=tags or None,
        max_hours=mh,
        gpu_index=gi,
        cpu_model=cpu_model.strip() or None,
        ram_gb=rb,
        connect_instructions=connect_instructions.strip() or None,
        is_remote=is_remote.lower() in ("1", "true", "on", "yes"),
        visibility=GpuVisibility.RESTRICTED,
    )
    db.add(gpu)
    db.commit()
    log_action(db, user, "gpu.add", target=f"gpu:{gpu.id} ({gpu.name})")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/edit")
def edit_gpu(
    gid: int,
    model: str = Form(""),
    host: str = Form(""),
    ssh_user: str = Form(""),
    ssh_port: str = Form(""),
    ssh_password: str = Form(""),
    notes: str = Form(""),
    tags: str = Form(""),
    max_hours: str = Form(""),
    cpu_model: str = Form(""),
    ram_gb: str = Form(""),
    connect_instructions: str = Form(""),
    is_remote: str = Form("false"),
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    mh = None
    if max_hours.strip():
        try:
            mh = float(max_hours.strip())
        except ValueError:
            raise HTTPException(400, "max_hours must be a number")
    sp = None
    if ssh_port.strip():
        try:
            sp = int(ssh_port.strip())
        except ValueError:
            raise HTTPException(400, "SSH port must be an integer")
    rb = None
    if ram_gb.strip():
        try:
            rb = int(ram_gb.strip())
        except ValueError:
            raise HTTPException(400, "ram_gb must be an integer")
    gpu.model = model or None
    gpu.host = host or None
    gpu.ssh_user = ssh_user or None
    gpu.ssh_port = sp
    if ssh_password.strip():
        gpu.ssh_password = ssh_password
    gpu.notes = notes or None
    gpu.tags = tags or None
    gpu.max_hours = mh
    gpu.cpu_model = cpu_model.strip() or None
    gpu.ram_gb = rb
    gpu.connect_instructions = connect_instructions.strip() or None
    gpu.is_remote = is_remote.lower() in ("1", "true", "on", "yes")
    db.commit()
    log_action(db, user, "gpu.edit", target=f"gpu:{gid} ({gpu.name})")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/status")
def set_gpu_status(gid: int, status: str = Form(...), db: Session = Depends(get_db), user=Depends(require_pi)):
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    if status not in GpuStatus.__members__:
        raise HTTPException(400, "Invalid status")
    if status == "MAINTENANCE":
        from ..models import utcnow as _utcnow
        now = _utcnow()
        active = next((r for r in gpu.reservations if r.status == ReservationStatus.ACTIVE), None)
        if active:
            release_reservation(db, active, by_admin=True)
        offered = next((r for r in gpu.reservations if r.status == ReservationStatus.OFFERED), None)
        if offered:
            offered.status = ReservationStatus.CANCELLED
            offered.ended_at = now
            from ..queue_logic import add_notification as _add_notif
            _add_notif(db, offered.user_id,
                       f"Your offer for {gpu.name} was cancelled — GPU entered maintenance.",
                       link="/dashboard")
            db.commit()
    old = gpu.status.value
    gpu.status = GpuStatus[status]
    db.commit()
    log_action(db, user, "gpu.set_status", target=f"gpu:{gid} ({gpu.name})", detail=f"{old} → {status}")
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
    old = gpu.visibility.value
    gpu.visibility = GpuVisibility[visibility]
    db.commit()
    log_action(db, user, "gpu.set_visibility", target=f"gpu:{gid} ({gpu.name})", detail=f"{old} → {visibility}")
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
    log_action(db, user, "gpu.grant_access", target=f"gpu:{gid} ({gpu.name})", detail=f"user:{user_id} ({target.email})")
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
        gpu = db.get(Gpu, gid)
        target = db.get(User, user_id)
        db.delete(existing)
        db.commit()
        log_action(db, user, "gpu.revoke_access",
                   target=f"gpu:{gid} ({gpu.name if gpu else '?'})",
                   detail=f"user:{user_id} ({target.email if target else '?'})")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/gpus/{gid}/delete")
def delete_gpu(gid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    for r in list(gpu.reservations):
        if r.status == ReservationStatus.ACTIVE:
            release_reservation(db, r, by_admin=True)
        elif r.status in (ReservationStatus.QUEUED, ReservationStatus.OFFERED, ReservationStatus.SCHEDULED):
            r.status = ReservationStatus.CANCELLED
        # all reservations (including completed/historical) are removed via cascade
    log_action(db, user, "gpu.delete", target=f"gpu:{gid} ({gpu.name})")
    db.delete(gpu)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ── GPU live metrics ───────────────────────────────────────────────────────

@router.get("/gpus/{gid}/metrics")
def gpu_live_metrics(gid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    """Return JSON of live nvidia-smi metrics for a GPU."""
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    metrics = fetch_gpu_metrics(gpu.host, gpu.gpu_index or 0, ssh_user=gpu.ssh_user, ssh_port=gpu.ssh_port, ssh_password=gpu.ssh_password)
    if metrics is None:
        raise HTTPException(503, "Metrics unavailable — nvidia-smi not accessible for this GPU")
    return metrics


@router.get("/analytics/reachable-count", response_class=HTMLResponse)
def analytics_reachable_count(request: Request, db: Session = Depends(get_db), user=Depends(require_pi)):
    """Return an HTML fragment showing how many GPUs actually return live metrics (nvidia-smi check)."""
    from concurrent.futures import ThreadPoolExecutor
    gpus = db.query(Gpu).order_by(Gpu.name).all()
    # Only check GPUs with SSH configured and not flagged as remote-PC
    ssh_gpus = [g for g in gpus if g.host and not g.is_remote]
    total = len(gpus)

    def _check(g: Gpu) -> tuple[bool, str]:
        m = fetch_gpu_metrics(
            g.host, g.gpu_index or 0,
            ssh_user=g.ssh_user, ssh_port=g.ssh_port, ssh_password=g.ssh_password,
        )
        return (m is not None, g.name)

    offline_names: list[str] = []
    reachable = 0
    if ssh_gpus:
        with ThreadPoolExecutor(max_workers=min(len(ssh_gpus), 8)) as pool:
            for ok, name in pool.map(_check, ssh_gpus):
                if ok:
                    reachable += 1
                else:
                    offline_names.append(name)

    if reachable == total:
        color = "text-green-600"
    elif reachable == 0:
        color = "text-red-600"
    else:
        color = "text-amber-600"

    offline_html = ""
    if offline_names:
        badges = "".join(
            f'<span class="inline-block text-[10px] px-1.5 py-0.5 rounded bg-red-100 dark:bg-red-900 '
            f'text-red-700 dark:text-red-300 border border-red-200 dark:border-red-800">{n}</span>'
            for n in offline_names
        )
        offline_html = f'<div class="flex flex-wrap gap-1 mt-2">{badges}</div>'

    html = (
        f'<div class="text-3xl font-bold {color}">'
        f'{reachable}<span class="text-lg text-slate-400"> / {total}</span></div>'
        f'{offline_html}'
    )
    return HTMLResponse(html)


@router.get("/gpus/{gid}/metrics-partial", response_class=HTMLResponse)
def gpu_metrics_partial(request: Request, gid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    """HTML snippet with compact live metrics bar for admin GPU card."""
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    m = fetch_gpu_metrics(gpu.host, gpu.gpu_index or 0, ssh_user=gpu.ssh_user, ssh_port=gpu.ssh_port, ssh_password=gpu.ssh_password)
    return templates.TemplateResponse("partials/gpu_metrics_card.html", {"request": request, "m": m, "is_remote": gpu.is_remote})


@router.get("/gpus/{gid}/live-snapshot", response_class=HTMLResponse)
def gpu_live_snapshot(request: Request, gid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    """HTML snippet with full live snapshot for analytics page."""
    gpu = db.get(Gpu, gid)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    m, disk = fetch_live_stats(
        gpu.host, gpu.gpu_index or 0,
        ssh_user=gpu.ssh_user, ssh_port=gpu.ssh_port, ssh_password=gpu.ssh_password,
    )
    return templates.TemplateResponse("partials/gpu_live_snapshot.html", {"request": request, "m": m, "disk": disk, "is_remote": gpu.is_remote, "gpu_id": gid})


# ── Reservations (admin actions) ───────────────────────────────────────────

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
    log_action(db, user, "reservation.approve", target=f"reservation:{rid}")
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
    log_action(db, user, "reservation.reject", target=f"reservation:{rid}", detail=reason)
    await send_email(
        res.user.email,
        f"[GPU Manager] Request rejected for {res.gpu.name}",
        f"Hi {res.user.name},\n\n{msg}\n",
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/reservations/{rid}/force-release")
async def force_release(rid: int, db: Session = Depends(get_db), user=Depends(require_pi)):
    res = db.get(Reservation, rid)
    if res is None:
        raise HTTPException(404, "Reservation not found")
    if res.status not in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED):
        raise HTTPException(400, "Not an active or offered reservation")
    owner_name = res.user.name
    gpu_name = res.gpu.name
    add_notification(
        db, res.user_id,
        f"Your session on {gpu_name} was ended by the PI.",
        link="/dashboard",
    )
    next_res = release_reservation(db, res, by_admin=True)
    log_action(db, user, "reservation.force_release", target=f"reservation:{rid} ({owner_name} on {gpu_name})")
    if next_res:
        from ..routers.reservations import _notify_next_offered
        await _notify_next_offered(db, next_res)
    return RedirectResponse(url="/admin/activity", status_code=303)


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
    log_action(db, user, "queue.move", target=f"reservation:{rid}", detail=direction)
    return RedirectResponse(url="/admin/activity", status_code=303)


# ── Audit log ──────────────────────────────────────────────────────────────

@router.get("/audit", response_class=HTMLResponse)
def admin_audit(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_pi),
    actor: str = "",
    action: str = "",
    target: str = "",
):
    q = db.query(AuditLog)
    if actor:
        q = q.filter(AuditLog.actor_name.ilike(f"%{actor}%"))
    if action:
        q = q.filter(AuditLog.action.ilike(f"%{action}%"))
    if target:
        q = q.filter(AuditLog.target.ilike(f"%{target}%"))
    logs = q.order_by(AuditLog.created_at.desc()).limit(500).all()
    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read.is_(False))
        .count()
    )
    return templates.TemplateResponse("admin_audit.html", {
        "request": request,
        "user": user,
        "logs": logs,
        "settings": settings,
        "unread_count": unread_count,
    })


# ── Activity (full history) ────────────────────────────────────────────────

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

    unread_count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read.is_(False))
        .count()
    )

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
            "unread_count": unread_count,
        },
    )


def _reservation_csv(rows: list[Reservation]) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "id", "user", "user_email", "gpu", "status", "requested_hours",
        "priority", "created_at", "scheduled_start_at", "started_at",
        "ended_at", "expected_end_at", "note",
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
            r.scheduled_start_at.isoformat() if r.scheduled_start_at else "",
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


# ── GPU metric history (JSON for charts) ───────────────────────────────────

@router.get("/gpus/{gid}/metrics-history")
def gpu_metrics_history(
    gid: int,
    window: str = "6h",
    db: Session = Depends(get_db),
    user=Depends(require_pi),
):
    """Return time-series GPU metric samples as JSON for Chart.js.
    window: 1h, 6h, 24h, 7d
    """
    windows = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
    hours = windows.get(window, 6)
    cutoff = utcnow() - timedelta(hours=hours)

    samples = (
        db.query(GpuMetricSample)
        .filter(GpuMetricSample.gpu_id == gid, GpuMetricSample.sampled_at >= cutoff)
        .order_by(GpuMetricSample.sampled_at)
        .all()
    )

    # Down-sample to at most 300 points so charts stay fast
    if len(samples) > 300:
        step = len(samples) // 300
        samples = samples[::step]

    return {
        "labels": [s.sampled_at.strftime("%Y-%m-%dT%H:%M:%S") for s in samples],
        "util_gpu": [s.util_gpu for s in samples],
        "util_mem": [s.util_mem for s in samples],
        "mem_used": [s.mem_used for s in samples],
        "mem_total": samples[0].mem_total if samples else None,
        "temp_c": [s.temp_c for s in samples],
        "power_w": [s.power_w for s in samples],
    }


# ── GPU Analytics ──────────────────────────────────────────────────────────

@router.get("/analytics", response_class=HTMLResponse)
def admin_analytics(request: Request, db: Session = Depends(get_db), user=Depends(require_pi)):
    import json

    gpus = db.query(Gpu).order_by(Gpu.name).all()
    now = utcnow()
    thirty_days_ago = now - timedelta(days=30)
    fourteen_days_ago = now - timedelta(days=14)

    gpu_live: dict[int, dict] = {}

    # ── Per-GPU historical stats ─────────────────────────────────────────────
    gpu_stats: dict[int, dict] = {}
    for g in gpus:
        completed = (
            db.query(Reservation)
            .filter(Reservation.gpu_id == g.id, Reservation.status == ReservationStatus.COMPLETED)
            .all()
        )
        total_hours = sum(
            (r.ended_at - r.started_at).total_seconds() / 3600
            for r in completed if r.started_at and r.ended_at
        )
        unique_users = len({r.user_id for r in completed})
        avg_hours = total_hours / len(completed) if completed else 0
        last_used = max((r.ended_at for r in completed if r.ended_at), default=None)
        active_r = next((r for r in g.reservations if r.status == ReservationStatus.ACTIVE), None)
        queued_count = sum(1 for r in g.reservations if r.status == ReservationStatus.QUEUED)
        gpu_stats[g.id] = {
            "total_sessions": len(completed),
            "total_hours": round(total_hours, 1),
            "avg_hours": round(avg_hours, 1),
            "unique_users": unique_users,
            "last_used": last_used,
            "active_user": active_r.user.name if active_r else None,
            "queued_count": queued_count,
        }

    # ── Chart: GPU hours last 30 days ────────────────────────────────────────
    gpu_hours_30d: dict[str, float] = {}
    for g in gpus:
        rows = (
            db.query(Reservation)
            .filter(
                Reservation.gpu_id == g.id,
                Reservation.status == ReservationStatus.COMPLETED,
                Reservation.ended_at >= thirty_days_ago,
            )
            .all()
        )
        gpu_hours_30d[g.name] = round(
            sum((r.ended_at - r.started_at).total_seconds() / 3600 for r in rows if r.started_at and r.ended_at), 1
        )

    # ── Chart: top users last 30 days ────────────────────────────────────────
    user_hours: dict[str, float] = {}
    for r in (
        db.query(Reservation)
        .filter(Reservation.status == ReservationStatus.COMPLETED, Reservation.ended_at >= thirty_days_ago)
        .all()
    ):
        if r.started_at and r.ended_at and r.user:
            h = (r.ended_at - r.started_at).total_seconds() / 3600
            user_hours[r.user.name] = user_hours.get(r.user.name, 0) + h
    top_users = sorted(user_hours.items(), key=lambda x: x[1], reverse=True)[:10]

    # ── Chart: daily GPU hours last 14 days ──────────────────────────────────
    daily_labels = [(now - timedelta(days=13 - i)).strftime("%b %d") for i in range(14)]
    daily_totals = [0.0] * 14
    for r in (
        db.query(Reservation)
        .filter(Reservation.status == ReservationStatus.COMPLETED, Reservation.ended_at >= fourteen_days_ago)
        .all()
    ):
        if r.started_at and r.ended_at:
            day_idx = (r.ended_at.date() - fourteen_days_ago.date()).days
            if 0 <= day_idx < 14:
                daily_totals[day_idx] += (r.ended_at - r.started_at).total_seconds() / 3600
    daily_totals = [round(v, 1) for v in daily_totals]

    # ── Chart: per-GPU user breakdown (stacked) ──────────────────────────────
    # {gpu_name: {user_name: hours}}
    gpu_user_hours: dict[str, dict[str, float]] = {g.name: {} for g in gpus}
    for r in (
        db.query(Reservation)
        .filter(Reservation.status == ReservationStatus.COMPLETED, Reservation.ended_at >= thirty_days_ago)
        .all()
    ):
        if r.started_at and r.ended_at and r.gpu and r.user:
            h = (r.ended_at - r.started_at).total_seconds() / 3600
            gpu_user_hours[r.gpu.name][r.user.name] = gpu_user_hours[r.gpu.name].get(r.user.name, 0) + h

    # Summary counters
    online_count = sum(1 for g in gpus if g.host)
    total_active = sum(1 for g in gpus if g.status.value == "IN_USE")
    total_queued = sum(s["queued_count"] for s in gpu_stats.values())
    all_hours_30d = round(sum(gpu_hours_30d.values()), 1)

    unread_count = (
        db.query(Notification).filter(Notification.user_id == user.id, Notification.read.is_(False)).count()
    )

    return templates.TemplateResponse(
        "admin_analytics.html",
        {
            "request": request,
            "user": user,
            "gpus": gpus,
            "gpu_live": gpu_live,
            "gpu_stats": gpu_stats,
            "top_users": top_users,
            "gpu_hours_30d_json": json.dumps(gpu_hours_30d),
            "top_users_json": json.dumps(dict(top_users)),
            "daily_labels_json": json.dumps(daily_labels),
            "daily_totals_json": json.dumps(daily_totals),
            "gpu_user_hours_json": json.dumps({k: v for k, v in gpu_user_hours.items()}),
            "online_count": online_count,
            "total_active": total_active,
            "total_queued": total_queued,
            "all_hours_30d": all_hours_30d,
            "settings": settings,
            "unread_count": unread_count,
            "GpuStatus": GpuStatus,
        },
    )
