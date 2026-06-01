from __future__ import annotations

import logging
from email.message import EmailMessage

import aiosmtplib

from .config import settings

logger = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body: str) -> bool:
    if not settings.EMAIL_ENABLED:
        logger.info("[email disabled] would send to %s: %s", to, subject)
        return False
    if not settings.SMTP_USERNAME or not settings.SMTP_PASSWORD:
        logger.warning("SMTP credentials not configured; skipping email to %s", to)
        return False

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
            timeout=15,
        )
        return True
    except Exception:
        logger.exception("Failed to send email to %s", to)
        return False
