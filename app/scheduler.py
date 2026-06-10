"""Background scheduler: periodically expire overdue reservations, skip stale
offers, send "session ending soon" warnings, activate scheduled reservations,
detect idle sessions, and ping users watching a freed GPU.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import partial

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import settings
from .database import session_scope
from .email_utils import send_email
from .gpu_metrics import fetch_gpu_metrics, is_gpu_idle
from .models import Gpu, GpuStatus, GpuMetricSample, Reservation, ReservationStatus, User, Watch, utcnow
from .queue_logic import (
    activate_scheduled_reservations,
    add_notification,
    expire_overdue,
    skip_stale_offers,
)
from .quota_reset import maybe_run as maybe_quota_reset
from .settings_store import get_setting
from .webhooks import send_webhook, WEBHOOK_URL_KEY

logger = logging.getLogger(__name__)

# After this many minutes of <5% GPU utilization during an active session,
# notify the user and the PI.
IDLE_WARN_MINUTES = 30


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
                # Respect user email preference
                if user.email_on_offer:
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
                await send_webhook(
                    f"🟢 {gpu.name} offered to {user.name} — confirm within {settings.QUEUE_CONFIRM_MINUTES}m",
                    db=db,
                )
        except Exception:
            logger.exception("notify_offered failed for reservation %s", rid)


async def _send_warning_emails() -> None:
    """Email users whose active reservation ends within WARN_BEFORE_END_MINUTES."""
    targets: list[tuple[str, str, str, str, bool]] = []
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
            add_notification(
                db,
                r.user_id,
                f"Your session on {r.gpu.name} ends at {r.expected_end_at:%H:%M}. "
                f"Extend it from the dashboard if you need more time.",
                link="/dashboard",
            )
            targets.append((
                r.user.email,
                r.user.name,
                r.gpu.name,
                r.expected_end_at.strftime("%b %d %H:%M"),
                r.user.email_on_warning,
            ))
        if rows:
            db.commit()
    for email, name, gpu_name, end_str, pref in targets:
        if not pref:
            continue
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
    plan: list[tuple[str, str, str, bool]] = []
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
                    plan.append((u.email, u.name, gpu.name, u.email_on_watch))
                db.delete(w)
        if any(True for _ in free_gpus):
            db.commit()
    for email, name, gpu_name, pref in plan:
        if not pref:
            continue
        try:
            await send_email(
                email,
                f"[GPU Manager] {gpu_name} is free",
                f"Hi {name},\n\n{gpu_name} just became available. "
                f"Head to the dashboard to grab it: {settings.BASE_URL}/dashboard\n",
            )
        except Exception:
            logger.exception("watcher email failed for %s", email)


async def _check_idle_sessions() -> None:
    """Detect ACTIVE sessions where GPU utilization has been near-zero for a while.

    We use `warning_sent_at` field as a proxy: if it's set to a very old timestamp
    (we repurpose a separate field) we'd need a new column. Instead we use an
    in-memory cache approach: track idle-warning-sent per reservation id.
    To keep it simple without extra DB columns, we send an idle warning once per
    reservation (stored in a set in this coroutine's closure — resets on restart).
    """
    pass  # Idle detection requires per-reservation state; see _idle_warned set below.


_idle_warned: set[int] = set()


async def _check_idle_sessions_impl() -> None:
    """Check ACTIVE sessions for GPU idleness and notify if warranted."""
    from datetime import timezone as _tz

    # Collect sessions to check without blocking
    to_check = []
    with session_scope() as db:
        active_res = (
            db.query(Reservation)
            .filter(Reservation.status == ReservationStatus.ACTIVE)
            .all()
        )
        for res in active_res:
            if res.id in _idle_warned:
                continue
            gpu = res.gpu
            if not gpu or gpu.status != GpuStatus.IN_USE or res.started_at is None:
                continue
            started = res.started_at if res.started_at.tzinfo else res.started_at.replace(tzinfo=_tz.utc)
            elapsed_min = (utcnow() - started).total_seconds() / 60
            if elapsed_min < IDLE_WARN_MINUTES:
                continue
            to_check.append((
                res.id, res.user_id, res.user.name, res.user.email,
                gpu.name, gpu.host, gpu.ssh_user, gpu.ssh_port, gpu.ssh_password,
                int(elapsed_min), gpu.gpu_index or 0,
            ))

    if not to_check:
        return

    loop = asyncio.get_running_loop()
    for (rid, uid, uname, uemail, gname, ghost, gssh_user, gssh_port, gssh_pw, elapsed_min, ggpu_idx) in to_check:
        # Run SSH in thread pool so the event loop stays responsive
        metrics = await loop.run_in_executor(
            None,
            partial(fetch_gpu_metrics, ghost, ggpu_idx, gssh_user, gssh_port, gssh_pw),
        )
        if not metrics or not is_gpu_idle(metrics):
            continue
        _idle_warned.add(rid)
        msg = (
            f"Your GPU session on {gname} appears idle "
            f"({metrics['util_gpu']}% utilization). "
            f"Please release it if you're not using it."
        )
        with session_scope() as db:
            from .models import Role
            add_notification(db, uid, msg, link="/dashboard")
            pis = db.query(User).filter(User.role == Role.PI, User.is_active.is_(True)).all()
            for pi in pis:
                add_notification(
                    db, pi.id,
                    f"Idle alert: {uname}'s session on {gname} shows {metrics['util_gpu']}% utilization "
                    f"after {elapsed_min}m.",
                    link="/admin/activity",
                )
        await send_email(
            uemail,
            f"[GPU Manager] Idle GPU detected — {gname}",
            f"Hi {uname},\n\nYour session on {gname} appears to be idle "
            f"({metrics['util_gpu']}% GPU utilization after {elapsed_min} minutes).\n\n"
            f"Please release the GPU from the dashboard if you're not actively using it:\n"
            f"{settings.BASE_URL}/dashboard\n",
        )
        with session_scope() as db:
            await send_webhook(
                f"⚠️ Idle GPU: {uname}'s session on {gname} — "
                f"{metrics['util_gpu']}% util after {elapsed_min}m",
                db=db,
            )


async def _collect_gpu_metrics() -> None:
    """Sample nvidia-smi for every GPU and persist to gpu_metric_samples.
    Runs every 2 minutes. Purges samples older than 30 days."""
    from concurrent.futures import ThreadPoolExecutor

    with session_scope() as db:
        gpus = db.query(Gpu).all()

        def _fetch(g: Gpu):
            return g.id, fetch_gpu_metrics(
                g.host, g.gpu_index or 0, ssh_user=g.ssh_user, ssh_port=g.ssh_port, ssh_password=g.ssh_password
            )

        try:
            with ThreadPoolExecutor(max_workers=min(len(gpus), 8) or 1) as pool:
                results = list(pool.map(_fetch, gpus))
        except Exception:
            results = []

        now = utcnow()
        for gid, m in results:
            if m is None:
                continue
            db.add(GpuMetricSample(
                gpu_id=gid,
                sampled_at=now,
                util_gpu=m.get("util_gpu"),
                util_mem=m.get("util_mem"),
                mem_used=m.get("mem_used"),
                mem_total=m.get("mem_total"),
                temp_c=m.get("temp_c"),
                power_w=m.get("power_w"),
            ))

        # Purge samples older than 30 days
        cutoff = now - timedelta(days=30)
        db.query(GpuMetricSample).filter(GpuMetricSample.sampled_at < cutoff).delete()
        db.commit()


async def _activate_scheduled() -> None:
    """Activate SCHEDULED reservations whose start time has arrived."""
    with session_scope() as db:
        offered = activate_scheduled_reservations(db)
    for r in offered:
        await _notify_offered([r.id])


async def _tick():
    try:
        with session_scope() as db:
            offered_expired = expire_overdue(db)
            offered_skipped = skip_stale_offers(db)
            ids = [r.id for r in (offered_expired + offered_skipped)]
        if ids:
            await _notify_offered(ids)
        await _activate_scheduled()
        await _send_warning_emails()
        await _notify_watchers_of_free_gpus()
        await _check_idle_sessions_impl()
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
    scheduler.add_job(_collect_gpu_metrics, "interval", minutes=2, id="gpu-metrics-collector", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
