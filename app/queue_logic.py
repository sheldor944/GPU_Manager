"""Reservation + queue logic.

Booking model:
- A user requests a GPU for N hours.
- If the GPU is free, the reservation becomes ACTIVE immediately.
- Otherwise the reservation is QUEUED, with a priority captured at queue time:
    priority 0 = user is within quota at queue time
    priority 1 = user is over quota at queue time
  Within a GPU's queue: lower priority first, then FIFO by created_at.
- When the active reservation on a GPU ends, the next queued entry is OFFERED
  (the user has QUEUE_CONFIRM_MINUTES to accept). If they ignore it, it becomes
  SKIPPED and the next one is offered.
- Scheduled reservations: user picks a future start time. The scheduler activates
  them when the time arrives (or queues them if the GPU is occupied at that point).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .config import settings
from .models import (
    Gpu,
    GpuAccess,
    GpuStatus,
    GpuVisibility,
    Notification,
    Reservation,
    ReservationStatus,
    Role,
    User,
    Watch,
    utcnow,
)
from .settings_store import REQUIRE_REQUEST_APPROVAL, get_bool


def user_can_access_gpu(db: Session, user: User, gpu: Gpu) -> bool:
    """PI always has access. Regular users can access PUBLIC GPUs or RESTRICTED ones they've been granted."""
    if user.role == Role.PI:
        return True
    if gpu.visibility == GpuVisibility.PUBLIC:
        return True
    return (
        db.query(GpuAccess)
        .filter(GpuAccess.gpu_id == gpu.id, GpuAccess.user_id == user.id)
        .first() is not None
    )


def _active_on_gpu(db: Session, gpu_id: int) -> Optional[Reservation]:
    return (
        db.query(Reservation)
        .filter(Reservation.gpu_id == gpu_id, Reservation.status == ReservationStatus.ACTIVE)
        .first()
    )


def _offered_on_gpu(db: Session, gpu_id: int) -> Optional[Reservation]:
    return (
        db.query(Reservation)
        .filter(Reservation.gpu_id == gpu_id, Reservation.status == ReservationStatus.OFFERED)
        .first()
    )


def queue_for_gpu(db: Session, gpu_id: int) -> list[Reservation]:
    return (
        db.query(Reservation)
        .filter(Reservation.gpu_id == gpu_id, Reservation.status == ReservationStatus.QUEUED)
        .order_by(Reservation.priority.asc(), Reservation.queue_sort_key.asc())
        .all()
    )


def scheduled_for_gpu(db: Session, gpu_id: int) -> list[Reservation]:
    """Return SCHEDULED reservations for a GPU ordered by start time."""
    return (
        db.query(Reservation)
        .filter(Reservation.gpu_id == gpu_id, Reservation.status == ReservationStatus.SCHEDULED)
        .order_by(Reservation.scheduled_start_at.asc())
        .all()
    )


def check_schedule_conflict(
    db: Session, gpu_id: int, start: datetime, end: datetime, exclude_id: Optional[int] = None
) -> Optional[Reservation]:
    """Return a conflicting SCHEDULED or ACTIVE reservation if one exists, else None."""
    # Check against other SCHEDULED reservations
    candidates = (
        db.query(Reservation)
        .filter(
            Reservation.gpu_id == gpu_id,
            Reservation.status == ReservationStatus.SCHEDULED,
            Reservation.scheduled_start_at.is_not(None),
            Reservation.scheduled_start_at < end,
        )
        .all()
    )
    for r in candidates:
        if exclude_id and r.id == exclude_id:
            continue
        r_start = r.scheduled_start_at
        if r_start.tzinfo is None:
            r_start = r_start.replace(tzinfo=timezone.utc)
        r_end = r_start + timedelta(hours=r.requested_hours)
        if r_start < end and r_end > start:
            return r

    # Also check the currently ACTIVE reservation — warn if it's expected to overlap
    active = _active_on_gpu(db, gpu_id)
    if active and active.expected_end_at:
        active_end = active.expected_end_at
        if active_end.tzinfo is None:
            active_end = active_end.replace(tzinfo=timezone.utc)
        if active_end > start:
            return active

    return None


