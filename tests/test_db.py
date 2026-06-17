"""
Unit tests for the SQLite DB layer.
Uses a temp file so tests are fully isolated.
"""
import os
import tempfile
import pytest
import app.db as db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point DB_PATH to a fresh temp file for every test."""
    temp_db = str(tmp_path / "test_turf.db")
    monkeypatch.setattr(db, "DB_PATH", temp_db)
    db.init_db()
    yield
    if os.path.exists(temp_db):
        os.remove(temp_db)


# ── Booking creation ───────────────────────────────────────────────────────────

def test_create_booking_returns_ref():
    ref = db.create_booking("+919999999999", "Ravi", "2025-08-01", "08:00", 1.0)
    assert ref is not None
    assert ref.startswith("BK")


def test_booking_refs_increment():
    ref1 = db.create_booking("+911111111111", "A", "2025-08-01", "06:00", 1.0)
    ref2 = db.create_booking("+912222222222", "B", "2025-08-01", "08:00", 1.0)
    num1 = int(ref1[2:])
    num2 = int(ref2[2:])
    assert num2 == num1 + 1


def test_first_booking_is_bk101():
    ref = db.create_booking("+910000000000", "X", "2025-08-01", "10:00", 1.0)
    assert ref == "BK101"


# ── Overlap detection ──────────────────────────────────────────────────────────

def test_exact_same_slot_blocked():
    db.create_booking("+911111111111", "A", "2025-08-02", "08:00", 1.0)
    result = db.create_booking("+912222222222", "B", "2025-08-02", "08:00", 1.0)
    assert result is None


def test_overlap_new_starts_inside_existing():
    # Existing: 08:00–09:00 | New: 08:30–09:30
    db.create_booking("+911111111111", "A", "2025-08-03", "08:00", 1.0)
    result = db.create_booking("+912222222222", "B", "2025-08-03", "08:30", 1.0)
    assert result is None


def test_overlap_new_ends_inside_existing():
    # Existing: 09:00–10:00 | New: 08:30–09:30
    db.create_booking("+911111111111", "A", "2025-08-04", "09:00", 1.0)
    result = db.create_booking("+912222222222", "B", "2025-08-04", "08:30", 1.0)
    assert result is None


def test_overlap_new_wraps_existing():
    # Existing: 09:00–10:00 | New: 08:00–11:00
    db.create_booking("+911111111111", "A", "2025-08-05", "09:00", 1.0)
    result = db.create_booking("+912222222222", "B", "2025-08-05", "08:00", 3.0)
    assert result is None


def test_adjacent_slots_do_not_overlap():
    # Existing: 08:00–09:00 | New: 09:00–10:00  — should be fine
    db.create_booking("+911111111111", "A", "2025-08-06", "08:00", 1.0)
    result = db.create_booking("+912222222222", "B", "2025-08-06", "09:00", 1.0)
    assert result is not None


def test_different_dates_do_not_conflict():
    db.create_booking("+911111111111", "A", "2025-08-07", "08:00", 1.0)
    result = db.create_booking("+912222222222", "B", "2025-08-08", "08:00", 1.0)
    assert result is not None


def test_cancelled_slot_frees_up():
    ref = db.create_booking("+911111111111", "A", "2025-08-09", "08:00", 1.0)
    db.cancel_booking(ref)
    result = db.create_booking("+912222222222", "B", "2025-08-09", "08:00", 1.0)
    assert result is not None


# ── Blocked slots ──────────────────────────────────────────────────────────────

def test_blocked_slot_prevents_booking():
    db.block_slot("2025-08-10", "10:00", 2.0, "maintenance")
    result = db.create_booking("+912222222222", "B", "2025-08-10", "10:00", 1.0)
    assert result is None


def test_block_slot_fails_if_already_booked():
    db.create_booking("+911111111111", "A", "2025-08-11", "07:00", 1.0)
    ok = db.block_slot("2025-08-11", "07:00", 1.0)
    assert ok is False


# ── Confirm + status ───────────────────────────────────────────────────────────

def test_confirm_booking():
    ref = db.create_booking("+919999999999", "Ravi", "2025-08-12", "08:00", 1.0)
    ok = db.confirm_booking(ref, utr="123456789012")
    assert ok is True
    row = db.get_booking_by_ref(ref)
    assert row["status"] == "confirmed"
    assert row["payment_utr"] == "123456789012"


def test_confirm_already_confirmed_is_idempotent():
    ref = db.create_booking("+919999999999", "Ravi", "2025-08-13", "08:00", 1.0)
    db.confirm_booking(ref)
    ok = db.confirm_booking(ref)   # second call
    assert ok is False             # rowcount=0 since status is no longer pending


# ── Get by date ────────────────────────────────────────────────────────────────

def test_get_bookings_by_date():
    db.create_booking("+911111111111", "A", "2025-08-14", "08:00", 1.0)
    db.create_booking("+912222222222", "B", "2025-08-14", "10:00", 1.0)
    rows = db.get_bookings_by_date("2025-08-14")
    assert len(rows) == 2
    assert rows[0]["start_time"] == "08:00"


def test_cancelled_excluded_from_date_list():
    ref = db.create_booking("+911111111111", "A", "2025-08-15", "08:00", 1.0)
    db.cancel_booking(ref)
    rows = db.get_bookings_by_date("2025-08-15")
    assert len(rows) == 0


# ── Free slots ─────────────────────────────────────────────────────────────────

def test_free_slots_excludes_booked():
    db.create_booking("+911111111111", "A", "2025-08-16", "08:00", 1.0)
    free = db.get_free_slots("2025-08-16", open_hour=6, close_hour=11)
    assert "08:00" not in free
    assert "06:00" in free
    assert "09:00" in free
