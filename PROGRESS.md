# Turf Booking WhatsApp Agent — Progress Notes

## Status: Working MVP ✅
Last updated: June 17, 2026

---

## What's Built

- FastAPI + Twilio WhatsApp webhook
- SQLite DB (bookings, blocked_slots) with BK101+ IDs
- AI-first customer handler (Groq llama-3.1-8b-instant)
- Rule-based owner command parser
- Multi-turn session state machine (10 min expiry)
- 30-min reminder scheduler (APScheduler)
- Input hardening + global error handler
- 105 tests passing

---

## What's Tested (WhatsApp live test)

- ✅ Greeting — "Hiii" → welcome message
- ✅ Natural booking — "6pm to 9pm tomorrow" → straight to name
- ✅ Booking created → UPI details sent
- ✅ Change booking mid-flow — "No I want 7pm today" → cancels + rebooks
- ✅ Screenshot received → owner notified
- ⬜ Owner commands (view, block, confirm) — not tested yet
- ⬜ Render deployment — not done yet

---

## What's Pending

1. Test owner commands from owner WhatsApp number
   - `today's bookings`
   - `block tomorrow 8 PM`
   - `confirm BK101`
   - `show BK101`

2. Fix .env before real use:
   - Set real UPI ID (currently `yourname@upi`)
   - Add owner number back: `OWNER_NUMBERS=whatsapp:+917815809989`

3. Deploy to Render (free tier)
   - Connect GitHub repo
   - Add /data disk for SQLite
   - Set all env vars in Render dashboard
   - Point Twilio webhook to Render URL

4. Sell to first turf owner 🏏

---

## Credentials to Rotate (DO THIS NOW)

- GitHub token — was shared in chat, regenerate at github.com/settings/tokens
- Twilio Auth Token — rotate at console.twilio.com
- Groq API key — delete and recreate at console.groq.com
- ngrok auth token — rotate at dashboard.ngrok.com

---

## Local Dev Setup (when coming back)

```
cd C:\whtagentforturf\turf-agent

# Terminal 1 — start bot
uvicorn app.main:app --reload --port 8000

# Terminal 2 — expose to Twilio
ngrok http 8000
# copy https URL → paste into Twilio sandbox webhook + /webhook
```

---

## Repo
https://github.com/bharathkumar7733/whatsapp_bot_turfbooking

## Cost
~₹300/month running cost. Charge turf owner ₹2,000–5,000/month.
