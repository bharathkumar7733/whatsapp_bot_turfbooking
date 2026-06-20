"""
Thin wrapper around the Twilio REST client.
All outbound WhatsApp messages go through send_whatsapp_message().

Production additions:
  - Logs every outbound message to the 'messages' DB table.
  - safe_notify_owner() — try/except wrapper for owner alerts.
    Never raises: a Twilio failure should never crash the app.
"""
import logging
from twilio.rest import Client
from .config import get_settings

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        s = get_settings()
        _client = Client(s.twilio_account_sid, s.twilio_auth_token)
    return _client


def send_whatsapp_message(to: str, body: str) -> None:
    """
    Send a WhatsApp message via Twilio and log it to the messages table.

    Args:
        to:   Recipient in 'whatsapp:+91XXXXXXXXXX' format.
        body: Plain text message body.
    """
    from . import db
    s = get_settings()
    get_client().messages.create(
        from_=s.twilio_whatsapp_number,
        to=to,
        body=body,
    )
    # Log every outbound message for conversation history + analytics
    db.log_message(to, "bot", body)


def safe_notify_owner(owner_phones: list, message: str) -> None:
    """
    Send a WhatsApp alert to all owner numbers.
    Completely swallows Twilio exceptions — an alert failure must NEVER
    propagate into the main request handler and cause a 500.

    Use this for:
      - Backend exception alerts
      - Owner escalation reminders
      - System status notifications
    """
    for phone in owner_phones:
        try:
            send_whatsapp_message(phone, message)
            logger.info("Owner alert sent to %s", phone[-4:])
        except Exception as exc:
            logger.error(
                "safe_notify_owner failed for %s: %s — alert suppressed, app continues",
                phone[-4:], exc
            )
