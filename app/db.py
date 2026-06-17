"""
SQLite database layer — schema creation, all helper functions.

Tables:
  bookings      — customer slot reservations
  blocked_slots — owner-blocked time ranges

Booking IDs start at BK101 and auto-increment.
"""
import sqlite3
import logging
from datetime import datetime, date, time, timedelta
from typing import Optional
from contextlib import contextmanager

import os

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "turf.db")


# ── Connection ─────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # access columns by name
    conn.execute("PRAGMA journal_mode=WAL") # safe for concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema ─────────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist. Called at app startup."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_ref    TEXT    UNIQUE NOT NULL,   -- BK101, BK102 …
                phone          TEXT    NOT NULL,
                name           TEXT    NOT NULL DEFAULT '',
                date           TEXT    NOT NULL,          -- YYYY-MM-DD
                start_time     TEXT    NOT NULL,          -- HH:MM  (24h)
                duration_hrs   REAL    NOT NULL DEFAULT 1,
                status         TEXT    NOT NULL DEFAULT 'pending_payment',
                                                          -- pending_payment |
                                                          -- confirmed |
                                                          -- cancelled
                payment_utr    TEXT    DEFAULT '',
                screenshot_url TEXT    DEFAULT '',
                reminder_sent  INTEGER NOT NULL DEFAULT 0,
                created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS blocked_slots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    NOT NULL,   -- YYYY-MM-DD
                start_time   TEXT    NOT NULL,   -- HH:MM  (24h)
                duration_hrs REAL    NOT NULL DEFAULT 1,
                reason       TEXT    DEFAULT 'blocked',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)
    logger.info("DB initialised at %s", DB_PATH)


# ── Booking reference helpers ──────────────────────────────────────────────────

def _next_booking_ref(conn: sqlite3.Connection) -> str:
    """Generate next BKxxx reference. Starts at BK101."""
    row = conn.execute(
        "SELECT booking_ref FROM bookings ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "BK101"
    last_num = int(row["booking_ref"][2:])  # strip "BK"
    return f"BK{last_num + 1}"


# ── Overlap detection ──────────────────────────────────────────────────────────

def _to_minutes(t: str) -> int:
    """Convert 'HH:MM' string to minutes since midnight."""
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _slots_overlap(
    start_a: str, dur_a: float,
    start_b: str, dur_b: float,
) -> bool:
    """
    True if two slots overlap.
    Overlap condition: new_start < existing_end AND new_end > existing_start
    """
    a_start = _to_minutes(start_a)
    a_end   = a_start + int(dur_a * 60)
    b_start = _to_minutes(start_b)
    b_end   = b_start + int(dur_b * 60)
    return a_start < b_end and a_end > b_start


# ── Public helpers ─────────────────────────────────────────────────────────────

def check_slot_available(slot_date: str, start_time: str, duration_hrs: float) -> bool:
    """
    Return True if the slot is free (not booked and not blocked).

    Args:
        slot_date:    'YYYY-MM-DD'
        start_time:   'HH:MM'
        duration_hrs: e.g. 1.0 or 2.0
    """
    with get_conn() as conn:
        # Check active bookings
        rows = conn.execute(
            """
            SELECT start_time, duration_hrs FROM bookings
            WHERE date = ? AND status != 'cancelled'
            """,
            (slot_date,),
        ).fetchall()
        for row in rows:
            if _slots_overlap(start_time, duration_hrs, row["start_time"], row["duration_hrs"]):
                return False

        # Check blocked slots
        rows = conn.execute(
            "SELECT start_time, duration_hrs FROM blocked_slots WHERE date = ?",
            (slot_date,),
        ).fetchall()
        for row in rows:
            if _slots_overlap(start_time, duration_hrs, row["start_time"], row["duration_hrs"]):
                return False

    return True


def create_booking(
    phone: str,
    name: str,
    slot_date: str,
    start_time: str,
    duration_hrs: float,
) -> Optional[str]:
    """
    Insert a new booking with status='pending_payment'.
    Returns the booking_ref (e.g. 'BK103') or None if slot is taken.
    """
    if not check_slot_available(slot_date, start_time, duration_hrs):
        return None

    with get_conn() as conn:
        ref = _next_booking_ref(conn)
        conn.execute(
            """
            INSERT INTO bookings (booking_ref, phone, name, date, start_time, duration_hrs)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ref, phone, name, slot_date, start_time, duration_hrs),
        )
    logger.info("Booking created: %s for %s on %s %s (%.1fh)", ref, phone, slot_date, start_time, duration_hrs)
    return ref


def get_booking_by_ref(ref: str) -> Optional[sqlite3.Row]:
    """Fetch a single booking by its BKxxx reference."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE booking_ref = ?", (ref.upper(),)
        ).fetchone()


def get_bookings_by_date(slot_date: str) -> list:
    """All non-cancelled bookings for a date, ordered by start_time."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM bookings
            WHERE date = ? AND status != 'cancelled'
            ORDER BY start_time
            """,
            (slot_date,),
        ).fetchall()


