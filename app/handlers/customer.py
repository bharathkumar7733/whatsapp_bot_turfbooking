"""
Customer message handler — AI-first booking flow.

Groq understands the message first.
Parser is a fast-path for exact keywords only.
Multi-turn session collects missing info step by step.
"""
import logging
import re
from datetime import date, timedelta

from ..config import get_settings
from ..twilio_client import send_whatsapp_message
from .. import db, session as sess
from ..parser import extract_date, extract_time, extract_duration

logger = logging.getLogger(__name__)


# ── Formatting helpers ─────────────────────────────────────────────────────────

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
    return dt.strftime("%a, %d %b")


def _slots_msg(free: list) -> str:
    if not free:
        return "❌ No free slots available for that day."
    lines = ["🟢 *Available Slots:*"]
    for t in free:
        lines.append(f"  • {_fmt_time(t)}")
    return "\n".join(lines)


def _booking_confirmation_msg(ref, d, t, dur, name, s) -> str:
    end_h = int(t.split(":")[0]) + int(dur)
    end_t = f"{end_h:02d}:00"
    total = int(dur) * s.turf_price_per_slot
    return (
        f"✅ *Booking Created!*\n\n"
        f"📋 Ref: *{ref}*\n"
        f"👤 {name}\n"
        f"📅 {_fmt_date(d)}\n"
        f"⏰ {_fmt_time(t)} – {_fmt_time(end_t)}\n"
        f"⏱ {int(dur)} hr(s)  |  💰 Total: ₹{total}\n\n"
        f"💸 *Pay Advance: ₹{s.advance_amount}*\n"
        f"UPI ID: `{s.upi_id}`\n\n"
        f"After payment, *send the screenshot here*.\n"
        f"Slot holds for *5 minutes* ⏳"
    )


def _owner_notify_msg(ref, phone, name, d, t, dur, screenshot_url) -> str:
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


# ── AI intent resolver ─────────────────────────────────────────────────────────

