"""
Owner message handler.

Standard commands (all regex-parsed, zero AI cost):
  view_bookings    — today's/tomorrow's/any date bookings
  block_slot       — block a time slot
  cancel_owner     — cancel any booking by ref
  confirm_payment  — confirm customer payment → sends customer confirmation
  booking_info     — details of a specific booking

Admin commands (prefixed with /admin, owner-only):
  /admin force cancel <ref>         — cancel any booking immediately
  /admin unlock slot <date> <time>  — remove slot blocks and cancel pending locks
  /admin reset session <phone>      — clear a customer's conversation state
  /admin show active users          — list all non-idle active sessions
  /admin resend payment <ref>       — re-send payment details to customer
"""
import logging
import re
from datetime import date, timedelta

from ..config import get_settings
from ..twilio_client import send_whatsapp_message
from .. import db
from ..parser import parse_message, extract_date, extract_time, extract_duration

logger = logging.getLogger(__name__)


# ── Formatting helpers (shared style with customer.py) ─────────────────────────

def _fmt_time(t: str) -> str:
    h, m = map(int, t.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def _fmt_date(d: str) -> str:
    dt = date.fromisoformat(d)
    today = date.today()
    if dt == today:
        return "Today"
    if dt == today + timedelta(days=1):
        return "Tomorrow"
    return dt.strftime("%a, %-d %b") if hasattr(dt, "strftime") else d


def _status_icon(status: str) -> str:
    return {
        "confirmed": "✅",
        "pending_payment": "⏳",
        "pending_owner": "📸",
        "cancelled": "❌"
    }.get(status, "❓")


# ── Owner bookings list ────────────────────────────────────────────────────────

def _format_bookings_list(rows: list, title: str) -> str:
    if not rows:
        return f"📋 *{title}*\n\nNo bookings."
    lines = [f"📋 *{title}*\n"]
    total_confirmed = 0
    total_pending = 0
    total_pending_owner = 0
    for r in rows:
        end_h = int(r["start_time"].split(":")[0]) + int(r["duration_hrs"])
        end_t = f"{end_h:02d}:00"
        icon = _status_icon(r["status"])
        lines.append(
            f"{icon} *{r['booking_ref']}*  {_fmt_time(r['start_time'])}–{_fmt_time(end_t)}"
            f"  {r['name']}"
        )
        if r["status"] == "confirmed":
            total_confirmed += 1
        elif r["status"] == "pending_payment":
            total_pending += 1
        elif r["status"] == "pending_owner":
            total_pending_owner += 1
    lines.append(
        f"\n✅ Confirmed: {total_confirmed}  ⏳ Unpaid: {total_pending}  📸 Awaiting Confirm: {total_pending_owner}"
    )
    return "\n".join(lines)


# ── Admin commands ─────────────────────────────────────────────────────────────

async def _handle_admin(sender: str, text: str) -> None:
    """
    Handle /admin prefixed commands.
    All admin actions are audit-logged.
    """
    cmd = text.strip().lower()

    # /admin force cancel <ref>
    m = re.match(r"/admin force cancel (bk\d+)", cmd)
    if m:
        ref = m.group(1).upper()
        row = db.cancel_booking(ref)
        if row:
            db.log_audit(
                actor=sender,
                action="admin_force_cancel",
                entity_type="booking",
                entity_id=ref,
                details=f"Force cancelled by owner"
            )
            # Notify the customer
            send_whatsapp_message(
                row["phone"],
                f"❌ Your booking *{ref}* has been cancelled by the turf.\n"
                f"Please contact us for more information. Type *book* to rebook."
            )
            send_whatsapp_message(sender, f"✅ *{ref}* force cancelled. Customer notified.")
        else:
            send_whatsapp_message(sender, f"❌ Booking *{ref}* not found.")
        return

    # /admin unlock slot <date> <time>  e.g. /admin unlock slot 2026-06-21 20:00
    m = re.match(r"/admin unlock slot (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})", cmd)
    if m:
        slot_date, slot_time = m.group(1), m.group(2)
        removed = db.unlock_slot(slot_date, slot_time)
        db.log_audit(
            actor=sender,
            action="admin_unlock_slot",
            entity_type="slot",
            entity_id=f"{slot_date}_{slot_time}",
            details=f"Slot unlocked: date={slot_date} time={slot_time}"
        )
        if removed:
            send_whatsapp_message(sender, f"🔓 Slot *{_fmt_time(slot_time)}* on *{slot_date}* unlocked.")
        else:
            send_whatsapp_message(sender, f"ℹ️ No blocks found for *{_fmt_time(slot_time)}* on *{slot_date}*.")
        return

    # /admin reset session <phone>  e.g. /admin reset session +919876543210
    m = re.match(r"/admin reset session (.+)", cmd)
    if m:
        raw_phone = m.group(1).strip()
        # Normalize phone format
        phone = raw_phone if raw_phone.startswith("whatsapp:") else f"whatsapp:{raw_phone}"
        from .. import session as sess
        sess.clear_session(phone)
        db.log_audit(
            actor=sender,
            action="admin_reset_session",
            entity_type="session",
            entity_id=phone,
            details=f"Session cleared by owner"
        )
        send_whatsapp_message(sender, f"🔄 Session cleared for *{phone}*.")
        return

    # /admin show active users
    if "/admin show active users" in cmd:
        from .. import session as sess
        sessions = sess.all_sessions()
        active = {p: s for p, s in sessions.items() if s.get("state", "idle") not in ("idle", "done")}
        if not active:
            send_whatsapp_message(sender, "👥 No active sessions right now.")
            return
        lines = [f"👥 *Active Sessions* ({len(active)})\n"]
        for phone, s in list(active.items())[:10]:
            data = s.get("data", {})
            state = s.get("state", "?")
            ref = data.get("booking_ref", "")
            lines.append(
                f"📞 ...{phone[-4:]}  State: {state}"
                + (f"  Ref: {ref}" if ref else "")
            )
        send_whatsapp_message(sender, "\n".join(lines))
        return

    # /admin resend payment <ref>
    m = re.match(r"/admin resend payment (bk\d+)", cmd)
    if m:
        ref = m.group(1).upper()
        bk = db.get_booking_by_ref(ref)
        if not bk:
            send_whatsapp_message(sender, f"❌ Booking *{ref}* not found.")
            return
        s = get_settings()
        end_h = int(bk["start_time"].split(":")[0]) + int(bk["duration_hrs"])
        end_t = f"{end_h:02d}:00"
        total = int(bk["duration_hrs"]) * s.turf_price_per_slot
        send_whatsapp_message(
            bk["phone"],
            f"💸 *Payment Reminder for {ref}*\n\n"
            f"📅 {_fmt_date(bk['date'])}  {_fmt_time(bk['start_time'])} – {_fmt_time(end_t)}\n"
            f"⏱ {int(bk['duration_hrs'])} hr(s)  |  💰 Total: ₹{total}\n\n"
            f"Pay Advance: ₹{s.advance_amount}\n"
            f"UPI ID: `{s.upi_id}`\n\n"
            f"Please send the payment screenshot here."
        )
        db.log_audit(
            actor=sender,
            action="admin_resend_payment",
            entity_type="payment",
            entity_id=ref,
            details=f"Payment details re-sent to {bk['phone']}"
        )
        send_whatsapp_message(sender, f"✅ Payment details resent to customer for *{ref}*.")
        return

    # Unknown admin command
    send_whatsapp_message(
        sender,
        "🔧 *Admin Commands:*\n\n"
        "`/admin force cancel BK101`\n"
        "`/admin unlock slot 2026-06-21 20:00`\n"
        "`/admin reset session +919876543210`\n"
        "`/admin show active users`\n"
        "`/admin resend payment BK101`"
    )


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_owner(sender: str, text: str) -> None:
    s = get_settings()

    # ── Admin commands ─────────────────────────────────────────────────────────
    if text.strip().lower().startswith("/admin"):
        await _handle_admin(sender, text)
        return

    parsed = parse_message(text, "owner")

    if not parsed:
        # Groq fallback for owners too
        from ..groq_fallback import groq_fallback
        reply = await groq_fallback(text, sender, "owner")
        send_whatsapp_message(sender, reply)
        return

    intent = parsed["intent"]

    # ── View bookings ──────────────────────────────────────────────────────────
    if intent == "view_bookings":
        d = parsed.get("date") or date.today().strftime("%Y-%m-%d")
        rows = db.get_bookings_by_date(d)
        title = f"{_fmt_date(d)} Bookings"
        send_whatsapp_message(sender, _format_bookings_list(rows, title))

    # ── Block slot ─────────────────────────────────────────────────────────────
    elif intent == "block_slot":
        d = parsed.get("date")
        t = parsed.get("time")
        dur = parsed.get("duration")
        if dur is None:
            dur = 1.0

        if not d:
            send_whatsapp_message(sender, "📅 Which date? e.g. *block tomorrow 8 PM*")
            return
        if not t:
            send_whatsapp_message(sender, "⏰ What time? e.g. *block tomorrow 8 PM*")
            return

        ok = db.block_slot(d, t, dur)
        end_h = int(t.split(":")[0]) + int(dur)
        end_t = f"{end_h:02d}:00"
        if ok:
            db.log_audit(
                actor=sender, action="block_slot",
                entity_type="slot", entity_id=f"{d}_{t}",
                details=f"{_fmt_date(d)} {_fmt_time(t)} {int(dur)}hr"
            )
            send_whatsapp_message(
                sender,
                f"🔒 Slot blocked:\n"
                f"📅 {_fmt_date(d)}  {_fmt_time(t)} – {_fmt_time(end_t)}\n"
                f"Duration: {int(dur)} hr(s)",
            )
        else:
            send_whatsapp_message(
                sender,
                f"❌ Cannot block — *{_fmt_time(t)}* on *{_fmt_date(d)}* already has a booking.",
            )

    # ── Cancel booking (owner) ────────────────────────────────────────────────
    elif intent == "cancel_owner":
        ref = parsed.get("booking_ref")
        if not ref:
            send_whatsapp_message(sender, "Which booking? e.g. *cancel BK104*")
            return

        row = db.cancel_booking(ref)
        if not row:
            send_whatsapp_message(sender, f"❌ Booking *{ref}* not found.")
            return

        db.log_audit(
            actor=sender, action="cancel_booking",
            entity_type="booking", entity_id=ref,
            details=f"Cancelled by owner: {row['name']} {row['date']} {row['start_time']}"
        )
        send_whatsapp_message(
            sender,
            f"✅ *{ref}* cancelled.\n"
            f"👤 {row['name']}  📅 {_fmt_date(row['date'])}  {_fmt_time(row['start_time'])}",
        )
        send_whatsapp_message(
            row["phone"],
            f"❌ Your booking *{ref}* on *{_fmt_date(row['date'])}* at "
            f"*{_fmt_time(row['start_time'])}* has been cancelled by the turf.\n"
            f"Please contact us for more info. Type *book* to rebook.",
        )

    # ── Confirm payment ────────────────────────────────────────────────────────
    elif intent == "confirm_payment":
        ref = parsed.get("booking_ref")
        if not ref:
            send_whatsapp_message(sender, "Which booking? e.g. *confirm BK104*")
            return

        ok = db.confirm_booking(ref)
        if not ok:
            bk = db.get_booking_by_ref(ref)
            if bk and bk["status"] == "confirmed":
                send_whatsapp_message(sender, f"ℹ️ *{ref}* is already confirmed.")
            else:
                send_whatsapp_message(sender, f"❌ *{ref}* not found or not in pending state.")
            return

        bk = db.get_booking_by_ref(ref)
        send_whatsapp_message(
            sender,
            f"✅ *{ref}* confirmed!\n"
            f"👤 {bk['name']}  📅 {_fmt_date(bk['date'])}  {_fmt_time(bk['start_time'])}",
        )
        end_h = int(bk["start_time"].split(":")[0]) + int(bk["duration_hrs"])
        end_t = f"{end_h:02d}:00"
        send_whatsapp_message(
            bk["phone"],
            f"🎉 *Booking Confirmed!*\n\n"
            f"📋 Ref: *{ref}*\n"
            f"👤 {bk['name']}\n"
            f"📅 {_fmt_date(bk['date'])}\n"
            f"⏰ {_fmt_time(bk['start_time'])} – {_fmt_time(end_t)}\n\n"
            f"See you on the turf! 🏏",
        )

    # ── Booking info ───────────────────────────────────────────────────────────
    elif intent == "booking_info":
        ref = parsed.get("booking_ref")
        if not ref:
            send_whatsapp_message(sender, "Which booking? e.g. *show BK104*")
            return

        bk = db.get_booking_by_ref(ref)
        if not bk:
            send_whatsapp_message(sender, f"❌ Booking *{ref}* not found.")
            return

        end_h = int(bk["start_time"].split(":")[0]) + int(bk["duration_hrs"])
        end_t = f"{end_h:02d}:00"
        utr_line = f"💳 UTR: {bk['payment_utr']}\n" if bk["payment_utr"] else ""
        screenshot_line = f"🖼 Screenshot: {bk['screenshot_url']}\n" if bk["screenshot_url"] else ""
        send_whatsapp_message(
            sender,
            f"📋 *{ref}* — {_status_icon(bk['status'])} {bk['status'].replace('_', ' ')}\n\n"
            f"👤 {bk['name']}\n"
            f"📞 {bk['phone']}\n"
            f"📅 {_fmt_date(bk['date'])}\n"
            f"⏰ {_fmt_time(bk['start_time'])} – {_fmt_time(end_t)}\n"
            f"⏱ {int(bk['duration_hrs'])} hr(s)\n"
            f"{utr_line}{screenshot_line}"
            f"🕐 Booked at: {bk['created_at']}",
        )
