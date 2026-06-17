from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, DateTime, func


class Base(DeclarativeBase):
    """All models inherit from this base."""
    pass


class TimestampMixin:
    """Adds created_at / updated_at to any model."""
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
