"""
In-memory session store for multi-turn customer booking conversations.

State machine:
  idle
    → awaiting_date      (customer said "book" but no date given)
    → awaiting_time      (have date, need time)
    → awaiting_duration  (have date+time, need duration)
    → awaiting_name      (have slot details, need customer name)
    → awaiting_payment   (sent UPI details, waiting for screenshot)
    → done               (booking created, screenshot received)

Sessions expire after SESSION_TTL_SECONDS of inactivity.
"""
import time
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 600  # 10 minutes

# phone (str) → {"state": str, "data": dict, "updated_at": float}
_sessions: dict[str, dict] = {}

# Valid state sequence
STATES = [
    "idle",
    "awaiting_date",
    "awaiting_time",
    "awaiting_duration",
    "awaiting_name",
    "awaiting_payment",
    "done",
]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _now() -> float:
    return time.monotonic()


def _is_expired(session: dict) -> bool:
    return (_now() - session["updated_at"]) > SESSION_TTL_SECONDS


def _prune() -> None:
    """Remove all expired sessions (called lazily on reads/writes)."""
    expired = [p for p, s in _sessions.items() if _is_expired(s)]
    for p in expired:
        del _sessions[p]
        logger.debug("Session expired and pruned: %s", p[-4:])


# ── Public API ─────────────────────────────────────────────────────────────────

def get_session(phone: str) -> dict:
    """
    Return the current session for a phone number.
    Creates a fresh idle session if none exists or if expired.
    """
    _prune()
    session = _sessions.get(phone)
    if session is None or _is_expired(session):
        session = {"state": "idle", "data": {}, "updated_at": _now()}
        _sessions[phone] = session
    return session


def set_state(phone: str, state: str, data: Optional[dict] = None) -> None:
    """
    Transition a session to a new state, optionally merging data.
    """
    if state not in STATES:
        raise ValueError(f"Invalid state: {state!r}. Must be one of {STATES}")
    session = get_session(phone)
    session["state"] = state
    if data:
        session["data"].update(data)
    session["updated_at"] = _now()
    logger.debug("Session %s → state=%s data=%s", phone[-4:], state, session["data"])


def update_data(phone: str, **kwargs: Any) -> None:
    """Merge extra key/value pairs into session data without changing state."""
    session = get_session(phone)
    session["data"].update(kwargs)
    session["updated_at"] = _now()


def clear_session(phone: str) -> None:
    """Reset session back to idle (after booking done or cancel)."""
    _sessions[phone] = {"state": "idle", "data": {}, "updated_at": _now()}
    logger.debug("Session cleared for %s", phone[-4:])


def get_state(phone: str) -> str:
    return get_session(phone)["state"]


def get_data(phone: str) -> dict:
    return get_session(phone)["data"]


def all_sessions() -> dict:
    """For debugging / tests — returns a copy of the full store."""
    _prune()
    return dict(_sessions)
