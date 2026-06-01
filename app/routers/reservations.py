from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_user
from ..config import settings
from ..database import get_db
from ..email_utils import send_email
from ..models import Gpu, Reservation, ReservationStatus, Role, User
from ..queue_logic import (
    add_notification,
    add_watch,
    adjust_reservation_hours,
    cancel_queued,
    confirm_offer,
    release_reservation,
    remove_watch,
    request_gpu,
)


def _redirect_back(msg: str = "", kind: str = "success") -> RedirectResponse:
    from urllib.parse import urlencode
    qs = urlencode({"msg": msg, "kind": kind}) if msg else ""
    return RedirectResponse(url=f"/dashboard{'?' + qs if qs else ''}", status_code=303)


async def _notify_next_offered(db: Session, next_res: Reservation) -> None:
    add_notification(
        db,
        next_res.user_id,
        f"{next_res.gpu.name} is now available for you (confirm within {settings.QUEUE_CONFIRM_MINUTES}m).",
        link="/dashboard",
    )
    await send_email(
        next_res.user.email,
        f"[GPU Manager] {next_res.gpu.name} is ready for you",
        f"Hi {next_res.user.name},\n\n{next_res.gpu.name} just became available for you.\n"
        f"Confirm within {settings.QUEUE_CONFIRM_MINUTES} minutes or you'll be skipped.\n\n"
        f"{settings.BASE_URL}/dashboard\n",
    )

router = APIRouter(prefix="/reservations", tags=["reservations"])


async def _notify_admins_of_pending(db: Session, res: Reservation) -> None:
    admins = db.query(User).filter(User.role == Role.PI, User.is_active.is_(True)).all()
    msg = f"New request: {res.user.name} → {res.gpu.name} for {res.requested_hours:g}h"
    for a in admins:
        add_notification(db, a.id, msg, link="/admin")
        await send_email(
            a.email,
            "[GPU Manager] New GPU request pending approval",
            f"{msg}\n\nReview at {settings.BASE_URL}/admin\n",
        )


@router.post("/request")
async def request_reservation(
    gpu_id: int = Form(...),
    hours: float = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    gpu = db.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    try:
        res, msg = request_gpu(db, user, gpu, hours, note=note)
    except ValueError as e:
        add_notification(db, user.id, f"Could not book {gpu.name}: {e}")
        return _redirect_back(str(e), kind="error")
    add_notification(db, user.id, msg, link="/dashboard")
    if res.status == ReservationStatus.PENDING_APPROVAL:
        await _notify_admins_of_pending(db, res)
    return _redirect_back(msg)


@router.post("/{rid}/release")
async def release(rid: int, db: Session = Depends(get_db), user=Depends(require_user)):
    res = db.get(Reservation, rid)
    if res is None or res.user_id != user.id:
        raise HTTPException(404, "Not found")
    try:
        next_res = release_reservation(db, res)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if next_res is not None:
        await _notify_next_offered(db, next_res)
    return _redirect_back("Released.")


@router.post("/{rid}/extend")
async def extend(
    rid: int,
    hours: float = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    """`hours` is the new TOTAL duration (start_at → expected_end_at)."""
    res = db.get(Reservation, rid)
    if res is None or res.user_id != user.id:
        raise HTTPException(404, "Not found")
    try:
        msg, next_res = adjust_reservation_hours(db, res, hours)
    except ValueError as e:
        return _redirect_back(str(e), kind="error")
    if next_res is not None:
        await _notify_next_offered(db, next_res)
    return _redirect_back(msg)


@router.post("/{rid}/cancel")
def cancel(rid: int, db: Session = Depends(get_db), user=Depends(require_user)):
    res = db.get(Reservation, rid)
    if res is None or res.user_id != user.id:
        raise HTTPException(404, "Not found")
    try:
        cancel_queued(db, res)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _redirect_back("Cancelled.")


@router.post("/{rid}/confirm")
def confirm(rid: int, db: Session = Depends(get_db), user=Depends(require_user)):
    res = db.get(Reservation, rid)
    if res is None or res.user_id != user.id:
        raise HTTPException(404, "Not found")
    try:
        confirm_offer(db, res)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _redirect_back("Confirmed — GPU is yours.")


@router.post("/{rid}/note")
def edit_note(
    rid: int,
    note: str = Form(""),
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    res = db.get(Reservation, rid)
    if res is None or res.user_id != user.id:
        raise HTTPException(404, "Not found")
    if res.status not in (
        ReservationStatus.PENDING_APPROVAL,
        ReservationStatus.QUEUED,
        ReservationStatus.OFFERED,
        ReservationStatus.ACTIVE,
    ):
        raise HTTPException(400, "Reservation is already finished")
    cleaned = note.strip()
    res.note = cleaned or None
    db.commit()
    return _redirect_back("Note updated.")


@router.post("/watches/{gpu_id}")
def watch_gpu(gpu_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    gpu = db.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    added = add_watch(db, user.id, gpu_id)
    return _redirect_back(
        f"Watching {gpu.name} — we'll email you when it's free." if added else f"Already watching {gpu.name}."
    )


@router.post("/watches/{gpu_id}/remove")
def unwatch_gpu(gpu_id: int, db: Session = Depends(get_db), user=Depends(require_user)):
    gpu = db.get(Gpu, gpu_id)
    if gpu is None:
        raise HTTPException(404, "GPU not found")
    remove_watch(db, user.id, gpu_id)
    return _redirect_back(f"Removed watch on {gpu.name}.")