def move_in_queue(db: Session, res: Reservation, direction: int) -> None:
    """Move a queued reservation up (-1) or down (+1) by one position."""
    if res.status != ReservationStatus.QUEUED:
        raise ValueError("Only queued reservations can be reordered")
    if direction not in (-1, 1):
        raise ValueError("direction must be -1 or +1")
    q = queue_for_gpu(db, res.gpu_id)
    idx = next((i for i, r in enumerate(q) if r.id == res.id), None)
    if idx is None:
        return
    target_idx = idx + direction
    if target_idx < 0 or target_idx >= len(q):
        return
    other = q[target_idx]
    if other.priority != res.priority:
        return
    res.queue_sort_key, other.queue_sort_key = other.queue_sort_key, res.queue_sort_key
    db.commit()


def queue_position(db: Session, reservation: Reservation) -> int:
    """1-based position in the queue for this GPU; 0 if not queued."""
    if reservation.status != ReservationStatus.QUEUED:
        return 0
    q = queue_for_gpu(db, reservation.gpu_id)
    for i, r in enumerate(q, start=1):
        if r.id == reservation.id:
            return i
    return 0


def estimated_wait_seconds(db: Session, reservation: Reservation) -> Optional[int]:
    """Rough estimate of seconds until a QUEUED reservation becomes active."""
    if reservation.status != ReservationStatus.QUEUED:
        return None
    now = utcnow()
    active = _active_on_gpu(db, reservation.gpu_id)
    offered = _offered_on_gpu(db, reservation.gpu_id)
    current = active or offered

    if current is None:
        return 0

    # Start from expected end of current session
    if current.expected_end_at:
        end_dt = current.expected_end_at
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        base_secs = max(0.0, (end_dt - now).total_seconds())
    else:
        base_secs = 0.0

    # Add time for each queue member ahead of this one
    q = queue_for_gpu(db, reservation.gpu_id)
    for r in q:
        if r.id == reservation.id:
            break
        base_secs += r.requested_hours * 3600

    return int(base_secs)


def user_has_active_or_offered(db: Session, user_id: int) -> bool:
    return (
        db.query(Reservation)
        .filter(
            Reservation.user_id == user_id,
            Reservation.status.in_([ReservationStatus.ACTIVE, ReservationStatus.OFFERED]),
        )
        .first()
        is not None
    )


def user_has_pending(db: Session, user_id: int) -> bool:
    return (
        db.query(Reservation)
        .filter(
            Reservation.user_id == user_id,
            Reservation.status == ReservationStatus.PENDING_APPROVAL,
        )
        .first()
        is not None
    )


def user_queued_on_gpu(db: Session, user_id: int, gpu_id: int) -> Optional[Reservation]:
    return (
        db.query(Reservation)
        .filter(
            Reservation.user_id == user_id,
            Reservation.gpu_id == gpu_id,
            Reservation.status == ReservationStatus.QUEUED,
        )
        .first()
    )


def user_scheduled_on_gpu(db: Session, user_id: int, gpu_id: int) -> Optional[Reservation]:
    return (
        db.query(Reservation)
        .filter(
            Reservation.user_id == user_id,
            Reservation.gpu_id == gpu_id,
            Reservation.status == ReservationStatus.SCHEDULED,
        )
        .first()
    )


def gpu_effective_max_hours(gpu: Gpu) -> float:
    """Per-GPU override or global config limit."""
    return gpu.max_hours if gpu.max_hours is not None else settings.MAX_RESERVATION_HOURS


