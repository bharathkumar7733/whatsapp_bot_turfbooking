"""
Integration tests for the customer booking flow.
Twilio send is mocked — we only verify state transitions and DB writes.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

import app.db as db
import app.session as sess
from app.handlers.customer import handle_customer

PHONE = "whatsapp:+919876543210"
OWNER = "whatsapp:+919000000000"
TODAY = date.today().strftime("%Y-%m-%d")
TOMORROW = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
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
    monkeypatch.setattr("app.handlers.customer.get_settings", lambda: mock_settings)
    yield
    sess._sessions.clear()


@pytest.fixture
def mock_send():
    with patch("app.handlers.customer.send_whatsapp_message") as m:
        yield m


# ── Greeting ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_greeting_response(mock_send):
    await handle_customer(PHONE, "Hi")
    mock_send.assert_called_once()
    assert "Welcome" in mock_send.call_args[0][1]


# ── Check availability ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_availability_shows_slots(mock_send):
    await handle_customer(PHONE, "Slots today")
    mock_send.assert_called_once()
    body = mock_send.call_args[0][1]
    assert "Available Slots" in body or "No free slots" in body


# ── Full booking flow (6 turns) ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_booking_flow(mock_send):
    # Turn 1: book intent
    await handle_customer(PHONE, "book")
    assert sess.get_state(PHONE) == "awaiting_date"

    # Turn 2: provide date
    await handle_customer(PHONE, "tomorrow")
    assert sess.get_state(PHONE) == "awaiting_time"
    assert sess.get_data(PHONE)["date"] == TOMORROW

    # Turn 3: provide time
    await handle_customer(PHONE, "8 PM")
    assert sess.get_state(PHONE) == "awaiting_duration"
    assert sess.get_data(PHONE)["time"] == "20:00"

    # Turn 4: provide duration
    await handle_customer(PHONE, "1 hour")
    assert sess.get_state(PHONE) == "awaiting_name"
    assert sess.get_data(PHONE)["duration"] == 1.0

    # Turn 5: provide name
    await handle_customer(PHONE, "Ravi Kumar")
    assert sess.get_state(PHONE) == "awaiting_payment"
    ref = sess.get_data(PHONE)["booking_ref"]
    assert ref.startswith("BK")

    # Booking in DB with pending_payment
    bk = db.get_booking_by_ref(ref)
    assert bk is not None
    assert bk["status"] == "pending_payment"
    assert bk["name"] == "Ravi Kumar"

    # UPI details sent
    calls = [c[0][1] for c in mock_send.call_args_list]
    assert any("upi" in c.lower() or "300" in c for c in calls)


@pytest.mark.asyncio
async def test_screenshot_notifies_owner(mock_send):
    """After screenshot is sent, owners get notified."""
    # Fast-forward to awaiting_payment state
    sess.set_state(PHONE, "awaiting_payment", {
        "booking_ref": "BK101",
        "name": "Ravi",
    })
    # Manually insert booking so get_booking_by_ref works
    db.create_booking(PHONE, "Ravi", TOMORROW, "20:00", 1.0)

    await handle_customer(PHONE, "UTR 123456789012", media_url="https://example.com/ss.jpg")

    assert sess.get_state(PHONE) == "done"
    # Should have sent: owner notify + customer ack
    assert mock_send.call_count >= 2
    owner_calls = [c for c in mock_send.call_args_list if c[0][0] == OWNER]
    assert len(owner_calls) == 1
    assert "confirm" in owner_calls[0][0][1].lower()


# ── Slot taken — shows alternatives ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_slot_taken_shows_free_slots(mock_send):
    # Block 8 PM tomorrow
    db.block_slot(TOMORROW, "20:00", 1.0)
    await handle_customer(PHONE, "Book tomorrow 8 PM")
    body = mock_send.call_args[0][1]
    assert "already booked" in body or "taken" in body or "Available" in body


# ── Cancel booking ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_own_booking(mock_send):
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "08:00", 1.0)
    await handle_customer(PHONE, f"Cancel {ref}")
    body = mock_send.call_args[0][1]
    assert "cancelled" in body.lower()

    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_someone_elses_booking_rejected(mock_send):
    other = "whatsapp:+910000000001"
    ref = db.create_booking(other, "Someone", TOMORROW, "10:00", 1.0)
    await handle_customer(PHONE, f"Cancel {ref}")
    body = mock_send.call_args[0][1]
    # Should be rejected
    assert "doesn't belong" in body or "not found" in body.lower() or "❌" in body


# ── Booking status ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_booking_status(mock_send):
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "09:00", 1.0)
    await handle_customer(PHONE, "My booking status")
    body = mock_send.call_args[0][1]
    assert ref in body
