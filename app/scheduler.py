"""
APScheduler background jobs — runs inside the FastAPI process.

Jobs:
  1. send_reminders()         — every 5 min, WhatsApp reminder 30 min before slot
  2. expire_pending()         — every 5 min, cancel pending_payment bookings after 5 min
  3. check_owner_pending()    — every 30 min, escalate pending_owner bookings:
                                  +6h  → remind owner
                                  +18h → final warning to owner
                                  +24h → auto-cancel, notify customer, release slot
  4. daily_cleanup_analytics() — once daily at midnight:
                                  cleanup_full() + update_daily_metrics()
"""
import logging
from datetime import date, timedelta, datetime

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)


# ── Reminder job ───────────────────────────────────────────────────────────────

def send_reminders() -> None:
    """
    Find confirmed bookings starting within 35 minutes that haven't been
    reminded yet. Send a WhatsApp message and set reminder_sent = 1.
    """
    from .db import get_upcoming_bookings, mark_reminder_sent
    from .twilio_client import send_whatsapp_message
    from .config import get_settings

    s = get_settings()
    bookings = get_upcoming_bookings(within_minutes=35)

    for bk in bookings:
        try:
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
    Does NOT touch pending_owner — those are handled by check_owner_pending().
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


# ── Owner pending escalation job ───────────────────────────────────────────────

def check_owner_pending() -> None:
    """
    Escalate or auto-cancel bookings in 'pending_owner' status.

    SLA from screenshot_at:
      < 6h:  no action
      6h:    first reminder to owner
      18h:   final warning to owner
      24h+:  auto-cancel, notify customer, log audit
    """
    from .db import get_pending_owner_bookings, cancel_booking, log_audit, log_event
    from .twilio_client import safe_notify_owner, send_whatsapp_message
    from .config import get_settings

    s = get_settings()
    bookings = get_pending_owner_bookings()
    now_utc = datetime.utcnow()

    for bk in bookings:
        try:
            screenshot_at = datetime.strptime(bk["screenshot_at"], "%Y-%m-%d %H:%M:%S")
            elapsed_hours = (now_utc - screenshot_at).total_seconds() / 3600

            ref = bk["booking_ref"]
            h, m = map(int, bk["start_time"].split(":"))
            suffix = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            time_str = f"{h12}:{m:02d} {suffix}"

            # ── +24h: auto-cancel ──────────────────────────────────────────
            if elapsed_hours >= 24:
                cancel_booking(ref)
                log_audit(
                    actor="system",
                    action="auto_cancel_owner_timeout",
                    entity_type="booking",
                    entity_id=ref,
                    details=f"Auto-cancelled after 24h without owner confirmation"
                )
                log_event(bk["phone"], "booking_cancelled", {
                    "booking_ref": ref,
                    "reason": "owner_timeout_24h"
                })
                # Notify customer
                send_whatsapp_message(
                    bk["phone"],
                    f"❌ Unfortunately your booking *{ref}* could not be confirmed "
                    f"in time.\n\n"
                    f"Your advance will be refunded. Please contact the turf directly "
                    f"or type *book* to make a new booking."
                )
                # Alert owners
                safe_notify_owner(
                    s.owner_list,
                    f"⚠️ *Auto-cancelled: {ref}*\n\n"
                    f"Booking was not confirmed within 24 hours.\n"
                    f"👤 {bk['name']}  📅 {bk['date']}  {time_str}\n\n"
                    f"Customer has been notified. Please arrange refund if payment was received."
                )
                logger.warning("Auto-cancelled pending_owner booking %s after 24h", ref)

            # ── +18h: final warning ────────────────────────────────────────
            elif 18 <= elapsed_hours < 24:
                # Only send once — check if we already sent 18h reminder via audit log
                from .db import get_conn
                with get_conn() as conn:
                    existing = conn.execute(
                        "SELECT id FROM audit_logs WHERE entity_id = ? AND action = 'owner_reminder_18h'",
                        (ref,)
                    ).fetchone()
                if not existing:
                    safe_notify_owner(
                        s.owner_list,
                        f"🚨 *FINAL REMINDER — Action Required*\n\n"
                        f"Booking *{ref}* has been waiting 18 hours for your confirmation.\n"
                        f"👤 {bk['name']}  📅 {bk['date']}  {time_str}\n\n"
                        f"⏰ *Auto-cancel in 6 hours* if not confirmed.\n"
                        f"Type *confirm {ref}* now."
                    )
                    log_audit(
                        actor="system",
                        action="owner_reminder_18h",
                        entity_type="booking",
                        entity_id=ref,
                        details="18h final reminder sent to owner"
                    )
                    logger.info("18h final reminder sent for %s", ref)

            # ── +6h: first reminder ────────────────────────────────────────
            elif 6 <= elapsed_hours < 18:
                from .db import get_conn
                with get_conn() as conn:
                    existing = conn.execute(
                        "SELECT id FROM audit_logs WHERE entity_id = ? AND action = 'owner_reminder_6h'",
                        (ref,)
                    ).fetchone()
                if not existing:
                    safe_notify_owner(
                        s.owner_list,
                        f"📸 *Payment Awaiting Confirmation*\n\n"
                        f"Booking *{ref}* received a payment screenshot 6 hours ago.\n"
                        f"👤 {bk['name']}  📅 {bk['date']}  {time_str}\n\n"
                        f"Type *confirm {ref}* to approve."
                    )
                    log_audit(
                        actor="system",
                        action="owner_reminder_6h",
                        entity_type="booking",
                        entity_id=ref,
                        details="6h reminder sent to owner"
                    )
                    logger.info("6h reminder sent for %s", ref)

        except Exception as exc:
            logger.error("check_owner_pending error for %s: %s", bk.get("booking_ref", "?"), exc)


# ── Daily cleanup & analytics job ─────────────────────────────────────────────

def daily_cleanup_analytics() -> None:
    """
    Heavy daily maintenance:
      1. cleanup_full() — archive old records
      2. update_daily_metrics() — snapshot KPIs
    """
    from .db import cleanup_full, update_daily_metrics
    logger.info("Running daily cleanup and analytics update...")
    cleanup_full()
    update_daily_metrics()
    logger.info("Daily maintenance complete.")


# ── Scheduler setup ────────────────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    """
    Start the background scheduler with all jobs.
    Called once at app startup from main.py lifespan.
    """
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    scheduler.add_job(
        send_reminders,
        trigger="interval",
        minutes=5,
        id="send_reminders",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        expire_pending,
        trigger="interval",
        minutes=5,
        id="expire_pending",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        check_owner_pending,
        trigger="interval",
        minutes=30,
        id="check_owner_pending",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.add_job(
        daily_cleanup_analytics,
        trigger="cron",
        hour=0,
        minute=30,
        id="daily_cleanup_analytics",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info(
        "Scheduler started — reminders every 5min, expiry every 5min, "
        "owner escalation every 30min, daily cleanup at 00:30"
    )
    return scheduler
