"""
SQLite database layer — schema creation, all helper functions.

Tables:
  bookings           — customer slot reservations
  blocked_slots      — owner-blocked time ranges
  sessions           — customer conversation state (SQLite-backed)
  turfs              — turf configuration
  messages           — full inbound/outbound message log
  conversation_failures — parser/AI failure log
  events             — business event log
  processed_messages — idempotency lock (worker_id ownership)
  audit_logs         — who did what, when, on which entity
  daily_metrics      — daily operational KPIs (event-driven)

Booking statuses:
  pending_payment   → slot held, waiting for screenshot
  pending_owner     → screenshot received, waiting for owner confirm
  confirmed         → owner confirmed payment
  cancelled         → cancelled by customer / owner / timeout

Booking IDs start at BK101 and auto-increment.
"""
import sqlite3
import logging
import uuid
from datetime import datetime, date, timedelta
from typing import Optional
from contextlib import contextmanager
import json
import os

logger = logging.getLogger(__name__)

def _db_path() -> str:
    """Get DB path — respects monkeypatched DB_PATH in tests."""
    return DB_PATH

DB_PATH = os.environ.get("DB_PATH", "turf.db")


# ── Connection ─────────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    conn = sqlite3.connect(_db_path())
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
                                                          -- pending_owner   |
                                                          -- confirmed       |
                                                          -- cancelled
                payment_utr    TEXT    DEFAULT '',
                screenshot_url TEXT    DEFAULT '',
                reminder_sent  INTEGER NOT NULL DEFAULT 0,
                screenshot_at  TEXT    DEFAULT NULL,      -- UTC when screenshot received
                created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                turf_id        INTEGER NOT NULL DEFAULT 1,
                reserved_until TEXT
            );

            CREATE TABLE IF NOT EXISTS blocked_slots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    NOT NULL,   -- YYYY-MM-DD
                start_time   TEXT    NOT NULL,   -- HH:MM  (24h)
                duration_hrs REAL    NOT NULL DEFAULT 1,
                reason       TEXT    DEFAULT 'blocked',
                created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                phone TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                date TEXT,
                start_time TEXT,
                duration REAL,
                name TEXT,
                booking_ref TEXT,
                proposed_date TEXT,
                proposed_time TEXT,
                proposed_duration REAL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS turfs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                upi TEXT NOT NULL,
                owner_numbers TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                role TEXT NOT NULL,       -- 'customer' or 'bot'
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversation_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                state TEXT NOT NULL,
                bot_reply TEXT NOT NULL,
                intent TEXT,
                failure_type TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                event TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
                message_sid TEXT PRIMARY KEY,
                status      TEXT NOT NULL,   -- 'processing', 'done', 'failed'
                worker_id   TEXT NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                actor       TEXT NOT NULL,   -- phone or 'system'
                action      TEXT NOT NULL,   -- e.g. 'cancel_booking'
                entity_type TEXT,            -- 'booking', 'session', 'payment', 'slot'
                entity_id   TEXT,            -- e.g. 'BK101' or phone
                details     TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_metrics (
                date                 TEXT PRIMARY KEY,  -- YYYY-MM-DD
                messages             INTEGER DEFAULT 0,
                bookings             INTEGER DEFAULT 0,
                conversion           REAL    DEFAULT 0.0,
                fallback             INTEGER DEFAULT 0,
                avg_response_ms      REAL    DEFAULT 0.0,
                error_rate           REAL    DEFAULT 0.0,
                owner_response_time  REAL    DEFAULT 0.0,
                payment_timeout_rate REAL    DEFAULT 0.0,
                updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
            );
        """)

        # ── Migrations for existing databases ───────────────────────────────
        cursor = conn.execute("PRAGMA table_info(bookings)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "turf_id" not in columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN turf_id INTEGER DEFAULT 1")
        if "reserved_until" not in columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN reserved_until TEXT")
        if "screenshot_at" not in columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN screenshot_at TEXT DEFAULT NULL")

        cursor = conn.execute("PRAGMA table_info(sessions)")
        s_columns = [row["name"] for row in cursor.fetchall()]
        if s_columns:
            if "proposed_date" not in s_columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN proposed_date TEXT")
            if "proposed_time" not in s_columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN proposed_time TEXT")
            if "proposed_duration" not in s_columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN proposed_duration REAL")

    logger.info("DB initialised at %s", _db_path())


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
        # Check active bookings (pending_payment, pending_owner, confirmed)
        rows = conn.execute(
            """
            SELECT start_time, duration_hrs, status, reserved_until FROM bookings
            WHERE date = ? AND status != 'cancelled'
            """,
            (slot_date,),
        ).fetchall()
        for row in rows:
            if row["status"] == "pending_payment":
                res_until_str = row["reserved_until"]
                if res_until_str:
                    try:
                        res_until = datetime.strptime(res_until_str, "%Y-%m-%d %H:%M:%S")
                        if datetime.utcnow() > res_until:
                            continue  # lock expired, ignore booking
                    except Exception:
                        pass
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

    now_utc = datetime.utcnow()
    created_at_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    res_until_str = (now_utc + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        ref = _next_booking_ref(conn)
        conn.execute(
            """
            INSERT INTO bookings (booking_ref, phone, name, date, start_time, duration_hrs, created_at, reserved_until, turf_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (ref, phone, name, slot_date, start_time, duration_hrs, created_at_str, res_until_str),
        )
    logger.info("Booking created: %s for %s on %s %s (%.1fh)", ref, phone, slot_date, start_time, duration_hrs)

    log_event(phone, "payment_pending", {
        "booking_ref": ref,
        "name": name,
        "date": slot_date,
        "start_time": start_time,
        "duration_hrs": duration_hrs,
        "reserved_until": res_until_str
    })

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
            WHERE booking_ref = ? AND status IN ('pending_payment', 'pending_owner')
            """,
            (utr, screenshot_url, ref.upper()),
        )
        updated = cur.rowcount > 0
    if updated:
        logger.info("Booking confirmed: %s UTR=%s", ref, utr)
        bk = get_booking_by_ref(ref)
        if bk:
            log_event(bk["phone"], "booking_confirmed", {
                "booking_ref": ref,
                "name": bk["name"],
                "date": bk["date"],
                "start_time": bk["start_time"],
                "duration_hrs": bk["duration_hrs"]
            })
            log_audit(
                actor="owner",
                action="confirm_booking",
                entity_type="booking",
                entity_id=ref,
                details=f"Confirmed for {bk['name']} on {bk['date']} {bk['start_time']}"
            )
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
    log_event(row["phone"], "booking_cancelled", {
        "booking_ref": ref,
        "name": row["name"],
        "date": row["date"],
        "start_time": row["start_time"],
        "duration_hrs": row["duration_hrs"]
    })
    return row


def mark_screenshot_received(ref: str, screenshot_url: str, utr: str = "") -> bool:
    """
    Update booking status to pending_owner after screenshot received.
    Records screenshot_at timestamp for owner SLA tracking.
    Returns True if updated.
    """
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE bookings
            SET screenshot_url = ?, payment_utr = ?, status = 'pending_owner',
                screenshot_at = ?
            WHERE booking_ref = ? AND status = 'pending_payment'
            """,
            (screenshot_url, utr, now_utc, ref.upper()),
        )
        updated = cur.rowcount > 0
    if updated:
        logger.info("Screenshot received for %s → pending_owner", ref)
    return updated


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