def request_gpu(
    db: Session,
    user: User,
    gpu: Gpu,
    hours: float,
    note: str = "",
    scheduled_start_at: Optional[datetime] = None,
) -> tuple[Reservation, str]:
    """Create a reservation. Returns (reservation, human_message).

    If scheduled_start_at is provided (and in the future), the reservation is
    created as SCHEDULED. Otherwise it follows the normal QUEUED/ACTIVE flow.

    Raises ValueError for client-side problems (caller maps to HTTP 400).
    """
    if hours <= 0:
        raise ValueError("Hours must be positive")
    effective_max = gpu_effective_max_hours(gpu)
    if hours > effective_max:
        raise ValueError(f"Reservation exceeds max of {effective_max:g}h for this GPU")
    if not user_can_access_gpu(db, user, gpu):
        raise ValueError("You do not have access to this GPU.")
    if gpu.status == GpuStatus.MAINTENANCE:
        raise ValueError("GPU is in maintenance and cannot be booked")

    now = utcnow()

    # --- Scheduled path ---
    if scheduled_start_at is not None:
        if scheduled_start_at.tzinfo is None:
            scheduled_start_at = scheduled_start_at.replace(tzinfo=timezone.utc)
        if scheduled_start_at <= now + timedelta(minutes=5):
            raise ValueError("Scheduled start time must be at least 5 minutes in the future. For immediate booking, leave start time blank.")
        scheduled_end = scheduled_start_at + timedelta(hours=hours)
        conflict = check_schedule_conflict(db, gpu.id, scheduled_start_at, scheduled_end)
        if conflict:
            cs = conflict.scheduled_start_at
            if cs.tzinfo is None:
                cs = cs.replace(tzinfo=timezone.utc)
            raise ValueError(
                f"Time conflict: {conflict.user.name} has already booked this GPU "
                f"from {cs:%b %d %H:%M} for {conflict.requested_hours:g}h."
            )
        if user_scheduled_on_gpu(db, user.id, gpu.id):
            raise ValueError("You already have a scheduled booking for this GPU.")
        if user_queued_on_gpu(db, user.id, gpu.id):
            raise ValueError("You are already queued for this GPU. Cancel that first before scheduling.")

        res = Reservation(
            user_id=user.id,
            gpu_id=gpu.id,
            requested_hours=hours,
            priority=1 if user.over_quota else 0,
            note=note or None,
            status=ReservationStatus.SCHEDULED,
            scheduled_start_at=scheduled_start_at,
        )
        db.add(res)
        db.commit()
        return res, f"Scheduled on {gpu.name} for {scheduled_start_at:%b %d %H:%M} ({hours:g}h)."

    # --- Immediate path ---
    if user_has_active_or_offered(db, user.id):
        raise ValueError("You already have an active reservation or pending offer. Release it first.")
    if user_has_pending(db, user.id):
        raise ValueError("You already have a request waiting for admin approval.")
    if user_queued_on_gpu(db, user.id, gpu.id):
        raise ValueError("You are already queued for this GPU")

    priority = 1 if user.over_quota else 0
    res = Reservation(
        user_id=user.id,
        gpu_id=gpu.id,
        requested_hours=hours,
        priority=priority,
        note=note or None,
        status=ReservationStatus.PENDING_APPROVAL,
    )
    db.add(res)
    db.flush()

    skip_approval = user.role == Role.PI or not get_bool(db, REQUIRE_REQUEST_APPROVAL, default=False)
    if skip_approval:
        db.commit()
        _activate_or_queue(db, res)
        return res, _request_outcome_message(db, res)

    db.commit()
    return res, "Request submitted — waiting for admin approval."


