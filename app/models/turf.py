from sqlalchemy import Column, String, Integer, Numeric, Time
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin


class Turf(Base, TimestampMixin):
    """
    Represents a turf ground. Keeping this as its own table so you can
    later manage multiple turfs from a single WhatsApp number.
    """
    __tablename__ = "turfs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    location = Column(String(255), nullable=True)
    open_time = Column(Time, nullable=False)           # e.g. 06:00
    close_time = Column(Time, nullable=False)          # e.g. 23:00
    slot_duration_minutes = Column(Integer, default=60, nullable=False)

    # price per slot (INR)
    price_per_slot = Column(Numeric(10, 2), nullable=False, default=600)
    advance_amount = Column(Numeric(10, 2), nullable=False, default=500)

    bookings = relationship("Booking", back_populates="turf", lazy="selectin")
    blocked_slots = relationship("BlockedSlot", back_populates="turf", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Turf id={self.id} name={self.name}>"
