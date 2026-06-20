"""
Tests for all production hardening features:
  - Idempotency lock (worker_id, processing/done/failed states)
  - pending_owner booking status and escalation
  - Admin commands (force cancel, unlock slot, reset session, resend payment)
  - safe_notify_owner graceful failure
  - /health and /ready endpoints
  - cleanup_fast and cleanup_full
  - Audit logging
  - Daily metrics snapshot
"""
import pytest
import time
from unittest.mock import patch, MagicMock, call
from datetime import date, timedelta, datetime

import app.db as db
import app.session as sess
from app.handlers.owner import handle_owner

PHONE = "whatsapp:+919876543210"
OWNER = "whatsapp:+919000000000"
TOMORROW = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
TODAY = date.today().strftime("%Y-%m-%d")


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_prod.db"))
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
    mock_settings.groq_api_key = ""
    monkeypatch.setattr("app.config.get_settings", lambda: mock_settings)
    monkeypatch.setattr("app.handlers.owner.get_settings", lambda: mock_settings)
    monkeypatch.setattr("app.handlers.customer.get_settings", lambda: mock_settings)
    yield
    sess._sessions.clear()


# ── Idempotency ────────────────────────────────────────────────────────────────

def test_acquire_message_lock_succeeds_first_time():
    """First worker gets the lock."""
    acquired = db.acquire_message_lock("sid_001", "worker_A")
    assert acquired is True


def test_acquire_message_lock_fails_second_time():
    """Second worker cannot acquire the same lock."""
    db.acquire_message_lock("sid_002", "worker_A")
    acquired = db.acquire_message_lock("sid_002", "worker_B")
    assert acquired is False


def test_finish_message_processing_sets_done():
    db.acquire_message_lock("sid_003", "worker_A")
    db.finish_message_processing("sid_003", "done")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM processed_messages WHERE message_sid = ?", ("sid_003",)
        ).fetchone()
    assert row["status"] == "done"


def test_finish_message_processing_sets_failed():
    db.acquire_message_lock("sid_004", "worker_A")
    db.finish_message_processing("sid_004", "failed")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM processed_messages WHERE message_sid = ?", ("sid_004",)
        ).fetchone()
    assert row["status"] == "failed"


def test_two_concurrent_workers_only_one_processes():
    """Simulates concurrent duplicate POST from Twilio retry."""
    sid = "sid_concurrent"
    results = []
    results.append(db.acquire_message_lock(sid, "worker_1"))
    results.append(db.acquire_message_lock(sid, "worker_2"))
    # Exactly one True, one False
    assert results.count(True) == 1
    assert results.count(False) == 1


# ── pending_owner status ───────────────────────────────────────────────────────

def test_mark_screenshot_received_sets_pending_owner():
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "08:00", 1.0)
    updated = db.mark_screenshot_received(ref, "https://example.com/ss.jpg", "123456789012")
    assert updated is True
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "pending_owner"
    assert bk["screenshot_url"] == "https://example.com/ss.jpg"
    assert bk["screenshot_at"] is not None


def test_pending_owner_not_expired_by_payment_timeout():
    """pending_owner bookings must NOT be cancelled by expire_pending_bookings."""
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "10:00", 1.0)
    db.mark_screenshot_received(ref, "https://example.com/ss.jpg")
    # Force reserved_until to the past
    with db.get_conn() as conn:
        past = (datetime.utcnow() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE bookings SET reserved_until = ? WHERE booking_ref = ?", (past, ref))
    expired = db.expire_pending_bookings(older_than_minutes=1)
    refs = [r["booking_ref"] for r in expired]
    assert ref not in refs
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "pending_owner"


def test_confirm_booking_from_pending_owner():
    """Owner can confirm from pending_owner state."""
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "12:00", 1.0)
    db.mark_screenshot_received(ref, "https://example.com/ss.jpg")
    ok = db.confirm_booking(ref)
    assert ok is True
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "confirmed"


def test_get_pending_owner_bookings():
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "14:00", 1.0)
    db.mark_screenshot_received(ref, "https://example.com/ss.jpg")
    rows = db.get_pending_owner_bookings()
    assert any(r["booking_ref"] == ref for r in rows)


# ── Audit Logs ─────────────────────────────────────────────────────────────────

def test_log_audit_writes_entry():
    db.log_audit(
        actor=OWNER,
        action="test_action",
        entity_type="booking",
        entity_id="BK999",
        details="Test details"
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM audit_logs WHERE entity_id = 'BK999'"
        ).fetchone()
    assert row is not None
    assert row["action"] == "test_action"
    assert row["entity_type"] == "booking"
    assert row["actor"] == OWNER


def test_log_audit_never_raises_on_bad_input():
    """Audit logging must be fire-and-forget — never raises."""
    db.log_audit(actor=None, action="test", entity_type=None, entity_id=None, details=None)


def test_confirm_booking_writes_audit_log():
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "16:00", 1.0)
    db.confirm_booking(ref)
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM audit_logs WHERE entity_id = ? AND action = 'confirm_booking'",
            (ref,)
        ).fetchone()
    assert row is not None


# ── Admin Commands ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_force_cancel():
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "18:00", 1.0)
    with patch("app.handlers.owner.send_whatsapp_message") as mock_send:
        await handle_owner(OWNER, f"/admin force cancel {ref}")
        # Owner gets confirmation, customer gets notification
        assert mock_send.call_count == 2
        owner_msg = mock_send.call_args_list[0][0][1]
        assert "cancelled" in owner_msg.lower() or "force" in owner_msg.lower() or ref in owner_msg
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "cancelled"


