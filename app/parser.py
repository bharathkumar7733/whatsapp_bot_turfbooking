"""
Rule-based message parser.

parse_message(text, role) -> dict | None

Returns a dict with at minimum {"intent": str} plus any extracted entities,
or None if no pattern matched (triggers Groq fallback).

Roles: "customer" | "owner"
"""
import re
from datetime import date, timedelta
from typing import Optional

# ── Text normalisation ─────────────────────────────────────────────────────────

# Strip common emoji ranges so regex matches survive emoji-heavy messages
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002700-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "]+",
    flags=re.UNICODE,
)


def _clean(text: str) -> str:
    """Lowercase, strip emojis, collapse whitespace."""
    text = _EMOJI_RE.sub(" ", text)
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


# ── Date extractor ─────────────────────────────────────────────────────────────

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "july": 7, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4,
    "june": 6, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def extract_date(text: str) -> Optional[str]:
    """
    Extract a date from text and return as 'YYYY-MM-DD', or None.

    Handles:
      today / tomorrow / day after tomorrow
      DD month  e.g. '20 june', '5 jan'
      DD/MM or DD-MM  e.g. '20/06', '20-06'
    """
    t = _clean(text)
    today = date.today()

    if re.search(r"\btoday\b|\baaj\b", t):
        return today.strftime("%Y-%m-%d")
    if re.search(r"\btomorrow\b|\bkal\b|\bkl\b", t):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"\bday after tomorrow\b|\bparso\b", t):
        return (today + timedelta(days=2)).strftime("%Y-%m-%d")

    # "20 june" / "5 jan"
    m = re.search(
        r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
        r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
        r"nov(?:ember)?|dec(?:ember)?)\b",
        t,
    )
    if m:
        day = int(m.group(1))
        month = _MONTH_MAP[m.group(2).lower()]
        year = today.year
        # roll over to next year if date already passed
        candidate = date(year, month, day)
        if candidate < today:
            candidate = date(year + 1, month, day)
        return candidate.strftime("%Y-%m-%d")

    # DD/MM or DD-MM
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            year = today.year
            candidate = date(year, month, day)
            if candidate < today:
                candidate = date(year + 1, month, day)
            return candidate.strftime("%Y-%m-%d")

    return None


# ── Time extractor ─────────────────────────────────────────────────────────────

def extract_time(text: str) -> Optional[str]:
    """
    Extract a start time from text and return as 'HH:MM' (24h), or None.

    Handles:
      8 pm / 8pm / 8:30 pm / 20:00 / 20h
      morning  → 06:00
      afternoon → 14:00
      evening  → 18:00
      night    → 20:00
    """
    t = _clean(text)

    # HH:MM am/pm  e.g. "8:30 pm"
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm)\b", t)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        meridiem = m.group(3)
        if meridiem == "pm" and h != 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        return f"{h:02d}:{mn:02d}"

    # H am/pm  e.g. "8 pm", "9pm"
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1))
        meridiem = m.group(2)
        if meridiem == "pm" and h != 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        return f"{h:02d}:00"

    # 24h  e.g. "20:00" or "20h"
    m = re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)?\b", t)
    if m:
        h = int(m.group(1))
        mn = int(m.group(2)) if m.group(2) else 0
        if 0 <= h <= 23:
            return f"{h:02d}:{mn:02d}"

    # Fuzzy time words
    if re.search(r"\bmorning\b|\bsubah\b", t):
        return "06:00"
    if re.search(r"\bafternoon\b|\bdupehr\b", t):
        return "14:00"
    if re.search(r"\bevening\b|\bshaam\b", t):
        return "18:00"
    if re.search(r"\bnight\b|\braat\b", t):
        return "20:00"

    return None


# ── Duration extractor ─────────────────────────────────────────────────────────

def extract_duration(text: str) -> float:
    """
    Extract duration in hours from text. Defaults to 1.0.
    Handles: "2 hours", "1.5 hrs", "for 2h", "2 ghante"
    """
    t = _clean(text)
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|ghante|ghanta)\b", t)
    if m:
        val = float(m.group(1))
        if 1 <= val <= 4:   # sanity cap
            return val
    # Range like "6-8 PM" → 2 hours
    m = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(?:am|pm)?\b", t)
    if m:
        start = int(m.group(1))
        end = int(m.group(2))
        diff = end - start
        if 1 <= diff <= 4:
            return float(diff)
    return 1.0