def activate_scheduled_reservations(db: Session) -> list[Reservation]:
    """Activate SCHEDULED reservations whose start time has arrived.

    Returns the list of reservations just activated/offered (so the caller can email them).
    """
    now = utcnow()
    due: list[Reservation] = (
        db.query(Reservation)
        .filter(
            Reservation.status == ReservationStatus.SCHEDULED,
            Reservation.scheduled_start_at.is_not(None),
            Reservation.scheduled_start_at <= now,
        )
        .all()
    )
    activated: list[Reservation] = []
    for res in due:
        gpu = res.gpu
        if gpu.status == GpuStatus.MAINTENANCE:
            res.status = ReservationStatus.CANCELLED
            res.ended_at = now
            add_notification(
                db,
                res.user_id,
                f"Your scheduled session on {gpu.name} was cancelled — GPU entered maintenance.",
                link="/dashboard",
            )
            db.flush()
            continue

        # Flush before querying so prior iterations' changes are visible
        db.flush()
        active = _active_on_gpu(db, gpu.id)
        offered_res = _offered_on_gpu(db, gpu.id)
        if active is None and offered_res is None and gpu.status == GpuStatus.AVAILABLE:
            _start_reservation(db, res)
            gpu.status = GpuStatus.IN_USE
            db.flush()
            add_notification(
                db, res.user_id,
                f"Your scheduled session on {gpu.name} is now ACTIVE.",
                link="/dashboard",
            )
            activated.append(res)
        else:
            # GPU occupied — move to front of queue (priority 0, very early sort key)
            # Only queue if not already queued for this GPU (avoid duplicates)
            existing_queued = user_queued_on_gpu(db, res.user_id, gpu.id)
            if existing_queued:
                res.status = ReservationStatus.CANCELLED
                res.ended_at = now
                add_notification(
                    db, res.user_id,
                    f"Your scheduled time on {gpu.name} arrived but you're already in the queue.",
                    link="/dashboard",
                )
            else:
                res.status = ReservationStatus.QUEUED
                res.queue_sort_key = 0.0  # front of the line
                add_notification(
                    db, res.user_id,
                    f"Your scheduled time on {gpu.name} arrived but the GPU is still in use. "
                    f"You've been moved to the front of the queue.",
                    link="/dashboard",
                )
    if due:
        db.commit()
    return activated


def _request_outcome_message(db: Session, res: Reservation) -> str:
    if res.status == ReservationStatus.ACTIVE:
        return f"Reservation active on {res.gpu.name} for {res.requested_hours:g}h."
    if res.status == ReservationStatus.QUEUED:
        pos = queue_position(db, res)
        return f"Queued for {res.gpu.name} (position {pos})."
    return f"Reservation is {res.status.value}."


def _activate_or_queue(db: Session, res: Reservation) -> None:
    """Move a freshly-approved reservation into ACTIVE or QUEUED."""
    gpu = res.gpu
    # Re-check at activation time: don't give a user two simultaneous sessions
    if user_has_active_or_offered(db, res.user_id):
        res.status = ReservationStatus.QUEUED
        res.queue_sort_key = utcnow().timestamp()
        db.commit()
        return
    active = _active_on_gpu(db, gpu.id)
    offered = _offered_on_gpu(db, gpu.id)
    if active is None and offered is None and gpu.status == GpuStatus.AVAILABLE:
        _start_reservation(db, res)
        gpu.status = GpuStatus.IN_USE
    else:
        res.status = ReservationStatus.QUEUED
        res.queue_sort_key = utcnow().timestamp()
    db.commit()


def approve_request(db: Session, res: Reservation) -> Reservation:
    if res.status != ReservationStatus.PENDING_APPROVAL:
        raise ValueError("Only pending requests can be approved")
    if res.gpu.status == GpuStatus.MAINTENANCE:
        raise ValueError("GPU is in maintenance; cancel the request instead")
    _activate_or_queue(db, res)
    return res


def reject_request(db: Session, res: Reservation, reason: str = "") -> None:
    if res.status != ReservationStatus.PENDING_APPROVAL:
        raise ValueError("Only pending requests can be rejected")
    res.status = ReservationStatus.REJECTED
    res.ended_at = utcnow()
    if reason:
        res.note = (res.note + " | " if res.note else "") + f"rejected: {reason}"
    db.commit()


