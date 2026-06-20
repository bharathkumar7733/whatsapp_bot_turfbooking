"""
Integration tests for booking corrections, confirmations, slot locks, and database session robustness.
"""
import pytest
import time
from unittest.mock import patch, MagicMock
from datetime import date, timedelta, datetime

import app.db as db
import app.session as sess
from app.handlers.customer import handle_customer

PHONE = "whatsapp:+919876543210"
OWNER = "whatsapp:+919000000000"
TODAY = date.today().strftime("%Y-%m-%d")
TOMORROW = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_corr.db"))
    db.init_db()
    sess._sessions.clear()

    # Patch settings
    mock_settings = MagicMock()
    mock_settings.advance_amount = 300
    mock_settings.upi_id = "test@upi"
    mock_settings.turf_name = "Test Turf"
    mock_settings.turf_location = "Chennai"
    mock_settings.turf_open_hour = 6
    mock_settings.turf_close_hour = 23
    mock_settings.turf_price_per_slot = 600
    mock_settings.owner_list = [OWNER]
    mock_settings.groq_api_key = ""
    monkeypatch.setattr("app.config.get_settings", lambda: mock_settings)
    yield
    sess._sessions.clear()


@pytest.fixture
def mock_send():
    with patch("app.handlers.customer.send_whatsapp_message") as m:
        yield m


# ── Mid-conversation corrections ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mid_flow_corrections(mock_send):
    # Step 1: Start booking
    await handle_customer(PHONE, "Book tomorrow 8 PM")
    assert sess.get_state(PHONE) == "awaiting_duration"
    data = sess.get_data(PHONE)
    assert data["date"] == TOMORROW
    assert data["time"] == "20:00"

    # Step 2: Mid-flow correction - change to today 9 PM instead
    await handle_customer(PHONE, "Actually today 9 PM")
    assert sess.get_state(PHONE) == "awaiting_duration"
    data = sess.get_data(PHONE)
    assert data["date"] == TODAY
    assert data["time"] == "21:00"

    # Step 3: Provide duration as 2 hours
    await handle_customer(PHONE, "2 hours")
    assert sess.get_state(PHONE) == "awaiting_name"
    assert sess.get_data(PHONE)["duration"] == 2.0

    # Step 4: Mid-flow correction - change date back to tomorrow
    await handle_customer(PHONE, "No wait, change it to tomorrow")
    assert sess.get_state(PHONE) == "awaiting_name"
    data = sess.get_data(PHONE)
    assert data["date"] == TOMORROW
    assert data["time"] == "21:00"      # time kept
    assert data["duration"] == 2.0      # duration kept


# ── Non-destructive change confirmation ────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_confirmation_flow(mock_send):
    # Step 1: Go through full flow and create pending booking
    await handle_customer(PHONE, "Book tomorrow 8 PM")
    await handle_customer(PHONE, "1 hour")
    await handle_customer(PHONE, "Ravi Kumar")
    
    assert sess.get_state(PHONE) == "awaiting_payment"
    ref = sess.get_data(PHONE)["booking_ref"]
    assert ref.startswith("BK")
    
    # Booking is in DB
    bk = db.get_booking_by_ref(ref)
    assert bk is not None
    assert bk["status"] == "pending_payment"

    # Step 2: User requests change during payment state
    await handle_customer(PHONE, "Actually tomorrow 9 PM instead")
    assert sess.get_state(PHONE) == "confirm_change"
    data = sess.get_data(PHONE)
    assert data["proposed_time"] == "21:00"
    assert data["booking_ref"] == ref
    
    # Verify old booking is NOT cancelled yet
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "pending_payment"

    # Step 3: User decides to KEEP the existing booking
    await handle_customer(PHONE, "Keep existing booking")
    assert sess.get_state(PHONE) == "awaiting_payment"
    assert sess.get_data(PHONE).get("proposed_time") is None
    
    # Old booking remains pending
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "pending_payment"

    # Step 4: User requests change again
    await handle_customer(PHONE, "Actually change to 9 PM tomorrow")
    assert sess.get_state(PHONE) == "confirm_change"

    # Step 5: User confirms UPDATE
    await handle_customer(PHONE, "Update")
    # Old booking must be cancelled
    bk_old = db.get_booking_by_ref(ref)
    assert bk_old["status"] == "cancelled"
    
    # Session is transitioned to name collection or next field
    assert sess.get_state(PHONE) == "awaiting_name"
    new_data = sess.get_data(PHONE)
    assert new_data.get("booking_ref") is None
    assert new_data["date"] == TOMORROW
    assert new_data["time"] == "21:00"
    assert new_data["duration"] == 1.0


# ── Expiry & Slot lock release ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slot_lock_expiration():
    # 1. Create a booking (locks the slot)
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "18:00", 1.0)
    assert ref is not None
    assert db.check_slot_available(TOMORROW, "18:00", 1.0) is False

    # 2. Fake the slot lock expiry in DB to be in the past
    with db.get_conn() as conn:
        past_time = (datetime.utcnow() - timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE bookings SET reserved_until = ? WHERE booking_ref = ?", (past_time, ref))

    # 3. Check slot availability again — should be True (released!)
    assert db.check_slot_available(TOMORROW, "18:00", 1.0) is True

    # 4. Try to book the same slot — should succeed!
    ref2 = db.create_booking("whatsapp:+910000000001", "Ajay", TOMORROW, "18:00", 1.0)
    assert ref2 is not None
    assert ref2 != ref


# ── Server Restart Robustness (DB Session Store) ───────────────────────────────

@pytest.mark.asyncio
async def test_session_restart_robustness():
    # 1. Write some state to SessionStore
    sess.set_state(PHONE, "awaiting_time", {"date": TOMORROW})
    assert sess.get_state(PHONE) == "awaiting_time"
    assert sess.get_data(PHONE)["date"] == TOMORROW

    # 2. Verify that it is persisted in the database (restart robustness)
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE phone = ?", (PHONE,)).fetchone()
        assert row is not None
        assert row["state"] == "awaiting_time"
        assert row["date"] == TOMORROW
    assert sess.get_data(PHONE)["date"] == TOMORROW
