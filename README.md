# WhatsApp Turf Booking Agent 🏏

A WhatsApp AI agent that handles turf bookings end-to-end. No website, no app, no dashboard — everything happens inside WhatsApp.

Built for turf owners who currently manage bookings manually through calls and WhatsApp groups.

---

## How It Works

**Customer side:**
```
Customer: 6pm to 9pm tomorrow
Bot: 📋 Tomorrow 6:00 PM – 9:00 PM (3 hr). What's your name?

Customer: Ravi Kumar
Bot: ✅ Booking Created! BK101
     Pay ₹300 advance to owner@upi
     Send screenshot after payment.

Customer: [sends UPI screenshot]
Bot: Screenshot received! Owner will confirm shortly.

Owner: confirm BK101
Bot → Customer: 🎉 Booking Confirmed! See you on the turf!
```

**Owner side:**
```
today's bookings     → full list with status
block tomorrow 8 PM  → slot blocked
confirm BK101        → payment confirmed, customer notified
cancel BK101         → booking cancelled, customer notified
show BK101           → full booking details
```

---

## Tech Stack

| Layer | Tech |
|---|---|
| Messaging | Twilio WhatsApp API |
| Backend | FastAPI (Python) |
| AI | Groq (llama-3.1-8b-instant) |
| Database | SQLite |
| Scheduler | APScheduler |
| Deploy | Render (free tier) |

---

## Features

- Natural language booking — "6pm to 9pm tomorrow" works in one message
- AI-first understanding via Groq, rule-based parser as fast-path
- Double booking prevention with overlap detection
- Manual UPI payment — customer sends screenshot, owner confirms
- Owner phone whitelist — owner commands locked to specific numbers
- 30-min booking reminders (APScheduler)
- Pending bookings auto-expire after 5 minutes
- Change booking mid-flow — "No, I want 7pm instead" works
- Global error handler — never silently fails on WhatsApp
- 105 tests passing

---

## Project Structure

```
turf-agent/
├── app/
│   ├── main.py              # FastAPI app, lifespan, error handler
│   ├── config.py            # Settings from .env
│   ├── router.py            # POST /webhook — Twilio entry point
│   ├── db.py                # SQLite schema + all DB helpers
│   ├── parser.py            # Regex parser (fast-path, zero API cost)
│   ├── session.py           # In-memory multi-turn state machine
│   ├── groq_fallback.py     # Groq AI for natural language
│   ├── scheduler.py         # Reminders + pending expiry jobs
│   ├── twilio_client.py     # Twilio send wrapper
│   └── handlers/
│       ├── customer.py      # Full customer booking flow (AI-first)
│       └── owner.py         # Owner commands
├── tests/                   # 105 tests
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
# Fill in your Twilio, Groq, and turf details
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
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886

GROQ_API_KEY=gsk_...

TURF_NAME=Champions Turf
TURF_LOCATION=Anna Nagar, Chennai
TURF_OPEN_HOUR=6
TURF_CLOSE_HOUR=23
TURF_PRICE_PER_SLOT=600
ADVANCE_AMOUNT=300
UPI_ID=yourname@upi

OWNER_NUMBERS=whatsapp:+91XXXXXXXXXX

DB_PATH=turf.db
APP_ENV=development
```

---

## Deploy to Render

1. Push to GitHub
2. New Web Service → connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add Disk → mount path `/data` → 1 GB
6. Add all env vars
7. Deploy → copy URL → update Twilio webhook

Full guide in [SETUP.md](SETUP.md)

---

## Cost

| Service | Cost |
|---|---|
| Render free tier | ₹0 |
| Twilio ~500 msgs/month | ~₹200 |
| Groq API | ~₹100 |
| **Total** | **~₹300/month** |

---

## Running Tests

```bash
pytest tests/ -v
# 105 passed
```

---

Built by Bharath Kumar
