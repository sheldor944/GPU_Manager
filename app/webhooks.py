"""Outbound webhook notifications (Slack, Discord, custom HTTP endpoints).

The PI configures a webhook URL in admin settings (key: "webhook_url").
On key events, we POST a JSON payload to that URL.

Slack/Discord both support Incoming Webhooks with a `{"text": "..."}` payload.
Custom endpoints receive a richer JSON body.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .settings_store import get_setting

logger = logging.getLogger(__name__)

WEBHOOK_URL_KEY = "webhook_url"


async def _post(url: str, payload: dict) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload)
    except Exception as exc:
        logger.warning("Webhook POST failed: %s", exc)


async def send_webhook(text: str, detail: dict | None = None, db=None) -> None:
    """Send a notification to the configured webhook URL (if any)."""
    url: Optional[str] = None
    if db is not None:
        url = get_setting(db, WEBHOOK_URL_KEY)
    if not url:
        return
    payload = {"text": text}
    if detail:
        payload["detail"] = detail
    # Slack/Discord compatible minimal payload — just `text`
    await _post(url, {"text": text})
