from sqlalchemy import Column, String, Boolean, Integer
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin


class Customer(Base, TimestampMixin):
    """
    Represents a customer who has interacted via WhatsApp.
    phone is the WhatsApp number in E.164 format (e.g. 919876543210).
    """
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=True)          # filled once they share their name
    is_owner = Column(Boolean, default=False, nullable=False)

    # conversation state – stores which step of the booking flow they are in
    # e.g. "idle" | "awaiting_date" | "awaiting_slot" | "awaiting_payment"
    conversation_state = Column(String(50), default="idle", nullable=False)

    # temporary storage for multi-turn booking data (JSON string)
    conversation_context = Column(String(2000), default="{}", nullable=False)

    bookings = relationship("Booking", back_populates="customer", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Customer phone={self.phone} name={self.name}>"