def _start_reservation(db: Session, res: Reservation) -> None:
    now = utcnow()
    res.status = ReservationStatus.ACTIVE
    res.started_at = now
    res.expected_end_at = now + timedelta(hours=res.requested_hours)


def release_reservation(db: Session, res: Reservation, by_admin: bool = False) -> Optional[Reservation]:
    """End an ACTIVE / OFFERED reservation and promote the next in queue."""
    if res.status not in (ReservationStatus.ACTIVE, ReservationStatus.OFFERED):
        raise ValueError("Reservation is not active")

    now = utcnow()
    if res.status == ReservationStatus.ACTIVE:
        elapsed = 0.0
        if res.started_at:
            started = res.started_at if res.started_at.tzinfo else res.started_at.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (now - started).total_seconds() / 3600.0)
        charged = min(elapsed, res.requested_hours)
        res.user.used_hours = round(res.user.used_hours + charged, 4)
        res.status = ReservationStatus.COMPLETED
    else:
        res.status = ReservationStatus.SKIPPED
    res.ended_at = now

    next_res = _promote_next(db, res.gpu_id)
    db.commit()
    return next_res


def cancel_queued(db: Session, res: Reservation) -> None:
    if res.status not in (ReservationStatus.QUEUED, ReservationStatus.PENDING_APPROVAL, ReservationStatus.SCHEDULED):
        raise ValueError("Only queued, scheduled, or pending reservations can be cancelled")
    res.status = ReservationStatus.CANCELLED
    res.ended_at = utcnow()
    db.commit()


def confirm_offer(db: Session, res: Reservation) -> None:
    if res.status != ReservationStatus.OFFERED:
        raise ValueError("This reservation is not currently offered")
    # Defensive check: ensure user doesn't already have an active session
    existing = (
        db.query(Reservation)
        .filter(
            Reservation.user_id == res.user_id,
            Reservation.status == ReservationStatus.ACTIVE,
        )
        .first()
    )
    if existing:
        raise ValueError(f"You already have an active session on {existing.gpu.name}. Release it first.")
    _start_reservation(db, res)
    res.gpu.status = GpuStatus.IN_USE
    db.commit()


def _promote_next(db: Session, gpu_id: int) -> Optional[Reservation]:
    """Offer the next queued reservation on this GPU. Returns it, or None.

    Skips any user who already has an ACTIVE or OFFERED session on another GPU
    to prevent a user from holding two GPUs simultaneously.
    """
    gpu = db.get(Gpu, gpu_id)
    if gpu is None:
        return None
    if gpu.status == GpuStatus.MAINTENANCE:
        return None

    # Guard against double-offer from concurrent calls (e.g. two expiries same tick)
    if _offered_on_gpu(db, gpu_id) is not None:
        return None

    q = queue_for_gpu(db, gpu_id)
    if not q:
        gpu.status = GpuStatus.AVAILABLE
        return None

    for nxt in q:
        if user_has_active_or_offered(db, nxt.user_id):
            # User is already holding or waiting on another GPU; skip them for now.
            continue
        nxt.status = ReservationStatus.OFFERED
        nxt.offered_at = utcnow()
        gpu.status = GpuStatus.IN_USE
        return nxt

    # Everyone in queue is already active elsewhere
    gpu.status = GpuStatus.AVAILABLE
    return None