@pytest.mark.asyncio
async def test_admin_unlock_slot():
    db.block_slot(TOMORROW, "20:00", 1.0)
    assert db.check_slot_available(TOMORROW, "20:00", 1.0) is False
    with patch("app.handlers.owner.send_whatsapp_message") as mock_send:
        await handle_owner(OWNER, f"/admin unlock slot {TOMORROW} 20:00")
        body = mock_send.call_args[0][1]
        assert "unlocked" in body.lower() or "🔓" in body
    assert db.check_slot_available(TOMORROW, "20:00", 1.0) is True


@pytest.mark.asyncio
async def test_admin_reset_session():
    sess.set_state(PHONE, "awaiting_time", {"date": TOMORROW})
    assert sess.get_state(PHONE) == "awaiting_time"
    with patch("app.handlers.owner.send_whatsapp_message"):
        await handle_owner(OWNER, f"/admin reset session {PHONE}")
    assert sess.get_state(PHONE) == "idle"


@pytest.mark.asyncio
async def test_admin_show_active_users():
    sess.set_state(PHONE, "awaiting_time", {"date": TOMORROW})
    with patch("app.handlers.owner.send_whatsapp_message") as mock_send:
        await handle_owner(OWNER, "/admin show active users")
        body = mock_send.call_args[0][1]
        assert "Active" in body or "Session" in body


@pytest.mark.asyncio
async def test_admin_resend_payment():
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "08:00", 1.0)
    with patch("app.handlers.owner.send_whatsapp_message") as mock_send:
        await handle_owner(OWNER, f"/admin resend payment {ref}")
        # Should send to customer + owner confirmation
        assert mock_send.call_count == 2
        calls_to = [c[0][0] for c in mock_send.call_args_list]
        assert PHONE in calls_to


@pytest.mark.asyncio
async def test_admin_unknown_command_shows_help():
    with patch("app.handlers.owner.send_whatsapp_message") as mock_send:
        await handle_owner(OWNER, "/admin gibberish command")
        body = mock_send.call_args[0][1]
        assert "Admin Commands" in body or "/admin" in body


# ── safe_notify_owner ──────────────────────────────────────────────────────────

def test_safe_notify_owner_does_not_raise_on_twilio_failure():
    """Twilio failure must be swallowed — never raises."""
    from app.twilio_client import safe_notify_owner
    with patch("app.twilio_client.send_whatsapp_message", side_effect=Exception("Twilio down")):
        # Should not raise
        safe_notify_owner([OWNER], "Test alert")


def test_safe_notify_owner_sends_to_all_phones():
    from app.twilio_client import safe_notify_owner
    owners = ["whatsapp:+910000000001", "whatsapp:+910000000002"]
    with patch("app.twilio_client.send_whatsapp_message") as mock_send:
        safe_notify_owner(owners, "Test message")
        assert mock_send.call_count == 2


# ── Cleanup ────────────────────────────────────────────────────────────────────

def test_cleanup_fast_removes_expired_sessions():
    sess.set_state(PHONE, "awaiting_time", {"date": TOMORROW})
    # Manually expire session
    cutoff = time.time() - 700  # 700s > 600s TTL
    with db.get_conn() as conn:
        conn.execute("UPDATE sessions SET updated_at = ? WHERE phone = ?", (cutoff, PHONE))
    db.cleanup_fast()
    assert sess.get_state(PHONE) == "idle"


def test_cleanup_full_removes_old_idempotency_records():
    # Insert an old processed_message record
    old_date = "2020-01-01 00:00:00"
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO processed_messages (message_sid, status, worker_id, created_at) VALUES (?, ?, ?, ?)",
            ("old_sid_001", "done", "w1", old_date)
        )
    db.cleanup_full()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM processed_messages WHERE message_sid = 'old_sid_001'"
        ).fetchone()
    assert row is None


# ── Unlock Slot ────────────────────────────────────────────────────────────────

def test_unlock_slot_removes_blocked_slot():
    db.block_slot(TOMORROW, "22:00", 1.0)
    removed = db.unlock_slot(TOMORROW, "22:00")
    assert removed is True
    assert db.check_slot_available(TOMORROW, "22:00", 1.0) is True


def test_unlock_slot_cancels_pending_payment():
    ref = db.create_booking(PHONE, "Ravi", TOMORROW, "09:00", 1.0)
    # Force past reserved_until so it's not auto-expired
    with db.get_conn() as conn:
        future = (datetime.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE bookings SET reserved_until = ? WHERE booking_ref = ?", (future, ref))
    # slot check shows occupied
    assert db.check_slot_available(TOMORROW, "09:00", 1.0) is False
    # Unlock should cancel the pending booking
    db.unlock_slot(TOMORROW, "09:00")
    bk = db.get_booking_by_ref(ref)
    assert bk["status"] == "cancelled"


# ── Daily Metrics ──────────────────────────────────────────────────────────────

def test_update_daily_metrics_runs_without_error():
    """Just verifies it doesn't raise — DB may have no data yet."""
    db.create_booking(PHONE, "Ravi", TOMORROW, "07:00", 1.0)
    db.update_daily_metrics()
    today = date.today().strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM daily_metrics WHERE date = ?", (today,)
        ).fetchone()
    assert row is not None


def test_log_message_writes_to_db():
    db.log_message(PHONE, "customer", "Hello there")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE phone = ? AND role = 'customer'", (PHONE,)
        ).fetchone()
    assert row is not None
    assert row["text"] == "Hello there"
