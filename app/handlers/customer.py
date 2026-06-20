"""
Customer message handler — AI-first booking flow.

Router priority:
  - Session state machine
  - Parser (rule-based, zero cost)
  - Groq AI (fallback)
"""
import logging
import re
from datetime import date, timedelta

from ..config import get_settings
from ..twilio_client import send_whatsapp_message, safe_notify_owner
from .. import db, session as sess
from ..parser import extract_date, extract_time, extract_duration

logger = logging.getLogger(__name__)

# Change words pattern
CHANGE_WORDS = ["no", "change", "instead", "cancel", "wrong", "different", "actually", "wait", "reset", "start over"]


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
    Resolve intent using rule-based parser first, falling back to Groq AI.
    """
    from ..parser import parse_message
    
    # 1. Fast-path parser first (zero API cost, instant)
    parsed = parse_message(text, "customer")
    if parsed:
        logger.info("Parser resolved intent for %s: %r", sender[-4:], parsed)
        return parsed

    # 2. Groq AI Fallback
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

    if s.groq_api_key:
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
                res = json.loads(m.group())
                if res.get("intent") != "unknown":
                    return res
        except Exception as e:
            logger.error("Groq intent error: %s", e)

    # 3. Local entity extraction fallback (if Groq fails, is disabled, or returns unknown)
    d = extract_date(text)
    t = extract_time(text)
    dur = extract_duration(text)
    intent = "book_slot" if (d or t) else "unknown"
    return {
        "intent": intent,
        "date": d,
        "time": t,
        "duration": dur
    }


# ── Failure & Correction Helpers ───────────────────────────────────────────────

def log_failure(message: str, state: str, bot_reply: str, intent: str, failure_type: str) -> None:
    """Log customer failure/fallback scenarios to the database for analysis."""
    try:
        with db.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO conversation_failures (message, state, bot_reply, intent, failure_type, resolved)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (message, state, bot_reply, intent, failure_type)
            )
    except Exception as e:
        logger.error("Error logging conversation failure: %s", e)


def is_change_request(text: str) -> bool:
    """Return True if the text suggests the customer wants to cancel or modify a booking flow."""
    t = text.lower().strip()
    if any(re.search(r'\b' + re.escape(kw) + r'\b', t) for kw in CHANGE_WORDS):
        return True
    if extract_date(text) or extract_time(text):
        return True
    return False


async def ask_next_missing_field(sender: str) -> None:
    """Scan session data and ask for the first missing booking field."""
    data = sess.get_data(sender)
    s = get_settings()

    # Early check of availability if date and time are both present
    if data.get("date") and data.get("time"):
        dur = float(data.get("duration") or 1.0)
        if not db.check_slot_available(data["date"], data["time"], dur):
            free = db.get_free_slots(data["date"], s.turf_open_hour, s.turf_close_hour)
            reply = (
                f"❌ *{_fmt_time(data['time'])}* on *{_fmt_date(data['date'])}* is already booked.\n\n"
                + _slots_msg(free[:6])
            )
            send_whatsapp_message(sender, reply)
            log_failure(
                message=f"book {data['date']} {data['time']} {dur}",
                state=sess.get_state(sender),
                bot_reply=reply,
                intent="book_slot",
                failure_type="slot_locked"
            )
            sess.clear_session(sender)
            return

    # 1. Date
    if not data.get("date"):
        sess.set_state(sender, "awaiting_date")
        send_whatsapp_message(sender, "📅 Which date? (e.g. *today*, *tomorrow*, *20 June*)")
        return

    # 2. Time
    if not data.get("time"):
        sess.set_state(sender, "awaiting_time")
        send_whatsapp_message(sender, f"📅 *{_fmt_date(data['date'])}* — what time? (e.g. *8 PM*, *evening*)")
        return

    # 3. Duration
    if "duration" not in data or data.get("duration") is None:
        sess.set_state(sender, "awaiting_duration")
        send_whatsapp_message(sender, f"⏰ *{_fmt_time(data['time'])}*. How many hours? (1, 2, or 3)")
        return

    # 4. Name
    if not data.get("name"):
        sess.set_state(sender, "awaiting_name")
        send_whatsapp_message(sender, f"⏱ *{int(data['duration'])} hr(s)*. What's your name?")
        return

    # 5. Complete booking
    d = data["date"]
    t = data["time"]
    dur = float(data["duration"])
    name = data["name"]

    if not db.check_slot_available(d, t, dur):
        free = db.get_free_slots(d, s.turf_open_hour, s.turf_close_hour)
        reply = (
            f"❌ *{_fmt_time(t)}* on *{_fmt_date(d)}* was just taken!\n\n"
            + _slots_msg(free[:6]) + "\n\nType *book* to pick another slot."
        )
        send_whatsapp_message(sender, reply)
        log_failure(
            message=f"book {d} {t} {dur}",
            state=sess.get_state(sender),
            bot_reply=reply,
            intent="book_slot",
            failure_type="slot_locked"
        )
        sess.clear_session(sender)
        return

    ref = db.create_booking(sender, name, d, t, dur)
    if not ref:
        reply = "❌ Slot no longer available. Type *book* to try again."
        send_whatsapp_message(sender, reply)
        log_failure(
            message=f"book {d} {t} {dur}",
            state=sess.get_state(sender),
            bot_reply=reply,
            intent="book_slot",
            failure_type="slot_locked"
        )
        sess.clear_session(sender)
        return

    sess.set_state(sender, "awaiting_payment", {"booking_ref": ref})
    send_whatsapp_message(sender, _booking_confirmation_msg(ref, d, t, dur, name, s))


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

        # Transition to pending_owner — owner must now confirm within 24 hours
        updated = db.mark_screenshot_received(ref, media_url, utr)
        if not updated:
            # Fallback: update fields directly if status already moved
            with db.get_conn() as conn:
                conn.execute(
                    "UPDATE bookings SET screenshot_url=?, payment_utr=? WHERE booking_ref=?",
                    (media_url, utr, ref),
                )

        bk = db.get_booking_by_ref(ref)
        if bk:
            # Audit log
            db.log_audit(
                actor=sender,
                action="screenshot_received",
                entity_type="payment",
                entity_id=ref,
                details=f"UTR={utr or 'none'} screenshot={media_url[:60]}"
            )
            db.log_event(sender, "screenshot_received", {
                "booking_ref": ref,
                "utr": utr,
                "screenshot_url": media_url
            })
            # Notify owners — use safe wrapper (Twilio failure won't crash handler)
            safe_notify_owner(
                s.owner_list,
                _owner_notify_msg(ref, sender, bk["name"], bk["date"],
                                  bk["start_time"], bk["duration_hrs"], media_url),
            )

        send_whatsapp_message(
            sender,
            f"📸 Got it! Screenshot received for *{ref}*.\n"
            f"Owner will confirm your payment within a few hours. You'll get a message. 🙏",
        )
        sess.set_state(sender, "done")
        return

    # ── Mid-conversation corrections (not in Idle / Done / ConfirmChange) ─────
    if state not in ("idle", "done", "confirm_change") and is_change_request(text):
        if state == "awaiting_payment":
            ref = data.get("booking_ref", "")
            parsed = await _resolve_intent(text, sender)
            sess.update_data(sender,
                proposed_date=parsed.get("date"),
                proposed_time=parsed.get("time"),
                proposed_duration=parsed.get("duration")
            )
            sess.set_state(sender, "confirm_change")
            send_whatsapp_message(sender,
                f"You have a pending booking *{ref}*.\n"
                f"Do you want to update it to the new slot?\n\n"
                f"1️⃣ Update booking\n"
                f"2️⃣ Keep existing booking"
            )
            return
        else:
            parsed = await _resolve_intent(text, sender)
            updates = {}
            if parsed.get("date"): updates["date"] = parsed.get("date")
            if parsed.get("time"): updates["time"] = parsed.get("time")
            if parsed.get("duration"): updates["duration"] = parsed.get("duration")
            
            if updates:
                sess.update_data(sender, **updates)
                send_whatsapp_message(sender, "🔄 Got it, updating your booking details...")
                await ask_next_missing_field(sender)
            else:
                if any(k in text.lower() for k in ["cancel", "reset", "start over"]):
                    send_whatsapp_message(sender, "❌ Booking cancelled.")
                    sess.clear_session(sender)
                else:
                    send_whatsapp_message(sender, "Type *slots* to check free times or *book* to start again.")
                    sess.clear_session(sender)
            return

    # ── confirm_change state ───────────────────────────────────────────────────
    if state == "confirm_change":
        ref = data.get("booking_ref", "")
        clean_txt = text.strip().lower()
        if clean_txt in ("1", "update", "yes", "update booking"):
            db.cancel_booking(ref)
            
            d = data.get("proposed_date") or data.get("date")
            t = data.get("proposed_time") or data.get("time")
            dur = data.get("proposed_duration") or data.get("duration")
            
            sess.clear_session(sender)
            sess.update_data(sender, date=d, time=t, duration=dur)
            
            send_whatsapp_message(sender, "🔄 Booking updated. Proceeding with new details...")
            await ask_next_missing_field(sender)
            
        elif clean_txt in ("2", "keep", "no", "keep existing booking"):
            sess.update_data(sender, proposed_date=None, proposed_time=None, proposed_duration=None)
            sess.set_state(sender, "awaiting_payment")
            
            bk = db.get_booking_by_ref(ref)
            if bk:
                send_whatsapp_message(sender, f"ℹ️ Kept existing booking *{ref}*.")
                send_whatsapp_message(sender, _booking_confirmation_msg(ref, bk["date"], bk["start_time"], bk["duration_hrs"], bk["name"], s))
            else:
                send_whatsapp_message(sender, "Couldn't find the booking. Please start again.")
                sess.clear_session(sender)
        else:
            send_whatsapp_message(sender, 
                "Please reply with:\n"
                "1️⃣ *Update booking* to change to the new slot\n"
                "2️⃣ *Keep existing booking* to continue with your current booking"
            )
        return

    # ── State Machine booking flow ─────────────────────────────────────────────
    if state == "awaiting_date":
        d = extract_date(text)
        if not d:
            parsed = await _resolve_intent(text, sender)
            d = parsed.get("date")
        if d:
            sess.update_data(sender, date=d)
            await ask_next_missing_field(sender)
        else:
            reply = "📅 Which date? (e.g. *today*, *tomorrow*, *20 June*)"
            send_whatsapp_message(sender, reply)
            log_failure(text, state, reply, "book_slot", "parser_failed")
        return

    if state == "awaiting_time":
        t = extract_time(text)
        dur = extract_duration(text)
        if not t:
            parsed = await _resolve_intent(text, sender)
            t = parsed.get("time")
            dur = parsed.get("duration") or dur
        
        updates = {}
        if t: updates["time"] = t
        if dur: updates["duration"] = dur
            
        if updates:
            sess.update_data(sender, **updates)
            await ask_next_missing_field(sender)
        else:
            reply = "⏰ What time? (e.g. *8 PM*, *6 to 9 PM*, *evening*)"
            send_whatsapp_message(sender, reply)
            log_failure(text, state, reply, "book_slot", "parser_failed")
        return

    if state == "awaiting_duration":
        dur = extract_duration(text)
        if dur is None:
            m = re.match(r"^\s*([1-4])\s*$", text.strip())
            if m:
                dur = float(m.group(1))
        
        if dur:
            sess.update_data(sender, duration=dur)
            await ask_next_missing_field(sender)
        else:
            reply = "⏱ How many hours? Please reply with a number (1, 2, or 3)."
            send_whatsapp_message(sender, reply)
            log_failure(text, state, reply, "book_slot", "parser_failed")
        return

    if state == "awaiting_name":
        name = text.strip().title()
        if len(name) < 2 or bool(re.match(r"^[\d\s\-]+$", name)) or any(
            kw in text.lower() for kw in ["hour", " pm", " am", "book", "slot", "duration", "hrs"]
        ):
            reply = "What's your *name*? (e.g. *Ravi*, *Team Kings*)"
            send_whatsapp_message(sender, reply)
            log_failure(text, state, reply, "book_slot", "parser_failed")
            return
            
        sess.update_data(sender, name=name)
        await ask_next_missing_field(sender)
        return

    if state == "awaiting_payment":
        ref = data.get("booking_ref", "")
        if any(kw in text.lower() for kw in ["cost", "price", "total", "how much", "kitna"]):
            bk = db.get_booking_by_ref(ref)
            if bk:
                total = int(bk["duration_hrs"]) * s.turf_price_per_slot
                send_whatsapp_message(sender,
                    f"💰 Total: ₹{total} ({int(bk['duration_hrs'])} hr × ₹{s.turf_price_per_slot})\n"
                    f"Pay advance ₹{s.advance_amount} to `{s.upi_id}` and send screenshot.")
        else:
            reply = (
                f"⏳ Waiting for screenshot for *{ref}*.\n"
                f"Pay ₹{s.advance_amount} to `{s.upi_id}` and send the payment screenshot here.\n\n"
                f"Want to change the slot? Just tell me the new time."
            )
            send_whatsapp_message(sender, reply)
            log_failure(text, state, reply, "payment_done", "state_conflict")
        return

    # ── Idle state — resolve intent ────────────────────────────────────────────
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
        db.log_event(sender, "booking_started", {"text": text})
        d = parsed.get("date")
        t = parsed.get("time")
        dur = parsed.get("duration")
        
        updates = {}
        if d: updates["date"] = d
        if t: updates["time"] = t
        if dur: updates["duration"] = dur
        
        sess.set_state(sender, "awaiting_date", updates)
        await ask_next_missing_field(sender)

    elif intent == "cancel_booking":
        ref = parsed.get("booking_ref")
        if ref:
            row = db.cancel_booking(ref)
            if row and row["phone"] == sender:
                send_whatsapp_message(sender, f"✅ *{ref}* cancelled.")
            elif row:
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
        answer = parsed.get("answer", "")
        if answer:
            send_whatsapp_message(sender, answer)
        else:
            send_whatsapp_message(sender,
                f"📍 *{s.turf_name}*\n{s.turf_location}\n"
                f"🕐 {s.turf_open_hour}AM–{s.turf_close_hour}:00  💰 ₹{s.turf_price_per_slot}/hr")

    else:
        reply = (
            f"I can help you with:\n"
            f"• *slots* — check availability\n"
            f"• *book* — make a booking\n"
            f"• *my booking* — check status\n"
            f"• *cancel BKxxx* — cancel a booking"
        )
        send_whatsapp_message(sender, reply)
        log_failure(text, state, reply, "unknown", "parser_failed")