def get_bookings_by_phone(phone: str) -> list:
    """All bookings for a customer phone, most recent first."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM bookings
            WHERE phone = ? AND status != 'cancelled'
            ORDER BY date DESC, start_time DESC
            """,
            (phone,),
        ).fetchall()


def confirm_booking(ref: str, utr: str = "", screenshot_url: str = "") -> bool:
    """Set booking status to 'confirmed'. Returns True if updated."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE bookings
            SET status = 'confirmed', payment_utr = ?, screenshot_url = ?
            WHERE booking_ref = ? AND status = 'pending_payment'
            """,
            (utr, screenshot_url, ref.upper()),
        )
        updated = cur.rowcount > 0
    if updated:
        logger.info("Booking confirmed: %s UTR=%s", ref, utr)
    return updated


def cancel_booking(ref: str) -> Optional[sqlite3.Row]:
    """
    Cancel a booking by ref.
    Returns the booking row before cancellation, or None if not found.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE booking_ref = ?", (ref.upper(),)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE booking_ref = ?",
            (ref.upper(),),
        )
    logger.info("Booking cancelled: %s", ref)
    return row


def block_slot(slot_date: str, start_time: str, duration_hrs: float, reason: str = "blocked") -> bool:
    """
    Block a time slot (owner use). Returns False if slot is already booked.
    Proceeds even if another blocked_slot overlaps (owner decision).
    """
    if not check_slot_available(slot_date, start_time, duration_hrs):
        return False
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO blocked_slots (date, start_time, duration_hrs, reason) VALUES (?, ?, ?, ?)",
            (slot_date, start_time, duration_hrs, reason),
        )
    logger.info("Slot blocked: %s %s (%.1fh) reason=%s", slot_date, start_time, duration_hrs, reason)
    return True


def get_upcoming_bookings(within_minutes: int = 35) -> list:
    """
    Return confirmed bookings whose slot starts within `within_minutes` from now
    and where reminder_sent = 0.
    """
    now = datetime.now()
    cutoff = now + timedelta(minutes=within_minutes)

    # Build date+time strings for comparison
    now_date  = now.strftime("%Y-%m-%d")
    now_time  = now.strftime("%H:%M")
    cut_date  = cutoff.strftime("%Y-%m-%d")
    cut_time  = cutoff.strftime("%H:%M")

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM bookings
            WHERE status = 'confirmed'
              AND reminder_sent = 0
              AND (
                    (date = ? AND start_time >= ? AND start_time <= ?)
                 OR (date > ? AND date < ?)
                 OR (date = ? AND start_time <= ?)
              )
            ORDER BY date, start_time
            """,
            (now_date, now_time, cut_time,
             now_date, cut_date,
             cut_date, cut_time),
        ).fetchall()
    return rows


def mark_reminder_sent(booking_ref: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE bookings SET reminder_sent = 1 WHERE booking_ref = ?",
            (booking_ref,),
        )


def expire_pending_bookings(older_than_minutes: int = 5) -> list:
    """
    Cancel bookings that are still 'pending_payment' and older than N minutes.
    Returns list of cancelled refs so callers can notify customers.
    """
    cutoff = (datetime.now() - timedelta(minutes=older_than_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT booking_ref, phone FROM bookings
            WHERE status = 'pending_payment' AND created_at <= ?
            """,
            (cutoff,),
        ).fetchall()
        if rows:
            refs = [r["booking_ref"] for r in rows]
            placeholders = ",".join("?" * len(refs))
            conn.execute(
                f"UPDATE bookings SET status = 'cancelled' WHERE booking_ref IN ({placeholders})",
                refs,
            )
    if rows:
        logger.info("Expired pending bookings: %s", [r["booking_ref"] for r in rows])
    return list(rows)


def get_free_slots(slot_date: str, open_hour: int = 6, close_hour: int = 23) -> list[str]:
    """
    Return list of free start times ('HH:MM') for the given date,
    assuming 1-hour slots.
    """
    free = []
    for hour in range(open_hour, close_hour):
        t = f"{hour:02d}:00"
        if check_slot_available(slot_date, t, 1.0):
            free.append(t)
    return free