def expire_overdue(db: Session) -> list[Reservation]:
    """Auto-release reservations past their expected_end_at."""
    now = utcnow()
    overdue: list[Reservation] = (
        db.query(Reservation)
        .filter(
            Reservation.status == ReservationStatus.ACTIVE,
            Reservation.expected_end_at.is_not(None),
            Reservation.expected_end_at <= now,
        )
        .all()
    )
    offered: list[Reservation] = []
    for res in overdue:
        # Charge actual elapsed time (not requested_hours — session may have been shortened)
        if res.started_at:
            started = res.started_at if res.started_at.tzinfo else res.started_at.replace(tzinfo=timezone.utc)
            elapsed_hours = min(res.requested_hours, max(0.0, (now - started).total_seconds() / 3600.0))
        else:
            elapsed_hours = res.requested_hours
        res.user.used_hours = round(res.user.used_hours + elapsed_hours, 4)
        res.status = ReservationStatus.EXPIRED
        res.ended_at = now
        db.flush()  # commit status change before promoting so _active_on_gpu sees correct state
        nxt = _promote_next(db, res.gpu_id)
        if nxt is not None:
            offered.append(nxt)
    if overdue:
        db.commit()
    return offered


def skip_stale_offers(db: Session) -> list[Reservation]:
    """If an OFFERED reservation has sat unanswered for QUEUE_CONFIRM_MINUTES, skip it."""
    cutoff = utcnow() - timedelta(minutes=settings.QUEUE_CONFIRM_MINUTES)
    stale: list[Reservation] = (
        db.query(Reservation)
        .filter(
            Reservation.status == ReservationStatus.OFFERED,
            Reservation.offered_at.is_not(None),
            Reservation.offered_at <= cutoff,
        )
        .all()
    )
    promoted: list[Reservation] = []
    for res in stale:
        res.status = ReservationStatus.SKIPPED
        res.ended_at = utcnow()
        nxt = _promote_next(db, res.gpu_id)
        if nxt is not None:
            promoted.append(nxt)
    if stale:
        db.commit()
    return promoted


def add_notification(db: Session, user_id: int, message: str, link: str | None = None) -> None:
    db.add(Notification(user_id=user_id, message=message, link=link))
    db.commit()


def adjust_reservation_hours(db: Session, res: Reservation, new_hours: float) -> tuple[str, Optional[Reservation]]:
    """Extend or shorten an ACTIVE reservation."""
    if res.status != ReservationStatus.ACTIVE:
        raise ValueError("Only active reservations can be adjusted")
    if new_hours <= 0:
        raise ValueError("Hours must be positive")
    effective_max = gpu_effective_max_hours(res.gpu)
    if new_hours > effective_max:
        raise ValueError(f"Total reservation cannot exceed {effective_max:g}h for this GPU")
    if res.started_at is None:
        raise ValueError("Reservation missing start time")

    started = res.started_at if res.started_at.tzinfo else res.started_at.replace(tzinfo=timezone.utc)
    now = utcnow()
    elapsed_h = max(0.0, (now - started).total_seconds() / 3600.0)
    if new_hours <= elapsed_h:
        next_res = release_reservation(db, res)
        return f"Reservation ended now (used {elapsed_h:.2f}h).", next_res
    res.requested_hours = round(new_hours, 4)
    res.expected_end_at = started + timedelta(hours=new_hours)
    res.warning_sent_at = None
    db.commit()
    return f"Reservation set to {new_hours:g}h total (ends {res.expected_end_at:%b %d %H:%M}).", None


def add_watch(db: Session, user_id: int, gpu_id: int) -> bool:
    existing = (
        db.query(Watch)
        .filter(Watch.user_id == user_id, Watch.gpu_id == gpu_id)
        .first()
    )
    if existing:
        return False
    db.add(Watch(user_id=user_id, gpu_id=gpu_id))
    db.commit()
    return True


def remove_watch(db: Session, user_id: int, gpu_id: int) -> bool:
    existing = (
        db.query(Watch)
        .filter(Watch.user_id == user_id, Watch.gpu_id == gpu_id)
        .first()
    )
    if not existing:
        return False
    db.delete(existing)
    db.commit()
    return True


def pop_watchers_for_gpu(db: Session, gpu_id: int) -> list[Watch]:
    watches = db.query(Watch).filter(Watch.gpu_id == gpu_id).all()
    for w in watches:
        db.delete(w)
    if watches:
        db.commit()
    return watches
