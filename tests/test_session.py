"""
Tests for multi-turn session state machine.
Simulates a full 6-message booking conversation.
"""
import time
import pytest
import app.session as session


PHONE = "whatsapp:+919876543210"


@pytest.fixture(autouse=True)
def clean_sessions():
    """Wipe session store before every test."""
    session._sessions.clear()
    yield
    session._sessions.clear()


# ── Basic state transitions ────────────────────────────────────────────────────

def test_fresh_session_is_idle():
    assert session.get_state(PHONE) == "idle"


def test_set_state_transitions():
    session.set_state(PHONE, "awaiting_date")
    assert session.get_state(PHONE) == "awaiting_date"


def test_invalid_state_raises():
    with pytest.raises(ValueError):
        session.set_state(PHONE, "flying_spaghetti_monster")


def test_data_accumulates_across_set_state():
    session.set_state(PHONE, "awaiting_time", {"date": "2025-08-01"})
    session.set_state(PHONE, "awaiting_duration", {"time": "20:00"})
    data = session.get_data(PHONE)
    assert data["date"] == "2025-08-01"
    assert data["time"] == "20:00"


def test_update_data_without_state_change():
    session.set_state(PHONE, "awaiting_name", {"date": "2025-08-01"})
    session.update_data(PHONE, name="Ravi")
    assert session.get_data(PHONE)["name"] == "Ravi"
    assert session.get_state(PHONE) == "awaiting_name"   # state unchanged


def test_clear_session_resets_to_idle():
    session.set_state(PHONE, "awaiting_payment", {"date": "2025-08-01"})
    session.clear_session(PHONE)
    assert session.get_state(PHONE) == "idle"
    assert session.get_data(PHONE) == {}


# ── Full 6-message booking conversation ───────────────────────────────────────

def test_full_booking_conversation():
    """
    Simulate:
      1. Customer says "book"       → idle
      2. Bot asks for date          → awaiting_date
      3. Customer says "tomorrow"   → awaiting_time
      4. Customer says "8 PM"       → awaiting_duration
      5. Customer says "1 hour"     → awaiting_name
      6. Customer gives name        → awaiting_payment
      7. Customer sends screenshot  → done
    """
    # Step 1: initial state
    assert session.get_state(PHONE) == "idle"

    # Step 2: bot sets awaiting_date after "book" intent
    session.set_state(PHONE, "awaiting_date")
    assert session.get_state(PHONE) == "awaiting_date"

    # Step 3: customer provides date
    session.set_state(PHONE, "awaiting_time", {"date": "2025-08-02"})
    assert session.get_state(PHONE) == "awaiting_time"
    assert session.get_data(PHONE)["date"] == "2025-08-02"

    # Step 4: customer provides time
    session.set_state(PHONE, "awaiting_duration", {"time": "20:00"})
    assert session.get_state(PHONE) == "awaiting_duration"
    assert session.get_data(PHONE)["time"] == "20:00"

    # Step 5: customer provides duration
    session.set_state(PHONE, "awaiting_name", {"duration": 1.0})
    assert session.get_state(PHONE) == "awaiting_name"
    assert session.get_data(PHONE)["duration"] == 1.0

    # Step 6: customer provides name
    session.set_state(PHONE, "awaiting_payment", {"name": "Ravi Kumar"})
    assert session.get_state(PHONE) == "awaiting_payment"
    assert session.get_data(PHONE)["name"] == "Ravi Kumar"

    # Verify all fields present
    data = session.get_data(PHONE)
    assert data["date"] == "2025-08-02"
    assert data["time"] == "20:00"
    assert data["duration"] == 1.0
    assert data["name"] == "Ravi Kumar"

    # Step 7: screenshot received → done
    session.set_state(PHONE, "done", {"booking_ref": "BK101"})
    assert session.get_state(PHONE) == "done"
    assert session.get_data(PHONE)["booking_ref"] == "BK101"


# ── Expiry ─────────────────────────────────────────────────────────────────────

def test_expired_session_resets_to_idle(monkeypatch):
    session.set_state(PHONE, "awaiting_date", {"date": "2025-08-01"})
    # Fake the updated_at to be 11 minutes ago
    session._sessions[PHONE]["updated_at"] -= 660
    # Next access should create a fresh session
    assert session.get_state(PHONE) == "idle"
    assert session.get_data(PHONE) == {}


def test_active_session_not_pruned(monkeypatch):
    session.set_state(PHONE, "awaiting_date", {"date": "2025-08-01"})
    # Only 5 minutes old — should survive
    session._sessions[PHONE]["updated_at"] -= 300
    assert session.get_state(PHONE) == "awaiting_date"


def test_all_sessions_excludes_expired():
    phone2 = "whatsapp:+910000000001"
    session.set_state(PHONE, "awaiting_date")
    session.set_state(phone2, "awaiting_time")
    # Expire PHONE
    session._sessions[PHONE]["updated_at"] -= 700
    live = session.all_sessions()
    assert PHONE not in live
    assert phone2 in live
