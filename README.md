# WhatsApp Turf Booking Agent 🏏

A WhatsApp AI agent that handles turf bookings end-to-end. No website, no app, no dashboard — everything happens inside WhatsApp.

Built for turf owners who currently manage bookings manually through calls and WhatsApp groups.

**Version 2.0 — Pilot Ready | 135 Tests Passing**

---

## How It Works

**Customer side:**
```
Customer: 6pm to 9pm tomorrow
Bot: 📋 Tomorrow 6:00 PM – 9:00 PM (3 hr). What's your name?

Customer: Ravi Kumar
Bot: ✅ Booking Created! BK101
     Pay ₹300 advance to owner@upi
     Send screenshot after payment. Slot hold s 5 min ⏳  ,.

Customer: [sends UPI screenshot]
Bot: 📸 Got it! Owner will confirm within a few hours 🙏

Owner: confirm BK101
Bot → Customer: 🎉 Booking Confirmed! See you on the turf! 🏏
```

**Owner standard commands:**
```
today's bookings        → full list (✅ confirmed, 📸 awaiting, ⏳ unpaid)
block tomorrow 8 PM     → slot blocked
confirm BK101           → payment confirmed, customer notified
cancel BK101            → booking cancelled, customer notified
show BK101              → full booking details
```

**Owner admin commands:**
```
/admin force cancel BK101             → immediate cancel + customer notified
/admin unlock slot 2026-06-21 20:00   → remove block + cancel pending locks
/admin reset session +919876543210    → clear stuck customer session
/admin show active users              → all active sessions with state
/admin resend payment BK101           → re-send UPI prompt to customer
```

---

## Tech Stack

| Layer | Tech |
|---|---|
| Messaging | Twilio WhatsApp API |
| Backend | FastAPI (Python) |
| AI | Groq (llama-3.1-8b-instant) |
| Database | SQLite (WAL mode, persistent) |
| Scheduler | APScheduler |
| Deploy | Render (free tier) |

---

## Features

### Core Booking
- Natural language booking — "6pm to 9pm tomorrow" works in one message
- Parser-first routing (zero API cost) with Groq AI fallback
- Double booking prevention with time-overlap detection
- Slot locking — 5-minute hold via `reserved_until` prevents race conditions
- Manual UPI payment — customer sends screenshot, owner confirms
- Change booking mid-flow — "No, I want 7pm instead" works non-destructively
- Booking correction with confirmation — "Update" or "Keep existing"

### Production Reliability
- **Twilio Signature Validation** — 403 on spoofed webhooks
- **Idempotency Protection** — `worker_id` lock prevents double-booking on Twilio retries
- **Owner Approval Queue** — `pending_owner` status with 24h SLA
- **Owner Escalation** — Reminders at +6h, +18h, auto-cancel at +24h
- **Safe Notifications** — `safe_notify_owner()` never crashes the app on Twilio failure
- **SQLite Session Persistence** — survives server restarts
- **Audit Trail** — every owner action logged with entity type and ID
- **Daily Metrics** — messages, bookings, conversion, owner response time
- **Cleanup Jobs** — `cleanup_fast()` on every webhook, `cleanup_full()` daily
- **Health Endpoints** — `/health` and `/ready` for Render monitoring
- Owner alerts on unhandled backend exceptions
- 135 tests passing

---

## Project Structure

