"""
SQLite-backed session store for multi-turn customer booking conversations.

State machine:
  idle
    → awaiting_date      (customer said "book" but no date given)
    → awaiting_time      (have date, need time)
    → awaiting_duration  (have date+time, need duration)
    → awaiting_name      (have slot details, need customer name)
    → awaiting_payment   (sent UPI details, waiting for screenshot)
    → confirm_change     (confirm change/correction of pending booking)
    → done               (booking created, screenshot received)

Sessions expire after SESSION_TTL_SECONDS of inactivity.
"""
import time
import logging
import json
from typing import Any, Optional
from .db import get_conn

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 600  # 10 minutes

# Valid state sequence
STATES = [
    "idle",
    "awaiting_date",
    "awaiting_time",
    "awaiting_duration",
    "awaiting_name",
    "awaiting_payment",
    "confirm_change",
    "done",
]


class SessionStore:
    @staticmethod
    def get(phone: str) -> dict:
        """Fetch session for phone from SQLite, return default if not found or expired."""
        SessionStore.prune()
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE phone = ?", (phone,)).fetchone()
            if row:
                # check expiration
                if (time.time() - row["updated_at"]) > SESSION_TTL_SECONDS:
                    SessionStore.delete(phone)
                    return {"state": "idle", "data": {}, "updated_at": time.time()}
                
                # Map columns back to standard nested structure
                data = {}
                if row["date"] is not None: data["date"] = row["date"]
                if row["start_time"] is not None: data["time"] = row["start_time"]
                if row["duration"] is not None: data["duration"] = row["duration"]
                if row["name"] is not None: data["name"] = row["name"]
                if row["booking_ref"] is not None: data["booking_ref"] = row["booking_ref"]
                if row["proposed_date"] is not None: data["proposed_date"] = row["proposed_date"]
                if row["proposed_time"] is not None: data["proposed_time"] = row["proposed_time"]
                if row["proposed_duration"] is not None: data["proposed_duration"] = row["proposed_duration"]
                
                return {
                    "state": row["state"],
                    "data": data,
                    "updated_at": row["updated_at"]
                }
        return {"state": "idle", "data": {}, "updated_at": time.time()}

    @staticmethod
    def save(phone: str, state: str, data: dict, updated_at: float) -> None:
        """Save session columns to SQLite."""
        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    phone, state, date, start_time, duration, name, booking_ref,
                    proposed_date, proposed_time, proposed_duration, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    phone,
                    state,
                    data.get("date"),
                    data.get("time"),
                    data.get("duration"),
                    data.get("name"),
                    data.get("booking_ref"),
                    data.get("proposed_date"),
                    data.get("proposed_time"),
                    data.get("proposed_duration"),
                    updated_at
                )
            )

    @staticmethod
    def delete(phone: str) -> None:
        """Delete session for a phone number."""
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE phone = ?", (phone,))

    @staticmethod
    def prune() -> None:
        """Delete all expired sessions."""
        cutoff = time.time() - SESSION_TTL_SECONDS
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))

    @staticmethod
    def clear() -> None:
        """Clear all sessions (used in test setup)."""
        with get_conn() as conn:
            conn.execute("DELETE FROM sessions")


# ── Test compatibility proxy ──────────────────────────────────────────────────

class SessionDict(dict):
    """Custom dictionary that writes updates back to the SQLite store."""
    def __init__(self, phone, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.phone = phone

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        SessionStore.save(self.phone, self["state"], self["data"], self["updated_at"])


class _SessionsMock:
    """Mock class pretending to be the dict store for backwards compatibility in tests."""
    def clear(self):
        SessionStore.clear()

    def __getitem__(self, phone):
        s = SessionStore.get(phone)
        return SessionDict(phone, s)

    def __setitem__(self, phone, value):
        SessionStore.save(phone, value["state"], value["data"], value["updated_at"])

    def __delitem__(self, phone):
        SessionStore.delete(phone)

    def items(self):
        SessionStore.prune()
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM sessions").fetchall()
            items_list = []
            for row in rows:
                data = {}
                if row["date"] is not None: data["date"] = row["date"]
                if row["start_time"] is not None: data["time"] = row["start_time"]
                if row["duration"] is not None: data["duration"] = row["duration"]
                if row["name"] is not None: data["name"] = row["name"]
                if row["booking_ref"] is not None: data["booking_ref"] = row["booking_ref"]
                if row["proposed_date"] is not None: data["proposed_date"] = row["proposed_date"]
                if row["proposed_time"] is not None: data["proposed_time"] = row["proposed_time"]
                if row["proposed_duration"] is not None: data["proposed_duration"] = row["proposed_duration"]
                
                s_dict = {
                    "state": row["state"],
                    "data": data,
                    "updated_at": row["updated_at"]
                }
                items_list.append((row["phone"], SessionDict(row["phone"], s_dict)))
            return items_list


_sessions = _SessionsMock()


# ── Wrapper functions (Public API) ─────────────────────────────────────────────

def get_session(phone: str) -> dict:
    return SessionStore.get(phone)


def set_state(phone: str, state: str, data: Optional[dict] = None) -> None:
    if state not in STATES:
        raise ValueError(f"Invalid state: {state!r}. Must be one of {STATES}")
    session = SessionStore.get(phone)
    session["state"] = state
    if data:
        session["data"].update(data)
    SessionStore.save(phone, state, session["data"], time.time())
    logger.debug("Session %s → state=%s data=%s", phone[-4:], state, session["data"])


def update_data(phone: str, **kwargs: Any) -> None:
    session = SessionStore.get(phone)
    session["data"].update(kwargs)
    SessionStore.save(phone, session["state"], session["data"], time.time())


def clear_session(phone: str) -> None:
    SessionStore.delete(phone)
    logger.debug("Session cleared for %s", phone[-4:])


def get_state(phone: str) -> str:
    return SessionStore.get(phone)["state"]


def get_data(phone: str) -> dict:
    return SessionStore.get(phone)["data"]


def all_sessions() -> dict:
    SessionStore.prune()
    sessions = {}
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sessions").fetchall()
        for row in rows:
            data = {}
            if row["date"] is not None: data["date"] = row["date"]
            if row["start_time"] is not None: data["time"] = row["start_time"]
            if row["duration"] is not None: data["duration"] = row["duration"]
            if row["name"] is not None: data["name"] = row["name"]
            if row["booking_ref"] is not None: data["booking_ref"] = row["booking_ref"]
            if row["proposed_date"] is not None: data["proposed_date"] = row["proposed_date"]
            if row["proposed_time"] is not None: data["proposed_time"] = row["proposed_time"]
            if row["proposed_duration"] is not None: data["proposed_duration"] = row["proposed_duration"]
            
            sessions[row["phone"]] = {
                "state": row["state"],
                "data": data,
                "updated_at": row["updated_at"]
            }
    return sessions
