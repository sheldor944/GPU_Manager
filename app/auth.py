from __future__ import annotations

from typing import Optional

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import Role, User

oauth = OAuth()
oauth.register(
    name="google",
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """User is logged in and not disabled. May still be awaiting approval."""
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    return user


def require_approved_user(user: User = Depends(require_user)) -> User:
    if not user.is_approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account awaiting admin approval")
    return user


def require_pi(user: User = Depends(require_user)) -> User:
    if user.role != Role.PI:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="PI access required")
    return user


def email_allowed(email: str) -> bool:
    domains = settings.allowed_domains_list
    if not domains:
        return True
    domain = email.split("@", 1)[-1].lower()
    return domain in domains


def get_or_create_pi_user(db: Session) -> User:
    """Return (and create if missing) the user record for the configured PI_EMAIL.

    Used by the password-based admin login when the PI hasn't signed in with
    Google yet.
    """
    if not settings.PI_EMAIL:
        raise RuntimeError("PI_EMAIL is not configured")
    email = settings.PI_EMAIL.lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            name=email.split("@")[0],
            quota_hours=settings.DEFAULT_QUOTA_HOURS,
            role=Role.PI,
            is_approved=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        changed = False
        if user.role != Role.PI:
            user.role = Role.PI
            changed = True
        if not user.is_approved:
            user.is_approved = True
            changed = True
        if changed:
            db.commit()
            db.refresh(user)
    return user


def upsert_user_from_google(db: Session, info: dict) -> User:
    sub = info.get("sub")
    email = (info.get("email") or "").lower()
    name = info.get("name") or email.split("@")[0]
    picture = info.get("picture")

    is_pi = bool(settings.PI_EMAIL) and email == settings.PI_EMAIL.lower()

    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email,
            name=name,
            google_sub=sub,
            picture_url=picture,
            quota_hours=settings.DEFAULT_QUOTA_HOURS,
            role=Role.PI if is_pi else Role.USER,
            is_approved=is_pi,  # PI is auto-approved; everyone else awaits admin
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        changed = False
        if user.google_sub != sub and sub:
            user.google_sub = sub
            changed = True
        if picture and user.picture_url != picture:
            user.picture_url = picture
            changed = True
        if name and user.name != name:
            user.name = name
            changed = True
        # Promote the configured PI email if not already.
        if is_pi and user.role != Role.PI:
            user.role = Role.PI
            changed = True
        if is_pi and not user.is_approved:
            user.is_approved = True
            changed = True
        if changed:
            db.commit()
            db.refresh(user)
    return user
