"""
SQLite database models for WebSnap auth + usage tracking.
"""

import os
from datetime import datetime
from typing import Optional

from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.orm import DeclarativeBase, Session

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./websnap.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # needed for SQLite only
)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: int = Column(Integer, primary_key=True, index=True)
    google_id: str = Column(String, unique=True, nullable=False, index=True)
    email: str = Column(String, nullable=False)
    name: Optional[str] = Column(String)
    picture: Optional[str] = Column(String)
    tier: str = Column(String, default="free")  # "free" | "pro"
    created_at: datetime = Column(DateTime, default=datetime.utcnow)


class UsageRecord(Base):
    __tablename__ = "usage"

    id: int = Column(Integer, primary_key=True, index=True)
    user_id: int = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    job_id: Optional[str] = Column(String)
    created_at: datetime = Column(DateTime, default=datetime.utcnow)


def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(engine)


def get_db():
    """FastAPI dependency: yields a DB session and closes it after the request."""
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()