# ── Booking ref extractor ──────────────────────────────────────────────────────

def extract_booking_ref(text: str) -> Optional[str]:
    """Extract BKxxx reference from text."""
    m = re.search(r"\b(BK\d{3,})\b", text.upper())
    return m.group(1) if m else None


# ── Customer intents ───────────────────────────────────────────────────────────

_CUSTOMER_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Order matters — most specific first
    (
        "cancel_booking",
        re.compile(
            r"\b(cancel|remove|delete|band\s*kar|nahi\s*chahiye)\b",
            re.I,
        ),
    ),
    (
        "booking_status",
        re.compile(
            r"\b(my\s*booking|status|check\s*BK\d+|booking\s*detail|show\s*booking"
            r"|BK\d{3,})",
            re.I,
        ),
    ),
    (
        "payment_done",
        re.compile(
            r"\b(paid|payment\s*done|transferred|upi\s*sent|sent\s*advance|"
            r"payment\s*complet|check\s*payment|pay\s*kar\s*diya)\b",
            re.I,
        ),
    ),
    (
        "book_slot",
        re.compile(
            r"\b(book|reserve|need\s*(turf|ground|slot)|want\s*(to\s*)?play|"
            r"want\s+slot|i\s+want|schedule|ground\s*chahiye|slot\s*chahiye|"
            r"cricket|football)\b",
            re.I,
        ),
    ),
    (
        "check_availability",
        re.compile(
            r"\b(available|availab|slots?\s*(today|tomorrow|free|list)?|free\s*slot"
            r"|free|open|any\s*time|availability|openings|khali|khaali|timing)\b",
            re.I,
        ),
    ),
    (
        "greeting",
        re.compile(
            r"(^|\s)(hi|hello|hey|hlo|hii|namaste|namaskar|sup|yo)\b"
            r"|^hai$",
            re.I,
        ),
    ),
]


def _parse_customer(text: str) -> Optional[dict]:
    clean = _clean(text)
    for intent, pattern in _CUSTOMER_PATTERNS:
        if pattern.search(clean):
            result: dict = {"intent": intent}
            result["date"] = extract_date(text)
            result["time"] = extract_time(text)
            result["duration"] = extract_duration(text)
            result["booking_ref"] = extract_booking_ref(text)
            return result
    return None


# ── Owner intents ──────────────────────────────────────────────────────────────

_OWNER_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "confirm_payment",
        re.compile(
            r"\b(confirm|approve|mark\s*paid|payment\s*received|received\s*payment|"
            r"paid\s*kar\s*do|confirm\s*kar)\b",
            re.I,
        ),
    ),
    (
        "cancel_owner",
        re.compile(
            r"\b(cancel|remove|delete)\b",
            re.I,
        ),
    ),
    (
        "block_slot",
        re.compile(
            r"\b(block|close|disable|maintenance|band\s*kar|mat\s*do)\b",
            re.I,
        ),
    ),
    (
        "booking_info",
        re.compile(
            r"(?:\b(show|details?|info|who\s*booked|customer\s*detail)\b.*BK\d{3,}"
            r"|BK\d{3,}.*\b(details?|info|show)\b)",
            re.I,
        ),
    ),
    (
        "view_bookings",
        re.compile(
            r"\b(booking|bookings|today|tomorrow|schedule|list|occupied|slots?\s*list|"
            r"aaj|kal)\b",
            re.I,
        ),
    ),
]


def _parse_owner(text: str) -> Optional[dict]:
    clean = _clean(text)
    for intent, pattern in _OWNER_PATTERNS:
        if pattern.search(clean):
            result: dict = {"intent": intent}
            result["date"] = extract_date(text)
            result["time"] = extract_time(text)
            result["duration"] = extract_duration(text)
            result["booking_ref"] = extract_booking_ref(text)
            return result
    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def parse_message(text: str, role: str) -> Optional[dict]:
    """
    Parse an incoming WhatsApp message into structured intent + entities.

    Args:
        text: Raw message text from the user.
        role: "customer" or "owner"

    Returns:
        dict with keys: intent, date, time, duration, booking_ref
        or None if no pattern matched (caller should invoke Groq fallback).
    """
    if not text or not text.strip():
        return None

    if role == "owner":
        return _parse_owner(text)
    return _parse_customer(text)
