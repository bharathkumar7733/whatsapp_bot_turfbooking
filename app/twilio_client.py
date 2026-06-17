"""
Thin wrapper around the Twilio REST client.
All outbound WhatsApp messages go through send_whatsapp_message().
"""
from twilio.rest import Client
from .config import get_settings

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        s = get_settings()
        _client = Client(s.twilio_account_sid, s.twilio_auth_token)
    return _client


def send_whatsapp_message(to: str, body: str) -> None:
    """
    Send a WhatsApp message via Twilio.

    Args:
        to:   Recipient in 'whatsapp:+91XXXXXXXXXX' format.
        body: Plain text message body.
    """
    s = get_settings()
    get_client().messages.create(
        from_=s.twilio_whatsapp_number,
        to=to,
        body=body,
    )
