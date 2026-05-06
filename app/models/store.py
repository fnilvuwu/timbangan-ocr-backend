from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.db.base import Base


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
