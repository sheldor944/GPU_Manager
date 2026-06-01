"""Background scheduler: periodically expire overdue reservations, skip stale
offers, send "session ending soon" warnings, and ping users watching a freed GPU.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import settings
from .database import session_scope
from .email_utils import send_email
from .models import Gpu, GpuStatus, Reservation, ReservationStatus, User, Watch, utcnow
from .queue_logic import add_notification, expire_overdue, skip_stale_offers
from .quota_reset import maybe_run as maybe_quota_reset

logger = logging.getLogger(__name__)


async def _notify_offered(reservation_ids: list[int]):
    for rid in reservation_ids:
        try:
            with session_scope() as db:
                fresh = db.get(Reservation, rid)
                if fresh is None:
                    continue
                user = fresh.user
                gpu = fresh.gpu
                link = f"{settings.BASE_URL}/dashboard"
                msg = (
                    f"{gpu.name} is now available for you. "
                    f"Confirm within {settings.QUEUE_CONFIRM_MINUTES} minutes or you'll be skipped."
                )
                add_notification(db, user.id, msg, link=link)
                email = user.email
                name = user.name
                gpu_name = gpu.name
            body = (
                f"Hi {name},\n\n"
                f"{gpu_name} is now available for you in the lab GPU manager.\n"
                f"You have {settings.QUEUE_CONFIRM_MINUTES} minutes to confirm,"
                f" otherwise the next person in line will be offered the slot.\n\n"
                f"Confirm here: {settings.BASE_URL}/dashboard\n"
            )
            await send_email(email, f"[GPU Manager] {gpu_name} is ready for you", body)
        except Exception:
            logger.exception("notify_offered failed for reservation %s", rid)


async def _send_warning_emails() -> None:
    """Email users whose active reservation ends within WARN_BEFORE_END_MINUTES."""
    targets: list[tuple[str, str, str, str]] = []
    with session_scope() as db:
        now = utcnow()
        cutoff = now + timedelta(minutes=settings.WARN_BEFORE_END_MINUTES)
        rows = (
            db.query(Reservation)
            .filter(
                Reservation.status == ReservationStatus.ACTIVE,
                Reservation.expected_end_at.is_not(None),
                Reservation.expected_end_at <= cutoff,
                Reservation.expected_end_at > now,
                Reservation.warning_sent_at.is_(None),
            )
            .all()
        )
        for r in rows:
            r.warning_sent_at = now
            targets.append((
                r.user.email,
                r.user.name,
                r.gpu.name,
                r.expected_end_at.strftime("%b %d %H:%M"),
            ))
            add_notification(
                db,
                r.user_id,
                f"Your session on {r.gpu.name} ends at {r.expected_end_at:%H:%M}. "
                f"Extend it from the dashboard if you need more time.",
                link="/dashboard",
            )
        if rows:
            db.commit()
    for email, name, gpu_name, end_str in targets:
        try:
            await send_email(
                email,
                f"[GPU Manager] {gpu_name} session ending at {end_str}",
                f"Hi {name},\n\nYour reservation on {gpu_name} ends at {end_str}.\n"
                f"If you need more time, extend it from the dashboard: "
                f"{settings.BASE_URL}/dashboard\n",
            )
        except Exception:
            logger.exception("warning email failed for %s", email)


async def _notify_watchers_of_free_gpus() -> None:
    """For every AVAILABLE GPU with watchers, ping the watchers and clear watches."""
    plan: list[tuple[str, str, str]] = []  # (email, name, gpu_name)
    with session_scope() as db:
        free_gpus = db.query(Gpu).filter(Gpu.status == GpuStatus.AVAILABLE).all()
        for gpu in free_gpus:
            watches = db.query(Watch).filter(Watch.gpu_id == gpu.id).all()
            for w in watches:
                u = db.get(User, w.user_id)
                if u and u.is_active:
                    add_notification(
                        db,
                        u.id,
                        f"{gpu.name} is now free. Request it before someone else does.",
                        link="/dashboard",
                    )
                    plan.append((u.email, u.name, gpu.name))
                db.delete(w)
        if any(True for _ in free_gpus):
            db.commit()
    for email, name, gpu_name in plan:
        try:
            await send_email(
                email,
                f"[GPU Manager] {gpu_name} is free",
                f"Hi {name},\n\n{gpu_name} just became available. "
                f"Head to the dashboard to grab it: {settings.BASE_URL}/dashboard\n",
            )
        except Exception:
            logger.exception("watcher email failed for %s", email)


async def _tick():
    try:
        with session_scope() as db:
            offered_expired = expire_overdue(db)
            offered_skipped = skip_stale_offers(db)
            ids = [r.id for r in (offered_expired + offered_skipped)]
        if ids:
            await _notify_offered(ids)
        await _send_warning_emails()
        await _notify_watchers_of_free_gpus()
    except Exception:
        logger.exception("scheduler tick failed")


async def _quota_reset_tick():
    try:
        with session_scope() as db:
            maybe_quota_reset(db)
    except Exception:
        logger.exception("quota reset tick failed")


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(_tick, "interval", seconds=30, id="gpu-tick", max_instances=1, coalesce=True)
    scheduler.add_job(_quota_reset_tick, "interval", hours=1, id="quota-reset-tick", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
