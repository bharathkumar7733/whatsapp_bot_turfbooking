"""
Integration tests for the owner command flow.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import date, timedelta

import app.db as db
import app.session as sess
from app.handlers.owner import handle_owner

OWNER = "whatsapp:+919000000000"
CUSTOMER = "whatsapp:+919876543210"
TODAY = date.today().strftime("%Y-%m-%d")
TOMORROW = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    sess._sessions.clear()

    mock_settings = MagicMock()
    mock_settings.turf_open_hour = 6
    mock_settings.turf_close_hour = 23
    mock_settings.owner_list = [OWNER]
    monkeypatch.setattr("app.handlers.owner.get_settings", lambda: mock_settings)
    yield
    sess._sessions.clear()


@pytest.fixture
def mock_send():
    with patch("app.handlers.owner.send_whatsapp_message") as m:
        yield m


# ── View bookings ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_view_bookings_today_empty(mock_send):
    await handle_owner(OWNER, "Today's bookings")
    body = mock_send.call_args[0][1]
    assert "Bookings" in body
    assert "No bookings" in body


@pytest.mark.asyncio
async def test_view_bookings_shows_entries(mock_send):
    db.create_booking(CUSTOMER, "Ravi", TODAY, "08:00", 1.0)
    db.create_booking(CUSTOMER, "Team B", TODAY, "10:00", 2.0)
    await handle_owner(OWNER, "Today's bookings")
    body = mock_send.call_args[0][1]
    assert "Ravi" in body
    assert "Team B" in body
    assert "BK101" in body


# ── Block slot ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_block_slot_success(mock_send):
    await handle_owner(OWNER, "Block tomorrow 8 PM")
    body = mock_send.call_args[0][1]
    assert "blocked" in body.lower() or "🔒" in body


@pytest.mark.asyncio
async def test_block_slot_fails_if_booked(mock_send):
    db.create_booking(CUSTOMER, "Ravi", TOMORROW, "20:00", 1.0)
    await handle_owner(OWNER, "Block tomorrow 8 PM")
    body = mock_send.call_args[0][1]
    assert "❌" in body or "cannot" in body.lower() or "already" in body.lower()


@pytest.mark.asyncio
async def test_block_slot_missing_time(mock_send):
    await handle_owner(OWNER, "Block tomorrow")
    body = mock_send.call_args[0][1]
    assert "time" in body.lower() or "⏰" in body


# ── Cancel booking (owner) ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_cancel_notifies_customer(mock_send):
    ref = db.create_booking(CUSTOMER, "Ravi", TOMORROW, "08:00", 1.0)
    await handle_owner(OWNER, f"Cancel {ref}")

    # Two messages: owner confirmation + customer notification
    assert mock_send.call_count == 2
    owner_msg = mock_send.call_args_list[0][0][1]
    customer_msg = mock_send.call_args_list[1][0][1]
    assert "cancelled" in owner_msg.lower()
    assert "cancelled" in customer_msg.lower()
    assert mock_send.call_args_list[1][0][0] == CUSTOMER


@pytest.mark.asyncio
async def test_owner_cancel_unknown_ref(mock_send):
    await handle_owner(OWNER, "Cancel BK999")
    body = mock_send.call_args[0][1]
    assert "not found" in body.lower() or "❌" in body


# ── Confirm payment ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_payment_sends_customer_confirmation(mock_send):
    ref = db.create_booking(CUSTOMER, "Ravi", TOMORROW, "08:00", 1.0)
    await handle_owner(OWNER, f"Confirm {ref}")

    # Owner ack + customer confirmation
    assert mock_send.call_count == 2
    customer_msg = mock_send.call_args_list[1][0][1]
    assert "Confirmed" in customer_msg or "confirmed" in customer_msg
    assert mock_send.call_args_list[1][0][0] == CUSTOMER

    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "confirmed"


@pytest.mark.asyncio
async def test_confirm_already_confirmed(mock_send):
    ref = db.create_booking(CUSTOMER, "Ravi", TOMORROW, "08:00", 1.0)
    db.confirm_booking(ref)
    await handle_owner(OWNER, f"Confirm {ref}")
    body = mock_send.call_args[0][1]
    assert "already" in body.lower() or "ℹ️" in body


# ── Booking info ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_booking_info(mock_send):
    ref = db.create_booking(CUSTOMER, "Ravi", TOMORROW, "08:00", 1.0)
    await handle_owner(OWNER, f"Show {ref}")
    body = mock_send.call_args[0][1]
    assert "Ravi" in body
    assert ref in body
    assert CUSTOMER in body
