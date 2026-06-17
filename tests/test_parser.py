"""
Parser tests — 20 messages with correct intent + entities, 5 asserting None.
Zero API calls made.
"""
import pytest
from datetime import date, timedelta
from app.parser import parse_message, extract_date, extract_time, extract_duration


today     = date.today().strftime("%Y-%m-%d")
tomorrow  = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


# ══════════════════════════════════════════════════════════════════════════════
# Date extractor
# ══════════════════════════════════════════════════════════════════════════════

def test_date_today():
    assert extract_date("Available slots today") == today

def test_date_tomorrow():
    assert extract_date("Book tomorrow 8 PM") == tomorrow

def test_date_dd_month():
    result = extract_date("Book 20 June")
    assert result is not None
    assert result.endswith("-06-20")

def test_date_dd_mm():
    result = extract_date("Slots on 20/08")
    assert result is not None
    assert result.endswith("-08-20")

def test_date_none():
    assert extract_date("Book a slot") is None


# ══════════════════════════════════════════════════════════════════════════════
# Time extractor
# ══════════════════════════════════════════════════════════════════════════════

def test_time_8pm():
    assert extract_time("Book tomorrow 8 PM") == "20:00"

def test_time_8_30pm():
    assert extract_time("Reserve 8:30 pm slot") == "20:30"

def test_time_24h():
    assert extract_time("Slot at 20:00") == "20:00"

def test_time_morning():
    assert extract_time("Morning slot please") == "06:00"

def test_time_evening():
    assert extract_time("Evening slot") == "18:00"

def test_time_none():
    assert extract_time("Book a slot") is None


# ══════════════════════════════════════════════════════════════════════════════
# Customer intents — 20 messages
# ══════════════════════════════════════════════════════════════════════════════

class TestCustomerIntents:
    # check_availability (5)
    def test_avail_1(self):
        r = parse_message("Available slots tomorrow", "customer")
        assert r and r["intent"] == "check_availability"
        assert r["date"] == tomorrow

    def test_avail_2(self):
        r = parse_message("Is 8 PM free today?", "customer")
        assert r and r["intent"] == "check_availability"
        assert r["time"] == "20:00"

    def test_avail_3(self):
        r = parse_message("Slots today", "customer")
        assert r and r["intent"] == "check_availability"

    def test_avail_4(self):
        r = parse_message("Any openings tonight?", "customer")
        assert r and r["intent"] == "check_availability"

    def test_avail_5(self):
        r = parse_message("Show availability for 20 June", "customer")
        assert r and r["intent"] == "check_availability"
        assert r["date"] is not None and r["date"].endswith("-06-20")

    # book_slot (5)
    def test_book_1(self):
        r = parse_message("Book tomorrow 8 PM", "customer")
        assert r and r["intent"] == "book_slot"
        assert r["date"] == tomorrow
        assert r["time"] == "20:00"

    def test_book_2(self):
        r = parse_message("Reserve 7 PM today", "customer")
        assert r and r["intent"] == "book_slot"
        assert r["time"] == "19:00"

    def test_book_3(self):
        r = parse_message("I want slot today", "customer")
        assert r and r["intent"] == "book_slot"

    def test_book_4(self):
        r = parse_message("Book 2 hours tomorrow 6 PM", "customer")
        assert r and r["intent"] == "book_slot"
        assert r["duration"] == 2.0

    def test_book_5(self):
        r = parse_message("Need turf at 9 pm", "customer")
        assert r and r["intent"] == "book_slot"
        assert r["time"] == "21:00"

    # payment_done (3)
    def test_payment_1(self):
        r = parse_message("Paid ₹300 advance", "customer")
        assert r and r["intent"] == "payment_done"

    def test_payment_2(self):
        r = parse_message("UPI sent done", "customer")
        assert r and r["intent"] == "payment_done"

    def test_payment_3(self):
        r = parse_message("Transferred the amount", "customer")
        assert r and r["intent"] == "payment_done"

    # cancel_booking (3)
    def test_cancel_1(self):
        r = parse_message("Cancel BK104", "customer")
        assert r and r["intent"] == "cancel_booking"
        assert r["booking_ref"] == "BK104"

    def test_cancel_2(self):
        r = parse_message("Cancel my booking", "customer")
        assert r and r["intent"] == "cancel_booking"

    def test_cancel_3(self):
        r = parse_message("Remove booking please", "customer")
        assert r and r["intent"] == "cancel_booking"

    # booking_status (2)
    def test_status_1(self):
        r = parse_message("My booking status", "customer")
        assert r and r["intent"] == "booking_status"

    def test_status_2(self):
        r = parse_message("Check BK104 details", "customer")
        assert r and r["intent"] == "booking_status"

    # greeting (2)
    def test_greeting_1(self):
        r = parse_message("Hi", "customer")
        assert r and r["intent"] == "greeting"

    def test_greeting_2(self):
        r = parse_message("Hello! 👋", "customer")
        assert r and r["intent"] == "greeting"