def unlock_slot(slot_date: str, start_time: str) -> bool:
    """
    Remove all blocked_slots entries for the given date+time.
    Also cancels any pending_payment bookings on the same slot.
    Returns True if anything was removed.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM blocked_slots WHERE date = ? AND start_time = ?",
            (slot_date, start_time),
        )
        removed_blocks = cur.rowcount
        cur2 = conn.execute(
            """
            UPDATE bookings SET status = 'cancelled'
            WHERE date = ? AND start_time = ? AND status = 'pending_payment'
            """,
            (slot_date, start_time),
        )
        removed_pending = cur2.rowcount
    logger.info("Slot unlocked: %s %s (blocks=%d, pending=%d)", slot_date, start_time, removed_blocks, removed_pending)
    return (removed_blocks + removed_pending) > 0


def get_upcoming_bookings(within_minutes: int = 35) -> list:
    """
    Return confirmed bookings whose slot starts within `within_minutes` from now
    and where reminder_sent = 0.
    """
    now = datetime.now()
    cutoff = now + timedelta(minutes=within_minutes)

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
    Does NOT cancel 'pending_owner' — those go through the 24h escalation flow.
    Returns list of cancelled rows so callers can notify customers.
    """
    now_utc = datetime.utcnow()
    now_utc_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    cutoff_utc_str = (now_utc - timedelta(minutes=older_than_minutes)).strftime("%Y-%m-%d %H:%M:%S")

    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT booking_ref, phone, name, date, start_time, duration_hrs FROM bookings
            WHERE status = 'pending_payment'
              AND (
                (reserved_until IS NOT NULL AND reserved_until <= ?)
                OR (reserved_until IS NULL AND created_at <= ?)
              )
            """,
            (now_utc_str, cutoff_utc_str),
        ).fetchall()
        if rows:
            refs = [r["booking_ref"] for r in rows]
            placeholders = ",".join("?" * len(refs))
            conn.execute(
                f"UPDATE bookings SET status = 'cancelled' WHERE booking_ref IN ({placeholders})",
                refs,
            )
            for r in rows:
                log_event(r["phone"], "booking_cancelled", {
                    "booking_ref": r["booking_ref"],
                    "name": r["name"],
                    "date": r["date"],
                    "start_time": r["start_time"],
                    "duration_hrs": r["duration_hrs"],
                    "reason": "payment_timeout"
                })
    if rows:
        logger.info("Expired pending_payment bookings: %s", [r["booking_ref"] for r in rows])
    return list(rows)


def get_pending_owner_bookings() -> list:
    """
    Return all bookings in 'pending_owner' status for escalation checks.
    Each row includes screenshot_at to compute elapsed time.
    """
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM bookings
            WHERE status = 'pending_owner' AND screenshot_at IS NOT NULL
            ORDER BY screenshot_at
            """
        ).fetchall()


