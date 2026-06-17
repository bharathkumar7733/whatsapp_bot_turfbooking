"""
APScheduler background job — runs inside the FastAPI process.

Jobs:
  1. send_reminders()      — every 5 min, WhatsApp reminder 30 min before slot
  2. expire_pending()      — every 5 min, cancel bookings unpaid after 5 min
"""
import logging
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)


# ── Reminder job ───────────────────────────────────────────────────────────────

def send_reminders() -> None:
    """
    Find confirmed bookings starting within 35 minutes that haven't been
    reminded yet. Send a WhatsApp message and set reminder_sent = 1.
    Skips cancelled bookings automatically (get_upcoming_bookings filters them).
    """
    from .db import get_upcoming_bookings, mark_reminder_sent
    from .twilio_client import send_whatsapp_message
    from .config import get_settings

    s = get_settings()
    bookings = get_upcoming_bookings(within_minutes=35)

    for bk in bookings:
        try:
            # Format time for display
            h, m = map(int, bk["start_time"].split(":"))
            suffix = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            time_str = f"{h12}:{m:02d} {suffix}"

            dt = date.fromisoformat(bk["date"])
            today = date.today()
            date_str = "Today" if dt == today else "Tomorrow"

            send_whatsapp_message(
                bk["phone"],
                f"⏰ *Reminder!*\n\n"
                f"Your turf booking is in ~30 minutes.\n\n"
                f"📋 *{bk['booking_ref']}*\n"
                f"📅 {date_str}  {time_str}\n"
                f"⏱ {int(bk['duration_hrs'])} hr(s)\n\n"
                f"See you on the turf! 🏏 — {s.turf_name}",
            )
            mark_reminder_sent(bk["booking_ref"])
            logger.info("Reminder sent: %s → %s", bk["booking_ref"], bk["phone"][-4:])

        except Exception as exc:
            logger.error(
                "Failed to send reminder for %s: %s", bk["booking_ref"], exc
            )


# ── Expiry job ─────────────────────────────────────────────────────────────────

def expire_pending() -> None:
    """
    Cancel bookings that are still pending_payment after 5 minutes.
    Notifies the customer their slot has been released.
    """
    from .db import expire_pending_bookings
    from .twilio_client import send_whatsapp_message
    from .config import get_settings

    s = get_settings()
    expired = expire_pending_bookings(older_than_minutes=5)

    for row in expired:
        try:
            send_whatsapp_message(
                row["phone"],
                f"⌛ Your booking slot has been *released* because no payment "
                f"screenshot was received within 5 minutes.\n\n"
                f"Type *book* to make a new booking.",
            )
            logger.info("Pending booking expired: %s", row["booking_ref"])
        except Exception as exc:
            logger.error("Failed to notify expired booking %s: %s", row["booking_ref"], exc)


# ── Scheduler setup ────────────────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    """
    Start the background scheduler with both jobs.
    Called once at app startup from main.py lifespan.
    """
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    scheduler.add_job(
        send_reminders,
        trigger="interval",
        minutes=5,
        id="send_reminders",
        replace_existing=True,
        max_instances=1,       # never run two reminder jobs in parallel
    )

    scheduler.add_job(
        expire_pending,
        trigger="interval",
        minutes=5,
        id="expire_pending",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info("Scheduler started — reminder job every 5 min, expiry job every 5 min")
    return scheduler
