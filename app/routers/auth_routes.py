import hmac

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth import email_allowed, get_or_create_pi_user, oauth, upsert_user_from_google
from ..config import settings
from ..database import get_db

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/login")
async def login(request: Request):
    redirect_uri = str(request.url_for("auth_google_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback", name="auth_google_callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"OAuth error: {e}")

    info = token.get("userinfo")
    if info is None:
        # Fallback: hit the userinfo endpoint directly.
        try:
            resp = await oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo", token=token)
            info = resp.json()
        except Exception:
            info = None
    if not info or not info.get("email"):
        raise HTTPException(status_code=400, detail="Could not read account info from Google")

    if not info.get("email_verified", True):
        raise HTTPException(status_code=403, detail="Your Google email is not verified")

    email = info["email"].lower()
    if not email_allowed(email):
        raise HTTPException(status_code=403, detail="Your email domain is not allowed")

    user = upsert_user_from_google(db, info)
    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/admin-login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "settings": settings, "user": None, "error": None},
    )


@router.post("/admin-login", response_class=HTMLResponse)
def admin_login_submit(
    request: Request,
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin password login is disabled. Set ADMIN_PASSWORD in .env.")
    if not settings.PI_EMAIL:
        raise HTTPException(status_code=503, detail="PI_EMAIL is not configured.")
    if not hmac.compare_digest(password, settings.ADMIN_PASSWORD):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "settings": settings, "user": None, "error": "Incorrect password."},
            status_code=401,
        )
    user = get_or_create_pi_user(db)
    request.session["user_id"] = user.id
    return RedirectResponse(url="/admin", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)
