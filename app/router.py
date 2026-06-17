"""
WhatsApp webhook router.
Twilio POSTs here on every inbound message.

Hardening applied here:
  - Body truncated to 1000 chars (prevents LLM prompt injection via huge text)
  - Non-printable / control characters stripped
  - Empty-body messages handled gracefully
  - Structured request log: timestamp | masked_phone | body_preview | has_media
"""
import logging
import re
import unicodedata

from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse

from .config import get_settings
from .handlers.customer import handle_customer
from .handlers.owner import handle_owner

logger = logging.getLogger(__name__)
router = APIRouter()

_TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
MAX_BODY_LEN = 1000


def _sanitise(text: str) -> str:
    """
    Strip control characters and normalise unicode.
    Keeps emoji (they are valid WhatsApp content — parser strips them before regex).
    """
    # Remove control characters except newline/tab
    cleaned = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\n\t"
    )
    # Truncate
    return cleaned[:MAX_BODY_LEN].strip()


def _mask(phone: str) -> str:
    """'whatsapp:+919876543210' → 'wa:+91****3210'"""
    digits = re.sub(r"\D", "", phone)
    return f"wa:+{digits[:2]}****{digits[-4:]}" if len(digits) >= 6 else "wa:???"


@router.post("/webhook", response_class=PlainTextResponse)
async def whatsapp_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(default=""),
    MediaUrl0: str = Form(default=""),
    NumMedia: str = Form(default="0"),
):
    """
    Entry point for all inbound WhatsApp messages from Twilio.
    Returns empty TwiML — actual replies sent via REST API.
    """
    settings = get_settings()
    sender    = From.strip()
    text      = _sanitise(Body)
    media_url = MediaUrl0.strip() if int(NumMedia or 0) > 0 else ""

    logger.info(
        "INBOUND | from=%s | body=%r | media=%s",
        _mask(sender),
        text[:60],
        bool(media_url),
    )

    try:
        if settings.is_owner(sender):
            await handle_owner(sender, text)
        else:
            await handle_customer(sender, text, media_url)
    except Exception as exc:
        # Logged + handled by global_exception_handler in main.py
        raise exc

    return PlainTextResponse(_TWIML_EMPTY, media_type="application/xml")


@router.get("/health")
async def health():
    """Render/uptime-robot health check endpoint."""
    return {"status": "ok"}