```
turf-agent/
├── app/
│   ├── main.py              # FastAPI app, lifespan, error handler + owner alerts
│   ├── config.py            # Settings from .env (incl. signature validation)
│   ├── router.py            # POST /webhook — signature check + idempotency
│   │                        # GET /health + GET /ready
│   ├── db.py                # SQLite schema + all DB helpers
│   │                        # Tables: bookings, sessions, audit_logs,
│   │                        #   processed_messages, daily_metrics, events
│   ├── parser.py            # Regex parser (fast-path, zero API cost)
│   ├── session.py           # SQLite-backed multi-turn state machine
│   ├── groq_fallback.py     # Groq AI for natural language
│   ├── scheduler.py         # Reminders + expiry + owner escalation + daily cleanup
│   ├── twilio_client.py     # Twilio wrapper + safe_notify_owner()
│   └── handlers/
│       ├── customer.py      # Full customer booking flow
│       └── owner.py         # Owner commands + /admin suite
├── tests/
│   ├── test_db.py           # DB layer tests
│   ├── test_session.py      # Session store tests
│   ├── test_parser.py       # Parser tests
│   ├── test_customer_flow.py # Customer conversation flow tests
│   ├── test_owner_flow.py   # Owner command tests
│   ├── test_hardening.py    # Input hardening tests
│   ├── test_corrections.py  # Booking correction flow tests
│   └── test_production.py   # Production hardening tests (NEW)
├── WALKTHROUGH.md           # Complete flow + all commands explained
├── .env.example             # All config keys documented
├── render.yaml              # One-click Render deploy
├── Dockerfile               # Local Docker run
├── SETUP.md                 # Step-by-step setup guide
└── PROGRESS.md              # Dev progress notes
```

---

## Quick Start

**1. Clone and install**
```bash
git clone https://github.com/bharathkumar7733/whatsapp_bot_turfbooking
cd turf-agent
pip install -r requirements.txt
```

**2. Configure**
```bash
cp .env.example .env
# Fill in your Twilio, Groq, UPI, and turf details
```

**3. Run locally**
```bash
# Terminal 1
uvicorn app.main:app --reload --port 8000

# Terminal 2 — expose to Twilio
ngrok http 8000
```

**4. Connect Twilio**

Set your Twilio sandbox webhook to:
```
https://your-ngrok-url.ngrok-free.app/webhook
```

**5. Test**

Send `Hi` to your Twilio sandbox WhatsApp number.

---

## Environment Variables

```env
# Twilio
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886

# Groq (AI fallback)
GROQ_API_KEY=gsk_...

# Turf Config
TURF_NAME=Champions Turf
TURF_LOCATION=Anna Nagar, Chennai
TURF_OPEN_HOUR=6
TURF_CLOSE_HOUR=23
TURF_PRICE_PER_SLOT=600
ADVANCE_AMOUNT=300
UPI_ID=yourname@upi

# Owners (comma-separated, E.164 WhatsApp format)
OWNER_NUMBERS=whatsapp:+919876543210

# Database — set to /data/turf.db on Render
DB_PATH=turf.db
APP_ENV=production

# Security
VALIDATE_TWILIO_SIGNATURE=True    # set False for local dev
ALLOW_SIGNATURE_BYPASS=False      # NEVER True in production
```

---

## Deploy to Render

1. Push to GitHub
2. New Web Service → connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add Disk → mount path `/data` → 1 GB → set `DB_PATH=/data/turf.db`
6. Add all env vars in Render dashboard
7. Deploy → copy URL → update Twilio webhook URL + `/webhook`
8. Verify: `GET https://your-app.onrender.com/health`

Full guide in [SETUP.md](SETUP.md) · Full flow in [WALKTHROUGH.md](WALKTHROUGH.md)

---

## Booking Status Flow

```
pending_payment  →  pending_owner  →  confirmed
     ↓                   ↓
  (5 min)            (24 h SLA)
     ↓                   ↓
  cancelled          cancelled (+ customer notified + owner alerted)
```

---

## Cost

| Service | Cost |
|---|---|
| Render free tier | ₹0 |
| Twilio ~500 msgs/month | ~₹200 |
| Groq API | ~₹100 |
| **Total** | **~₹300/month** |

**Charge turf owners: ₹999 setup + ₹499–999/month**

---

## Running Tests

```bash
pytest tests/ -v
# 135 passed, 0 failed
```

---

Built by Bharath Kumar
