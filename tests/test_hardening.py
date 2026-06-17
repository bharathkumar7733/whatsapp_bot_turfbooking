"""
Hardening tests — malformed inputs must never crash the system.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import date, timedelta

import app.db as db
import app.session as sess
from app.handlers.customer import handle_customer
from app.handlers.owner import handle_owner
from app.router import _sanitise

PHONE = "whatsapp:+919876543210"
OWNER = "whatsapp:+919000000000"
TOMORROW = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    sess._sessions.clear()

    mock_settings = MagicMock()
    mock_settings.advance_amount = 300
    mock_settings.upi_id = "test@upi"
    mock_settings.turf_name = "Test Turf"
    mock_settings.turf_location = "Chennai"
    mock_settings.turf_open_hour = 6
    mock_settings.turf_close_hour = 23
    mock_settings.turf_price_per_slot = 600
    mock_settings.owner_list = [OWNER]
    mock_settings.groq_api_key = ""  # no Groq key — tests default fallback path
    monkeypatch.setattr("app.handlers.customer.get_settings", lambda: mock_settings)
    monkeypatch.setattr("app.handlers.owner.get_settings", lambda: mock_settings)
    monkeypatch.setattr("app.groq_fallback.get_settings", lambda: mock_settings)
    yield
    sess._sessions.clear()


@pytest.fixture
def mock_send():
    with patch("app.handlers.customer.send_whatsapp_message") as mc, \
         patch("app.handlers.owner.send_whatsapp_message") as mo:
        yield mc, mo


# ── Sanitiser unit tests ───────────────────────────────────────────────────────

def test_sanitise_strips_control_chars():
    result = _sanitise("hello\x00\x01\x02world")
    assert "\x00" not in result
    assert "hello" in result
    assert "world" in result


def test_sanitise_keeps_emoji():
    result = _sanitise("Book tomorrow 🏏 8 PM")
    assert "Book" in result
    assert "🏏" in result


def test_sanitise_truncates_long_input():
    result = _sanitise("a" * 2000)
    assert len(result) == 1000


def test_sanitise_empty_string():
    assert _sanitise("") == ""


# ── Customer: malformed inputs don't crash ─────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_body(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "")
    # Should send something (fallback), not raise
    mc.assert_called_once()


@pytest.mark.asyncio
async def test_emojis_only(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "🏏🏏🏏🏏🏏")
    mc.assert_called_once()


@pytest.mark.asyncio
async def test_very_long_message(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "book " * 200)
    # Should handle gracefully — either enters booking flow or fallback
    mc.assert_called_once()


@pytest.mark.asyncio
async def test_unicode_hindi(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "कल 8 बजे बुक करो")
    mc.assert_called_once()  # no crash


@pytest.mark.asyncio
async def test_gibberish(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "xyzzy asdf qwerty 123!@#")
    mc.assert_called_once()


@pytest.mark.asyncio
async def test_only_numbers(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "999999999")
    mc.assert_called_once()


@pytest.mark.asyncio
async def test_sql_injection_attempt(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "'; DROP TABLE bookings; --")
    mc.assert_called_once()
    # Verify bookings table still intact
    rows = db.get_bookings_by_date(TOMORROW)
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_unknown_booking_ref(mock_send):
    mc, _ = mock_send
    await handle_customer(PHONE, "Cancel BK999")
    mc.assert_called_once()
    body = mc.call_args[0][1]
    assert "not found" in body.lower() or "❌" in body


# ── Owner: malformed inputs ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_owner_empty_body(mock_send):
    _, mo = mock_send
    await handle_owner(OWNER, "")
    mo.assert_called_once()


@pytest.mark.asyncio
async def test_owner_gibberish(mock_send):
    _, mo = mock_send
    await handle_owner(OWNER, "!!!!!????####")
    mo.assert_called_once()


@pytest.mark.asyncio
async def test_owner_block_no_time(mock_send):
    _, mo = mock_send
    await handle_owner(OWNER, "Block tomorrow")
    body = mo.call_args[0][1]
    assert "time" in body.lower() or "⏰" in body