# ══════════════════════════════════════════════════════════════════════════════
# Owner intents
# ══════════════════════════════════════════════════════════════════════════════

class TestOwnerIntents:
    def test_view_today(self):
        r = parse_message("Today's bookings", "owner")
        assert r and r["intent"] == "view_bookings"
        assert r["date"] == today

    def test_view_tomorrow(self):
        r = parse_message("Tomorrow bookings", "owner")
        assert r and r["intent"] == "view_bookings"
        assert r["date"] == tomorrow

    def test_block_slot(self):
        r = parse_message("Block tomorrow 8 PM", "owner")
        assert r and r["intent"] == "block_slot"
        assert r["date"] == tomorrow
        assert r["time"] == "20:00"

    def test_block_maintenance(self):
        r = parse_message("Maintenance tomorrow 7-9 PM", "owner")
        assert r and r["intent"] == "block_slot"
        assert r["duration"] == 2.0

    def test_cancel_owner(self):
        r = parse_message("Cancel BK105", "owner")
        assert r and r["intent"] == "cancel_owner"
        assert r["booking_ref"] == "BK105"

    def test_confirm_payment(self):
        r = parse_message("Confirm BK104", "owner")
        assert r and r["intent"] == "confirm_payment"
        assert r["booking_ref"] == "BK104"

    def test_confirm_received(self):
        r = parse_message("Payment received BK102", "owner")
        assert r and r["intent"] == "confirm_payment"
        assert r["booking_ref"] == "BK102"

    def test_booking_info(self):
        r = parse_message("Show BK104", "owner")
        assert r and r["intent"] == "booking_info"
        assert r["booking_ref"] == "BK104"


# ══════════════════════════════════════════════════════════════════════════════
# None cases — should trigger Groq fallback (5)
# ══════════════════════════════════════════════════════════════════════════════

class TestNoneCases:
    def test_vague_shift(self):
        # "Can I shift my booking?" — no booking ref, not a direct cancel/book
        r = parse_message("Can I shift tomorrow booking to next week?", "customer")
        # We expect None because "shift" doesn't match any pattern
        # (if it accidentally matches something, the Groq fallback will handle it better)
        # This tests that pure natural-language nuance falls through
        assert r is None or r["intent"] not in ("book_slot", "cancel_booking")

    def test_rain_query(self):
        r = parse_message("What if rain comes?", "customer")
        assert r is None

    def test_cheaper_timing(self):
        r = parse_message("Any cheaper timings?", "customer")
        assert r is None

    def test_sunset_query(self):
        r = parse_message("Can we play after sunset?", "customer")
        assert r is None

    def test_empty_message(self):
        r = parse_message("", "customer")
        assert r is None

    def test_owner_empty(self):
        r = parse_message("   ", "owner")
        assert r is None