def log_event(phone: str, event: str, payload: dict) -> None:
    """Log business events to the events table for analytics."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO events (phone, event, payload) VALUES (?, ?, ?)",
            (phone, event, json.dumps(payload)),
        )


def log_message(phone: str, role: str, text: str) -> None:
    """Log inbound/outbound messages for conversation history."""
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO messages (phone, role, text) VALUES (?, ?, ?)",
                (phone, role, text[:2000]),  # cap at 2000 chars
            )
    except Exception as e:
        logger.warning("Failed to log message: %s", e)


def log_audit(
    actor: str,
    action: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    """Write an entry to audit_logs. Never raises — safe to call anywhere."""
    try:
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (actor, action, entity_type, entity_id, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (actor, action, entity_type, entity_id, details),
            )
    except Exception as e:
        logger.warning("Failed to write audit log: %s", e)


def get_free_slots(slot_date: str, open_hour: int = 6, close_hour: int = 23) -> list:
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


# ── Idempotency ────────────────────────────────────────────────────────────────

def acquire_message_lock(message_sid: str, worker_id: str) -> bool:
    """
    Attempt to acquire an exclusive lock for processing this message.
    Uses PRIMARY KEY conflict to ensure only ONE worker processes each message.

    Returns True if lock acquired (this worker should proceed).
    Returns False if already processing/done/failed (another worker owns it).
    """
    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO processed_messages (message_sid, status, worker_id) VALUES (?, 'processing', ?)",
                (message_sid, worker_id),
            )
        return True
    except sqlite3.IntegrityError:
        # PRIMARY KEY conflict — another worker already inserted this SID
        return False


def finish_message_processing(message_sid: str, status: str) -> None:
    """Update message processing status to 'done' or 'failed'."""
    try:
        with get_conn() as conn:
            conn.execute(
                "UPDATE processed_messages SET status = ? WHERE message_sid = ?",
                (status, message_sid),
            )
    except Exception as e:
        logger.warning("Failed to update message processing status: %s", e)


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_fast() -> None:
    """
    Lightweight cleanup — runs on every webhook request.
    Target: <10ms. Only prunes expired sessions and slot locks.
    """
    import time
    session_cutoff = time.time() - 600  # 10 minutes
    now_utc_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE updated_at < ?", (session_cutoff,))
    except Exception as e:
        logger.warning("cleanup_fast error: %s", e)


def cleanup_full() -> None:
    """
    Heavy cleanup — runs daily in the scheduler.
    Archives old records: idempotency logs (>7 days), failures (>30 days),
    audit logs (>90 days), events (>90 days).
    """
    now_utc = datetime.utcnow()
    idempotency_cutoff = (now_utc - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    failures_cutoff    = (now_utc - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    audit_cutoff       = (now_utc - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_conn() as conn:
            conn.execute(
                "DELETE FROM processed_messages WHERE created_at < ?", (idempotency_cutoff,)
            )
            conn.execute(
                "DELETE FROM conversation_failures WHERE created_at < ? AND resolved = 1",
                (failures_cutoff,)
            )
            conn.execute(
                "DELETE FROM audit_logs WHERE created_at < ?", (audit_cutoff,)
            )
            conn.execute(
                "DELETE FROM events WHERE created_at < ?", (audit_cutoff,)
            )
        logger.info("cleanup_full completed")
    except Exception as e:
        logger.error("cleanup_full error: %s", e)


# ── Analytics ──────────────────────────────────────────────────────────────────

def update_daily_metrics() -> None:
    """
    Compute and upsert today's operational KPIs into daily_metrics.
    Runs once daily from the scheduler — cheap, event-driven.
    """
    today = date.today().strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            # Total inbound messages today
            msg_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE role = 'customer' AND date(created_at) = ?",
                (today,)
            ).fetchone()
            messages = msg_row["cnt"] if msg_row else 0

            # Bookings created today
            bk_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM bookings WHERE date(created_at) = ?",
                (today,)
            ).fetchone()
            bookings_today = bk_row["cnt"] if bk_row else 0

            # Conversion = confirmed bookings / total bookings created today
            confirmed_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM bookings WHERE date(created_at) = ? AND status = 'confirmed'",
                (today,)
            ).fetchone()
            confirmed = confirmed_row["cnt"] if confirmed_row else 0
            conversion = round(confirmed / bookings_today, 2) if bookings_today > 0 else 0.0

            # Payment timeout rate
            timeout_row = conn.execute(
                """
                SELECT COUNT(*) as cnt FROM events
                WHERE date(created_at) = ? AND event = 'booking_cancelled'
                  AND payload LIKE '%payment_timeout%'
                """,
                (today,)
            ).fetchone()
            timeouts = timeout_row["cnt"] if timeout_row else 0
            payment_timeout_rate = round(timeouts / bookings_today, 2) if bookings_today > 0 else 0.0

            # Groq fallback usage (logged as 'groq_fallback' events)
            fallback_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM events WHERE date(created_at) = ? AND event = 'groq_fallback'",
                (today,)
            ).fetchone()
            fallback = fallback_row["cnt"] if fallback_row else 0

            # Owner avg response time (minutes from screenshot_at to confirmed)
            resp_row = conn.execute(
                """
                SELECT AVG((julianday(updated_at) - julianday(screenshot_at)) * 24 * 60) as avg_min
                FROM (
                    SELECT b.screenshot_at,
                           (SELECT MAX(a.created_at) FROM audit_logs a
                            WHERE a.entity_id = b.booking_ref AND a.action = 'confirm_booking') as updated_at
                    FROM bookings b
                    WHERE b.status = 'confirmed' AND date(b.screenshot_at) = ?
                      AND b.screenshot_at IS NOT NULL
                )
                """,
                (today,)
            ).fetchone()
            owner_response_time = round(resp_row["avg_min"] or 0.0, 1)

            conn.execute(
                """
                INSERT INTO daily_metrics
                    (date, messages, bookings, conversion, fallback,
                     payment_timeout_rate, owner_response_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date) DO UPDATE SET
                    messages             = excluded.messages,
                    bookings             = excluded.bookings,
                    conversion           = excluded.conversion,
                    fallback             = excluded.fallback,
                    payment_timeout_rate = excluded.payment_timeout_rate,
                    owner_response_time  = excluded.owner_response_time,
                    updated_at           = excluded.updated_at
                """,
                (today, messages, bookings_today, conversion, fallback,
                 payment_timeout_rate, owner_response_time)
            )
        logger.info(
            "Daily metrics updated: messages=%d bookings=%d conversion=%.0f%% fallback=%d timeout_rate=%.0f%% owner_resp=%.1fmin",
            messages, bookings_today, conversion * 100, fallback, payment_timeout_rate * 100, owner_response_time
        )
    except Exception as e:
        logger.error("update_daily_metrics error: %s", e)
