"""
WhatsApp webhook router.
Twilio POSTs here on every inbound message.

Production hardening applied here:
  - Twilio signature validation (RequestValidator) — prevents spoofed webhooks
  - ALLOW_SIGNATURE_BYPASS: emergency override with mandatory audit logging
  - Idempotency: acquire_message_lock() with worker_id before processing
    → 'processing' → 'done' / 'failed' lifecycle prevents double-booking on retries
  - cleanup_fast() on every request — prunes expired sessions + locks (<10ms)
  - Body truncated to 1000 chars (prevents LLM prompt injection via huge text)
  - Non-printable / control characters stripped
  - Empty-body messages handled gracefully
  - Structured request log: timestamp | masked_phone | body_preview | has_media
  - /health endpoint: checks db, twilio, scheduler connectivity
  - /ready endpoint: simple liveness probe for Render
"""
import logging
import re
import unicodedata
import uuid

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

from .config import get_settings
from .handlers.customer import handle_customer
from .handlers.owner import handle_owner
from . import db

logger = logging.getLogger(__name__)
router = APIRouter()

_TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
MAX_BODY_LEN = 1000

# Shared scheduler reference — set by main.py on startup
_scheduler = None


def set_scheduler(s) -> None:
    """Called from main.py lifespan to allow health check to inspect scheduler."""
    global _scheduler
    _scheduler = s


def _sanitise(text: str) -> str:
    """
    Strip control characters and normalise unicode.
    Keeps emoji (they are valid WhatsApp content — parser strips them before regex).
    """
    cleaned = "".join(
        ch for ch in text
        if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\n\t"
    )
    return cleaned[:MAX_BODY_LEN].strip()


def _mask(phone: str) -> str:
    """'whatsapp:+919876543210' → 'wa:+91****3210'"""
    digits = re.sub(r"\D", "", phone)
    return f"wa:+{digits[:2]}****{digits[-4:]}" if len(digits) >= 6 else "wa:???"


def _validate_twilio_signature(request: Request, form_data: dict) -> bool:
    """
    Validate Twilio's X-Twilio-Signature header.
    Reconstructs the URL using X-Forwarded-* headers (Render/proxy-aware).

    Returns True if valid (or if validation is disabled in dev).
    """
    from twilio.request_validator import RequestValidator
    s = get_settings()

    # Skip validation in dev/test mode (unless explicitly enabled)
    if not s.validate_twilio_signature:
        return True

    # Emergency bypass — log a critical warning whenever this is used in prod
    if s.allow_signature_bypass:
        logger.critical(
            "⚠️  TWILIO SIGNATURE BYPASS ACTIVE — this should never happen in normal operation. "
            "Disable ALLOW_SIGNATURE_BYPASS immediately after recovery."
        )
        db.log_audit(
            actor="system",
            action="signature_bypass_used",
            entity_type="security",
            entity_id=None,
            details="ALLOW_SIGNATURE_BYPASS=true — signature validation skipped"
        )
        return True

    # Reconstruct the original URL as Twilio sees it (handles Render proxy)
    forwarded_proto = request.headers.get("x-forwarded-proto", "https")
    forwarded_host  = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    url = f"{forwarded_proto}://{forwarded_host}{request.url.path}"

    signature = request.headers.get("x-twilio-signature", "")
    validator = RequestValidator(s.twilio_auth_token)
    return validator.validate(url, form_data, signature)


@router.post("/webhook", response_class=PlainTextResponse)
async def whatsapp_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(default=""),
    MediaUrl0: str = Form(default=""),
    NumMedia: str = Form(default="0"),
    MessageSid: str = Form(default=""),
):
    """
    Entry point for all inbound WhatsApp messages from Twilio.
    Returns empty TwiML — actual replies sent via REST API.
    """
    # ── Fast cleanup on every request ─────────────────────────────────────────
    db.cleanup_fast()

    # ── Signature validation ───────────────────────────────────────────────────
    form_data = dict(await request.form())
    if not _validate_twilio_signature(request, form_data):
        logger.warning("REJECTED: Invalid Twilio signature from %s", _mask(From))
        raise HTTPException(status_code=403, detail="Invalid Twilio Signature")

    # ── Idempotency lock ───────────────────────────────────────────────────────
    worker_id = str(uuid.uuid4())
    if MessageSid:
        locked = db.acquire_message_lock(MessageSid, worker_id)
        if not locked:
            logger.info("DUPLICATE: MessageSid=%s already processed — skipping", MessageSid[:16])
            return PlainTextResponse(_TWIML_EMPTY, media_type="application/xml")

    # ── Request parsing ────────────────────────────────────────────────────────
    settings  = get_settings()
    sender    = From.strip()
    text      = _sanitise(Body)
    media_url = MediaUrl0.strip() if int(NumMedia or 0) > 0 else ""

    logger.info(
        "INBOUND | from=%s | body=%r | media=%s",
        _mask(sender),
        text[:60],
        bool(media_url),
    )

    # Log inbound message for conversation history
    db.log_message(sender, "customer", text)

    # ── Route & process ────────────────────────────────────────────────────────
    try:
        if settings.is_owner(sender):
            await handle_owner(sender, text)
        else:
            await handle_customer(sender, text, media_url)

        # Mark as done — successful processing
        if MessageSid:
            db.finish_message_processing(MessageSid, "done")

    except Exception as exc:
        # Mark as failed so it can be retried if needed
        if MessageSid:
            db.finish_message_processing(MessageSid, "failed")
        raise exc

    return PlainTextResponse(_TWIML_EMPTY, media_type="application/xml")


# ── Health checks ──────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    """
    Deep health check for Render / uptime monitoring.
    Verifies DB connectivity, Twilio client init, and scheduler state.
    Returns 200 if all OK, 503 if any component is down.
    """
    status = {"db": "ok", "twilio": "ok", "scheduler": "ok"}
    code = 200

    # Check DB
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as e:
        status["db"] = f"error: {e}"
        code = 503

    # Check Twilio client (just checks config — no API call)
    try:
        s = get_settings()
        if not s.twilio_account_sid or not s.twilio_auth_token:
            raise ValueError("Missing Twilio credentials")
    except Exception as e:
        status["twilio"] = f"error: {e}"
        code = 503

    # Check scheduler
    try:
        if _scheduler is None or not _scheduler.running:
            status["scheduler"] = "not running"
            code = 503
    except Exception as e:
        status["scheduler"] = f"error: {e}"
        code = 503

    return JSONResponse(status, status_code=code)


@router.get("/ready")
async def ready():
    """
    Liveness / readiness probe for Render.
    Render checks this before routing traffic after a restart.
    Only verifies DB is reachable.
    """
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return JSONResponse({"ready": True})
    except Exception as e:
        return JSONResponse({"ready": False, "error": str(e)}, status_code=503)
