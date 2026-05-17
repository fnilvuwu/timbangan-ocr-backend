from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="employee", nullable=False)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    ramp_id = Column(Integer, ForeignKey("ramps.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
