"""
Customer message handler — full end-to-end booking flow.

Flow:
  idle → (book intent) → awaiting_date → awaiting_time
       → awaiting_duration → awaiting_name → awaiting_payment → done

At any point the customer can also:
  - check availability
  - cancel their own booking
  - check booking status
  - say payment done (if already in awaiting_payment with a booking_ref)
"""
import logging
import re
from datetime import date, timedelta

from ..config import get_settings
from ..twilio_client import send_whatsapp_message
from .. import db, session as sess
from ..parser import parse_message, extract_date, extract_time, extract_duration

logger = logging.getLogger(__name__)


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_time(t: str) -> str:
    """'20:00' → '8:00 PM'"""
    h, m = map(int, t.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def _fmt_date(d: str) -> str:
    """'2025-08-01' → 'Fri, 1 Aug'"""
    dt = date.fromisoformat(d)
    today = date.today()
    if dt == today:
        return "Today"
    if dt == today + timedelta(days=1):
        return "Tomorrow"
    return dt.strftime("%a, %-d %b") if hasattr(dt, "strftime") else d


def _slots_msg(free: list[str]) -> str:
    if not free:
        return "❌ No free slots available."
    lines = ["🟢 *Available Slots:*"]
    for t in free:
        lines.append(f"  • {_fmt_time(t)}")
    return "\n".join(lines)


def _booking_confirmation_msg(ref: str, d: str, t: str, dur: float, name: str, s: 'Settings') -> str:
    end_h = int(t.split(":")[0]) + int(dur)
    end_t = f"{end_h:02d}:00"
    return (
        f"✅ *Booking Created!*\n\n"
        f"📋 Ref: *{ref}*\n"
        f"👤 Name: {name}\n"
        f"📅 Date: {_fmt_date(d)}\n"
        f"⏰ Time: {_fmt_time(t)} – {_fmt_time(end_t)}\n"
        f"⏱ Duration: {int(dur)} hr(s)\n\n"
        f"💸 *Pay Advance: ₹{s.advance_amount}*\n"
        f"UPI ID: `{s.upi_id}`\n\n"
        f"After payment, *send the screenshot here* with your UTR number.\n"
        f"Booking holds for *5 minutes* — pay quickly! ⏳"
    )


def _owner_notify_msg(ref: str, phone: str, name: str, d: str, t: str, dur: float, screenshot_url: str) -> str:
    end_h = int(t.split(":")[0]) + int(dur)
    end_t = f"{end_h:02d}:00"
    return (
        f"💰 *Payment Received — Action Required*\n\n"
        f"📋 Ref: *{ref}*\n"
        f"👤 {name}  |  📞 {phone}\n"
        f"📅 {_fmt_date(d)}  {_fmt_time(t)} – {_fmt_time(end_t)}\n"
        f"🖼 Screenshot: {screenshot_url}\n\n"
        f"Type *confirm {ref}* to confirm booking."
    )


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_customer(sender: str, text: str, media_url: str = "") -> None:
    s = get_settings()
    state = sess.get_state(sender)
    data  = sess.get_data(sender)

    # ── Screenshot received ────────────────────────────────────────────────────
    if media_url and state == "awaiting_payment":
        ref = data.get("booking_ref")
        if not ref:
            send_whatsapp_message(sender, "⚠️ Couldn't find your booking. Type *book* to start again.")
            sess.clear_session(sender)
            return

        # Extract UTR from text if present
        utr = ""
        utr_match = re.search(r"\b(\d{10,})\b", text)
        if utr_match:
            utr = utr_match.group(1)

        # Update DB — mark screenshot received (still pending_payment until owner confirms)
        db.confirm_booking.__module__  # just a touch to ensure import
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE bookings SET screenshot_url=?, payment_utr=? WHERE booking_ref=?",
                (media_url, utr, ref),
            )

        # Notify all owners
        bk = db.get_booking_by_ref(ref)
        if bk:
            for owner_phone in s.owner_list:
                send_whatsapp_message(
                    owner_phone,
                    _owner_notify_msg(
                        ref, sender, bk["name"],
                        bk["date"], bk["start_time"], bk["duration_hrs"],
                        media_url,
                    ),
                )

        send_whatsapp_message(
            sender,
            f"📸 Screenshot received for *{ref}*!\n"
            f"Waiting for owner confirmation. You'll get a message once confirmed. 🙏",
        )
        sess.set_state(sender, "done")
        return

    # ── Parse intent ──────────────────────────────────────────────────────────
    parsed = parse_message(text, "customer")

    # ── In-flow: state-machine responses (collect missing info) ───────────────
    if state == "awaiting_date":
        d = extract_date(text)
        if d:
            sess.set_state(sender, "awaiting_time", {"date": d})
            send_whatsapp_message(sender, f"📅 Got it — *{_fmt_date(d)}*.\nWhat time? (e.g. *8 PM*, *20:00*)")
        else:
            send_whatsapp_message(sender, "📅 Which date? (e.g. *today*, *tomorrow*, *20 June*)")
        return

    if state == "awaiting_time":
        t = extract_time(text)
        if t:
            sess.set_state(sender, "awaiting_duration", {"time": t})
            send_whatsapp_message(sender, f"⏰ Time: *{_fmt_time(t)}*.\nHow many hours? (1, 2, or 3)")
        else:
            send_whatsapp_message(sender, "⏰ What time? (e.g. *8 PM*, *9:30 PM*, *morning*)")
        return

    if state == "awaiting_duration":
        dur = extract_duration(text)
        sess.set_state(sender, "awaiting_name", {"duration": dur})
        send_whatsapp_message(sender, f"⏱ Duration: *{int(dur)} hr(s)*.\nWhat's your name?")
        return

    if state == "awaiting_name":
        name = text.strip().title()
        if len(name) < 2:
            send_whatsapp_message(sender, "Please tell me your name so I can make the booking.")
            return
        d, t, dur = data.get("date"), data.get("time"), data.get("duration", 1.0)

        # Availability check before locking
        if not db.check_slot_available(d, t, dur):
            free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
            msg = (
                f"❌ Slot {_fmt_time(t)} on {_fmt_date(d)} just got taken!\n\n"
                + _slots_msg(free[:5])
                + "\n\nType *book* to choose another slot."
            )
            send_whatsapp_message(sender, msg)
            sess.clear_session(sender)
            return

        ref = db.create_booking(sender, name, d, t, dur)
        if not ref:
            send_whatsapp_message(sender, "❌ Sorry, that slot is no longer available. Type *book* to try again.")
            sess.clear_session(sender)
            return

        sess.set_state(sender, "awaiting_payment", {"booking_ref": ref, "name": name})
        send_whatsapp_message(sender, _booking_confirmation_msg(ref, d, t, dur, name, s))
        return

    if state == "awaiting_payment":
        # They typed something but didn't send a screenshot
        if parsed and parsed["intent"] == "payment_done":
            send_whatsapp_message(
                sender,
                "📸 Please *send the screenshot* of your UPI payment here.\n"
                f"UPI ID: `{s.upi_id}`",
            )
        else:
            ref = data.get("booking_ref", "")
            send_whatsapp_message(
                sender,
                f"⏳ Waiting for your payment screenshot for *{ref}*.\n"
                f"Send the screenshot after paying ₹{s.advance_amount} to `{s.upi_id}`.",
            )
        return

    # ── No active flow — handle by intent ────────────────────────────────────
    if not parsed:
        # Groq fallback called from here
        from ..groq_fallback import groq_fallback
        reply = await groq_fallback(text, sender, "customer")
        send_whatsapp_message(sender, reply)
        return

    intent = parsed["intent"]

    if intent == "greeting":
        send_whatsapp_message(
            sender,
            f"👋 Welcome to *{s.turf_name}*!\n\n"
            f"📍 {s.turf_location}\n"
            f"🕐 Open: {s.turf_open_hour}:00 AM – {s.turf_close_hour}:00\n"
            f"💰 ₹{s.turf_price_per_slot}/hr\n\n"
            f"Type *slots* to check availability\n"
            f"Type *book* to make a booking",
        )

    elif intent == "check_availability":
        d = parsed.get("date") or date.today().strftime("%Y-%m-%d")
        free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
        msg = f"📅 *{_fmt_date(d)}*\n\n" + _slots_msg(free)
        send_whatsapp_message(sender, msg)

    elif intent == "book_slot":
        d = parsed.get("date")
        t = parsed.get("time")
        dur = parsed.get("duration", 1.0)

        if d and t:
            # Check availability first
            if not db.check_slot_available(d, t, dur):
                free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
                msg = (
                    f"❌ *{_fmt_time(t)}* on *{_fmt_date(d)}* is already booked.\n\n"
                    + _slots_msg(free[:5])
                )
                send_whatsapp_message(sender, msg)
                return
            # Have everything except name
            sess.set_state(sender, "awaiting_name", {"date": d, "time": t, "duration": dur})
            send_whatsapp_message(
                sender,
                f"📋 Booking *{_fmt_date(d)}* at *{_fmt_time(t)}* for *{int(dur)} hr(s)*.\n"
                f"What's your name?",
            )
        elif d:
            sess.set_state(sender, "awaiting_time", {"date": d})
            send_whatsapp_message(sender, f"📅 *{_fmt_date(d)}* — what time? (e.g. *8 PM*)")
        else:
            sess.set_state(sender, "awaiting_date")
            send_whatsapp_message(sender, "📅 Which date would you like to book?\n(e.g. *today*, *tomorrow*, *20 June*)")

    elif intent == "cancel_booking":
        ref = parsed.get("booking_ref")
        if ref:
            row = db.cancel_booking(ref)
            if row and row["phone"] == sender:
                send_whatsapp_message(sender, f"✅ Booking *{ref}* has been cancelled.")
            elif row:
                send_whatsapp_message(sender, f"❌ *{ref}* doesn't belong to your number.")
                # restore
                with db.get_conn() as conn:
                    conn.execute("UPDATE bookings SET status='pending_payment' WHERE booking_ref=?", (ref,))
            else:
                send_whatsapp_message(sender, f"❌ Booking *{ref}* not found.")
        else:
            rows = db.get_bookings_by_phone(sender)
            if rows:
                latest = rows[0]
                send_whatsapp_message(
                    sender,
                    f"Your latest booking is *{latest['booking_ref']}* "
                    f"({_fmt_date(latest['date'])} {_fmt_time(latest['start_time'])}).\n"
                    f"Type *cancel {latest['booking_ref']}* to cancel it.",
                )
            else:
                send_whatsapp_message(sender, "You don't have any active bookings.")

    elif intent == "booking_status":
        ref = parsed.get("booking_ref")
        rows = db.get_bookings_by_phone(sender) if not ref else [db.get_booking_by_ref(ref)]
        rows = [r for r in rows if r]
        if not rows:
            send_whatsapp_message(sender, "You don't have any active bookings. Type *book* to create one.")
        else:
            lines = ["📋 *Your Bookings:*\n"]
            for r in rows[:3]:
                status_icon = {"confirmed": "✅", "pending_payment": "⏳", "cancelled": "❌"}.get(r["status"], "❓")
                lines.append(
                    f"{status_icon} *{r['booking_ref']}*  {_fmt_date(r['date'])}  {_fmt_time(r['start_time'])}"
                    f"  ({r['status'].replace('_', ' ')})"
                )
            send_whatsapp_message(sender, "\n".join(lines))

    elif intent == "payment_done":
        if state == "awaiting_payment":
            send_whatsapp_message(
                sender,
                f"📸 Please send the *screenshot* of your payment.\nUPI ID: `{s.upi_id}`",
            )
        else:
            send_whatsapp_message(sender, "I don't see an active booking waiting for payment. Type *book* to start.")
