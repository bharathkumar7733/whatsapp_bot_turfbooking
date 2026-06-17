"""
Turf Booking WhatsApp Agent — FastAPI entry point.

Hardening:
  - Global exception handler returns a graceful WhatsApp reply instead of 500
  - Request logging with masked phone numbers
  - Input sanitised before reaching handlers (router.py)
"""
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from .config import get_settings
from .db import init_db
from .router import router
from .scheduler import start_scheduler


# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("app.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── App lifespan ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "Starting *%s* agent [env=%s, owners=%d]",
        settings.turf_name,
        settings.app_env,
        len(settings.owner_list),
    )
    init_db()
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)
    logger.info("Agent shut down cleanly.")


app = FastAPI(
    title="Turf Booking WhatsApp Agent",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,    # disable Swagger in prod
    redoc_url=None,
)

app.include_router(router)


# ── Global exception handler ───────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch any unhandled exception.
    Attempt to send a graceful WhatsApp reply so the user doesn't get silence.
    """
    logger.exception("Unhandled exception on %s: %s", request.url.path, exc)

    try:
        form = await request.form()
        sender = str(form.get("From", ""))
        if sender:
            from .twilio_client import send_whatsapp_message
            send_whatsapp_message(
                sender,
                "Sorry, something went wrong on our end. Please try again in a moment. 🙏",
            )
    except Exception:
        pass  # best-effort; don't recurse into another exception

    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        status_code=200,          # always 200 to Twilio — never let it retry
        media_type="application/xml",
    )
