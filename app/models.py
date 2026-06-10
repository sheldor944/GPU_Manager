from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import String, Integer, Float, DateTime, Enum, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    USER = "USER"
    PI = "PI"


class GpuStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    MAINTENANCE = "MAINTENANCE"


class GpuVisibility(str, enum.Enum):
    PUBLIC = "PUBLIC"        # visible to all approved users
    RESTRICTED = "RESTRICTED"  # only PI + explicitly granted users


class ReservationStatus(str, enum.Enum):
    SCHEDULED = "SCHEDULED"           # future booking, activates at scheduled_start_at
    PENDING_APPROVAL = "PENDING_APPROVAL"  # waiting for admin to approve
    REJECTED = "REJECTED"             # admin rejected the request
    QUEUED = "QUEUED"
    OFFERED = "OFFERED"      # next in line, awaiting confirmation
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    SKIPPED = "SKIPPED"      # user did not confirm offer in time


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    google_sub: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    picture_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.USER, nullable=False)
    quota_hours: Mapped[float] = mapped_column(Float, default=40.0, nullable=False)
    used_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    # Email notification preferences
    email_on_offer: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_on_warning: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_on_watch: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    email_on_queue_move: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="user")
    api_tokens: Mapped[list["ApiToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def remaining_hours(self) -> float:
        return max(0.0, self.quota_hours - self.used_hours)

    @property
    def over_quota(self) -> bool:
        return self.used_hours >= self.quota_hours


class Gpu(Base):
    __tablename__ = "gpus"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    host: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ssh_user: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    ssh_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ssh_password: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    max_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gpu_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cpu_model: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    ram_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    connect_instructions: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_remote: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[GpuStatus] = mapped_column(Enum(GpuStatus), default=GpuStatus.AVAILABLE, nullable=False)
    visibility: Mapped[GpuVisibility] = mapped_column(Enum(GpuVisibility), default=GpuVisibility.RESTRICTED, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="gpu", cascade="all, delete-orphan")
    access_grants: Mapped[list["GpuAccess"]] = relationship(back_populates="gpu", cascade="all, delete-orphan")
    metric_samples: Mapped[list["GpuMetricSample"]] = relationship(back_populates="gpu", cascade="all, delete-orphan")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id"), nullable=False, index=True)
    requested_hours: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus), default=ReservationStatus.QUEUED, nullable=False, index=True
    )

    # Priority captured at queue time. Lower = served first.
    # 0 = within quota, 1 = over quota.
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    scheduled_start_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    offered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_end_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    warning_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Sort key within a GPU's queue (priority bucket). Defaults to the
    # queued-at timestamp so FIFO is the default; PI can swap to reorder.
    queue_sort_key: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship(back_populates="reservations")
    gpu: Mapped["Gpu"] = relationship(back_populates="reservations")


class GpuAccess(Base):
    """Explicit access grant: user can see and request a RESTRICTED GPU."""
    __tablename__ = "gpu_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    gpu: Mapped["Gpu"] = relationship(back_populates="access_grants")
    user: Mapped["User"] = relationship()


class AppSetting(Base):
    """Tiny key-value store for runtime-toggleable admin settings."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(512), nullable=False)


class Watch(Base):
    """A user wants to be pinged the next time a specific GPU is free."""
    __tablename__ = "watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ApiToken(Base):
    """Bearer token for programmatic API access."""
    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="api_tokens")


class AuditLog(Base):
    """Record of significant admin/system actions for accountability."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GpuMetricSample(Base):
    """Periodic nvidia-smi snapshot stored by the scheduler for time-series charts."""
    __tablename__ = "gpu_metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gpu_id: Mapped[int] = mapped_column(ForeignKey("gpus.id", ondelete="CASCADE"), nullable=False, index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    util_gpu: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    util_mem: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mem_total: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    temp_c: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    power_w: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    gpu: Mapped["Gpu"] = relationship(back_populates="metric_samples")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