async def _resolve_intent(text: str, sender: str) -> dict:
    """
    Use Groq to understand the message.
    Returns dict with intent + entities, or {"intent": "unknown"}.
    """
    from ..groq_fallback import groq_fallback as _gf
    from ..config import get_settings as _gs
    import json, re as _re

    s = _gs()
    today = date.today()
    tomorrow = today + timedelta(days=1)

    system = f"""You are a booking assistant for {s.turf_name}, a sports turf in {s.turf_location}.
Today is {today.strftime('%A, %d %B %Y')}. Tomorrow is {tomorrow.strftime('%d %B %Y')}.
Open: {s.turf_open_hour}AM to {s.turf_close_hour}:00. Price: ₹{s.turf_price_per_slot}/hr. Advance: ₹{s.advance_amount}.

Classify the user message into ONE of these intents and return ONLY valid JSON (no markdown):

1. {{"intent":"greeting"}}
2. {{"intent":"check_availability","date":"YYYY-MM-DD"}}
   - "slots today" → today's date
   - "tomorrow" alone → tomorrow's date  
   - "slots" alone → today's date
3. {{"intent":"book_slot","date":"YYYY-MM-DD","time":"HH:MM","duration":1.0}}
   - "6pm to 9pm tomorrow" → date=tomorrow, time=18:00, duration=3
   - "book 8pm" → time=20:00, date=null if not mentioned
   - Extract duration from time range (e.g. 6-9pm = 3 hours)
4. {{"intent":"cancel_booking","booking_ref":"BK101"}}
5. {{"intent":"booking_status"}}
6. {{"intent":"payment_done"}}
7. {{"intent":"faq","answer":"your answer here"}}
   - For questions about price, location, rules, parking, rain etc.
   - Answer in 1-2 lines using turf info above
8. {{"intent":"unknown"}}

Rules:
- "tomorrow" alone when user was asking about slots → check_availability for tomorrow
- Always extract duration from time ranges like "6 to 9" = 3 hours
- Respond in same language as user (English/Hindi/Tamil)
- Return ONLY the JSON, nothing else"""

    try:
        from groq import Groq
        client = Groq(api_key=s.groq_api_key)
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.1,
            max_tokens=150,
        )
        raw = response.choices[0].message.content.strip()
        logger.info("Groq intent for %s: %r", sender[-4:], raw)

        # Extract JSON
        m = _re.search(r"\{.*?\}", raw, _re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        logger.error("Groq intent error: %s", e)

    # Fast-path parser fallback
    from ..parser import parse_message
    parsed = parse_message(text, "customer")
    if parsed:
        return parsed

    return {"intent": "unknown"}


# ── Main handler ───────────────────────────────────────────────────────────────

async def handle_customer(sender: str, text: str, media_url: str = "") -> None:
    s = get_settings()
    state = sess.get_state(sender)
    data = sess.get_data(sender)

    # ── Screenshot received ────────────────────────────────────────────────────
    if media_url and state == "awaiting_payment":
        ref = data.get("booking_ref")
        if not ref:
            send_whatsapp_message(sender, "⚠️ Couldn't find your booking. Type *book* to start again.")
            sess.clear_session(sender)
            return

        utr = ""
        utr_match = re.search(r"\b(\d{10,})\b", text or "")
        if utr_match:
            utr = utr_match.group(1)

        with db.get_conn() as conn:
            conn.execute(
                "UPDATE bookings SET screenshot_url=?, payment_utr=? WHERE booking_ref=?",
                (media_url, utr, ref),
            )

        bk = db.get_booking_by_ref(ref)
        if bk:
            for owner_phone in s.owner_list:
                send_whatsapp_message(
                    owner_phone,
                    _owner_notify_msg(ref, sender, bk["name"], bk["date"],
                                      bk["start_time"], bk["duration_hrs"], media_url),
                )

        send_whatsapp_message(
            sender,
            f"📸 Got it! Screenshot received for *{ref}*.\n"
            f"Owner will confirm shortly. You'll get a message. 🙏",
        )
        sess.set_state(sender, "done")
        return

    # ── In-flow state machine (collecting missing booking info) ───────────────
    if state == "awaiting_date":
        d = extract_date(text)
        if not d:
            # Ask Groq to extract date from natural text
            parsed = await _resolve_intent(text, sender)
            d = parsed.get("date")
        if d:
            sess.set_state(sender, "awaiting_time", {"date": d})
            send_whatsapp_message(sender, f"📅 *{_fmt_date(d)}* — what time? (e.g. *8 PM*, *evening*)")
        else:
            send_whatsapp_message(sender, "📅 Which date? (e.g. *today*, *tomorrow*, *20 June*)")
        return

    if state == "awaiting_time":
        t = extract_time(text)
        dur = extract_duration(text)
        if not t:
            parsed = await _resolve_intent(text, sender)
            t = parsed.get("time")
            dur = parsed.get("duration", dur)
        if t:
            if dur and dur > 1.0:
                # Got both time and duration — skip to name
                sess.set_state(sender, "awaiting_name", {"time": t, "duration": dur})
                send_whatsapp_message(sender,
                    f"⏰ *{_fmt_time(t)}* for *{int(dur)} hrs*. What's your name?")
            else:
                sess.set_state(sender, "awaiting_duration", {"time": t})
                send_whatsapp_message(sender,
                    f"⏰ *{_fmt_time(t)}*. How many hours? (1, 2, or 3)")
        else:
            send_whatsapp_message(sender, "⏰ What time? (e.g. *8 PM*, *6 to 9 PM*, *evening*)")
        return

    if state == "awaiting_duration":
        dur = extract_duration(text)
        if dur == 1.0:
            m = re.match(r"^\s*([1-4])\s*$", text.strip())
            if m:
                dur = float(m.group(1))
        sess.set_state(sender, "awaiting_name", {"duration": dur})
        send_whatsapp_message(sender, f"⏱ *{int(dur)} hr(s)*. What's your name?")
        return

    if state == "awaiting_name":
        name = text.strip().title()
        # Reject non-names
        if len(name) < 2 or bool(re.match(r"^[\d\s\-]+$", name)) or any(
            kw in text.lower() for kw in ["hour", " pm", " am", "book", "slot", "duration", "hrs"]
        ):
            send_whatsapp_message(sender,
                "What's your *name*? (e.g. *Ravi*, *Team Kings*)")
            return

        d = data.get("date")
        t = data.get("time")
        dur = data.get("duration", 1.0)

        if not db.check_slot_available(d, t, dur):
            free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
            send_whatsapp_message(sender,
                f"❌ *{_fmt_time(t)}* on *{_fmt_date(d)}* was just taken!\n\n"
                + _slots_msg(free[:6]) + "\n\nType *book* to pick another slot.")
            sess.clear_session(sender)
            return

        ref = db.create_booking(sender, name, d, t, dur)
        if not ref:
            send_whatsapp_message(sender, "❌ Slot no longer available. Type *book* to try again.")
            sess.clear_session(sender)
            return

        sess.set_state(sender, "awaiting_payment", {"booking_ref": ref, "name": name})
        send_whatsapp_message(sender, _booking_confirmation_msg(ref, d, t, dur, name, s))
        return

    if state == "awaiting_payment":
        ref = data.get("booking_ref", "")
        # Customer wants to change their booking
        if any(kw in text.lower() for kw in ["no", "change", "different", "wrong", "cancel", "instead", "actually", "wait"]) \
                or extract_time(text) or extract_date(text):
            # Cancel the pending booking and start fresh
            db.cancel_booking(ref)
            sess.clear_session(sender)
            # Now resolve as a fresh intent
            parsed = await _resolve_intent(text, sender)
            intent = parsed.get("intent", "unknown")
            if intent == "book_slot":
                d = parsed.get("date")
                t = parsed.get("time")
                dur = float(parsed.get("duration") or 1.0)
                if d and t:
                    if not db.check_slot_available(d, t, dur):
                        free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
                        send_whatsapp_message(sender,
                            f"❌ *{_fmt_time(t)}* on *{_fmt_date(d)}* is already booked.\n\n"
                            + _slots_msg(free[:6]))
                        return
                    sess.set_state(sender, "awaiting_name", {"date": d, "time": t, "duration": dur})
                    end_h = int(t.split(":")[0]) + int(dur)
                    send_whatsapp_message(sender,
                        f"✅ Changed! New booking:\n"
                        f"📋 *{_fmt_date(d)}*  {_fmt_time(t)} – {_fmt_time(f'{end_h:02d}:00')}  ({int(dur)} hr)\n"
                        f"What's your name?")
                elif d:
                    sess.set_state(sender, "awaiting_time", {"date": d})
                    send_whatsapp_message(sender, f"📅 *{_fmt_date(d)}* — what time?")
                else:
                    sess.set_state(sender, "awaiting_date")
                    send_whatsapp_message(sender, "📅 Which date would you like?")
            else:
                send_whatsapp_message(sender,
                    f"Previous booking *{ref}* cancelled.\nType *book* to start a new booking.")
            return
        elif any(kw in text.lower() for kw in ["cost", "price", "total", "how much", "kitna"]):
            bk = db.get_booking_by_ref(ref)
            if bk:
                total = int(bk["duration_hrs"]) * s.turf_price_per_slot
                send_whatsapp_message(sender,
                    f"💰 Total: ₹{total} ({int(bk['duration_hrs'])} hr × ₹{s.turf_price_per_slot})\n"
                    f"Pay advance ₹{s.advance_amount} to `{s.upi_id}` and send screenshot.")
        else:
            send_whatsapp_message(sender,
                f"⏳ Waiting for screenshot for *{ref}*.\n"
                f"Pay ₹{s.advance_amount} to `{s.upi_id}` and send the payment screenshot here.\n\n"
                f"Want to change the slot? Just tell me the new time.")
        return

    # ── Idle state — resolve intent with AI ───────────────────────────────────
    parsed = await _resolve_intent(text, sender)
    intent = parsed.get("intent", "unknown")

    if intent == "greeting":
        send_whatsapp_message(sender,
            f"👋 Welcome to *{s.turf_name}*!\n\n"
            f"📍 {s.turf_location}\n"
            f"🕐 Open: {s.turf_open_hour}:00 AM – {s.turf_close_hour}:00\n"
            f"💰 ₹{s.turf_price_per_slot}/hr\n\n"
            f"• Type *slots* — check availability\n"
            f"• Type *book* — make a booking\n"
            f"• Type *my booking* — check your booking")

    elif intent == "check_availability":
        d = parsed.get("date") or date.today().strftime("%Y-%m-%d")
        free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
        send_whatsapp_message(sender, f"📅 *{_fmt_date(d)}*\n\n" + _slots_msg(free))

    elif intent == "book_slot":
        d = parsed.get("date")
        t = parsed.get("time")
        dur = float(parsed.get("duration") or 1.0)

        if d and t:
            if not db.check_slot_available(d, t, dur):
                free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
                send_whatsapp_message(sender,
                    f"❌ *{_fmt_time(t)}* on *{_fmt_date(d)}* is already booked.\n\n"
                    + _slots_msg(free[:6]))
                return
            sess.set_state(sender, "awaiting_name", {"date": d, "time": t, "duration": dur})
            end_h = int(t.split(":")[0]) + int(dur)
            send_whatsapp_message(sender,
                f"📋 *{_fmt_date(d)}*  {_fmt_time(t)} – {_fmt_time(f'{end_h:02d}:00')}  ({int(dur)} hr)\n"
                f"What's your name?")
        elif d:
            sess.set_state(sender, "awaiting_time", {"date": d})
            send_whatsapp_message(sender, f"📅 *{_fmt_date(d)}* — what time?")
        else:
            sess.set_state(sender, "awaiting_date")
            send_whatsapp_message(sender,
                "📅 Which date? (e.g. *today*, *tomorrow*, *20 June*)")

    elif intent == "cancel_booking":
        ref = parsed.get("booking_ref")
        if ref:
            row = db.cancel_booking(ref)
            if row and row["phone"] == sender:
                send_whatsapp_message(sender, f"✅ *{ref}* cancelled.")
            elif row:
                # restore — not their booking
                with db.get_conn() as conn:
                    conn.execute(
                        "UPDATE bookings SET status='pending_payment' WHERE booking_ref=?", (ref,))
                send_whatsapp_message(sender, f"❌ *{ref}* doesn't belong to your number.")
            else:
                send_whatsapp_message(sender, f"❌ *{ref}* not found.")
        else:
            rows = db.get_bookings_by_phone(sender)
            if rows:
                r = rows[0]
                send_whatsapp_message(sender,
                    f"Your active booking: *{r['booking_ref']}* "
                    f"{_fmt_date(r['date'])} {_fmt_time(r['start_time'])}.\n"
                    f"Type *cancel {r['booking_ref']}* to cancel.")
            else:
                send_whatsapp_message(sender, "You have no active bookings.")

    elif intent == "booking_status":
        rows = db.get_bookings_by_phone(sender)
        rows = [r for r in rows if r]
        if not rows:
            send_whatsapp_message(sender,
                "No active bookings found. Type *book* to make one.")
        else:
            icons = {"confirmed": "✅", "pending_payment": "⏳", "cancelled": "❌"}
            lines = ["📋 *Your Bookings:*\n"]
            for r in rows[:3]:
                lines.append(
                    f"{icons.get(r['status'], '❓')} *{r['booking_ref']}*  "
                    f"{_fmt_date(r['date'])}  {_fmt_time(r['start_time'])}  "
                    f"({r['status'].replace('_', ' ')})")
            send_whatsapp_message(sender, "\n".join(lines))

    elif intent == "payment_done":
        send_whatsapp_message(sender,
            f"📸 Please *send the screenshot* of your payment.\nUPI ID: `{s.upi_id}`")

    elif intent == "faq":
        # Groq already answered in the JSON
        answer = parsed.get("answer", "")
        if answer:
            send_whatsapp_message(sender, answer)
        else:
            send_whatsapp_message(sender,
                f"📍 *{s.turf_name}*\n{s.turf_location}\n"
                f"🕐 {s.turf_open_hour}AM–{s.turf_close_hour}:00  💰 ₹{s.turf_price_per_slot}/hr")

    else:
        send_whatsapp_message(sender,
            f"I can help you with:\n"
            f"• *slots* — check availability\n"
            f"• *book* — make a booking\n"
            f"• *my booking* — check status\n"
            f"• *cancel BKxxx* — cancel a booking")
