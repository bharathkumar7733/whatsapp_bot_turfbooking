# Turf Booking WhatsApp Agent — Setup Guide

Everything runs through WhatsApp. No website. No dashboard. No app.

---

## What this does

- Customers message your WhatsApp → AI handles bookings end-to-end
- Customers pay via UPI → send screenshot → you confirm with one message
- You get instant notifications for every payment
- Auto-reminders sent 30 minutes before every slot
- Zero double bookings — slots locked in database

---

## Step 1 — Get your accounts (all free to start)

| Service | Link | What to get |
|---|---|---|
| Twilio | twilio.com | Account SID, Auth Token, WhatsApp sandbox number |
| Groq | console.groq.com | Free API key (optional but recommended) |
| Render | render.com | Free hosting account |

### Twilio WhatsApp setup
1. Sign up at twilio.com
2. Go to **Messaging → Try it out → Send a WhatsApp message**
3. Note your sandbox number (e.g. `+14155238886`)
4. Your customers join the sandbox by sending `join <your-word>` to that number
5. For production, apply for a WhatsApp Business number (takes 2-3 days)

---

## Step 2 — Deploy to Render

1. Push this project to a GitHub repo
2. Go to render.com → **New Web Service** → connect your repo
3. Set build command: `pip install -r requirements.txt`
4. Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. Add a **Disk** → mount path `/data` → size 1 GB (for SQLite)
6. Add environment variables (see Step 3)
7. Deploy — you'll get a URL like `https://turf-agent.onrender.com`

---

## Step 3 — Environment variables

Set these in the Render dashboard under **Environment**:

```
TWILIO_ACCOUNT_SID       = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN        = your_auth_token
TWILIO_WHATSAPP_NUMBER   = whatsapp:+14155238886

GROQ_API_KEY             = gsk_...          (optional — enables natural language)

TURF_NAME                = Champions Turf
TURF_LOCATION            = Anna Nagar, Chennai
TURF_OPEN_HOUR           = 6
TURF_CLOSE_HOUR          = 23
TURF_PRICE_PER_SLOT      = 600
ADVANCE_AMOUNT           = 300
UPI_ID                   = yourname@upi

OWNER_NUMBERS            = whatsapp:+919876543210,whatsapp:+919123456789

DB_PATH                  = /data/turf.db
APP_ENV                  = production
```

**OWNER_NUMBERS** — comma-separated WhatsApp numbers of all turf owners.
These numbers get admin commands (view bookings, confirm payments, block slots).

---

## Step 4 — Connect Twilio to your server

1. In Twilio console → Messaging → Settings → WhatsApp Sandbox
2. Set **When a message comes in** webhook to:
   ```
   https://turf-agent.onrender.com/webhook
   ```
3. Method: **HTTP POST**
4. Save

---

## Step 5 — Test it

Send these messages to your WhatsApp sandbox number:

| Message | Expected response |
|---|---|
| `Hi` | Welcome message with turf info |
| `slots today` | List of available time slots |
| `book tomorrow 8 PM` | Starts booking flow |
| `slots` | Available slots for today |

From an owner number:
| Message | Expected response |
|---|---|
| `today's bookings` | List of today's bookings |
| `block tomorrow 7 PM` | Slot blocked confirmation |
| `confirm BK101` | Payment confirmed + customer notified |

---

## Customer booking flow

```
Customer: book tomorrow 8 PM
Bot: Booking Tomorrow at 8:00 PM for 1 hr. What's your name?

Customer: Ravi Kumar
Bot: ✅ Booking Created! BK101
     Pay ₹300 advance to yourname@upi
     Send screenshot after payment.

Customer: [sends UPI screenshot]
Bot: Screenshot received! Waiting for owner confirmation.

Owner: confirm BK101
Bot → Owner: ✅ BK101 confirmed!
Bot → Customer: 🎉 Booking Confirmed! See you on the turf!

[30 min before slot]
Bot → Customer: ⏰ Reminder! Your booking BK101 is in 30 minutes.
```

---

## Owner commands

| Command | What it does |
|---|---|
| `today's bookings` | Show all bookings for today |
| `tomorrow bookings` | Show tomorrow's bookings |
| `block tomorrow 8 PM` | Block a slot (e.g. for maintenance) |
| `block tomorrow 7-9 PM` | Block 2-hour slot |
| `confirm BK101` | Confirm payment → customer notified |
| `cancel BK101` | Cancel any booking → customer notified |
| `show BK101` | Full details of a booking |

---

## Costs (per month, 1 turf)

| Item | Cost |
|---|---|
| Render free tier | ₹0 |
| Twilio (~500 messages) | ~₹200 |
| Groq API | ~₹100 |
| **Total** | **~₹300/month** |

You can charge the turf owner ₹2,000–5,000/month.

---

## Troubleshooting

**Bot not responding?**
- Check Twilio webhook URL is set correctly
- Check Render logs: Dashboard → your service → Logs
- Verify OWNER_NUMBERS format: must be `whatsapp:+91XXXXXXXXXX`

**"I didn't understand that" for everything?**
- Set GROQ_API_KEY to enable natural language understanding

**Double booking possible?**
- No — SQLite uses WAL mode + overlap check on every booking attempt

**SQLite data lost on redeploy?**
- Only if you didn't add the Render Disk. Always add the `/data` disk.
