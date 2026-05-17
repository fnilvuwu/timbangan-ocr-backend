from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.db.base import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    employee_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    vehicle_no = Column(String(30), nullable=False)
    ramp_id = Column(Integer, ForeignKey("ramps.id"), nullable=True)
    flow_type = Column(String(20), nullable=False, default="brondolan")
    stage = Column(String(30), nullable=False, default="completed")
    serial_no = Column(String(32), nullable=True)
    relation_name = Column(String(120), nullable=True)
    driver_name = Column(String(120), nullable=True)
    origin_tbs = Column(String(120), nullable=True)
    entry_timestamp = Column(DateTime, nullable=True)
    exit_timestamp = Column(DateTime, nullable=True)
    potongan_percent = Column(Float, nullable=True)
    total_potongan_percent = Column(Float, nullable=True)
    total_potongan_weight = Column(Float, nullable=True)
    sampah_percent = Column(Float, nullable=True)
    air_percent = Column(Float, nullable=True)
    wajib_percent = Column(Float, nullable=True)
    t_panjang_percent = Column(Float, nullable=True)
    j_kosong_percent = Column(Float, nullable=True)
    pengiriman_brd = Column(Float, nullable=True)
    inbound_weight = Column(Float, nullable=True)
    outbound_weight = Column(Float, nullable=True)
    bruto_weight = Column(Float, nullable=False, default=0)
    tara_weight = Column(Float, nullable=False, default=0)
    netto_weight = Column(Float, nullable=False, default=0)
    keterangan = Column(Text, nullable=True)
    captured_image_path = Column(String(500), nullable=False, default="")
    cropped_image_path = Column(String(500), nullable=True)
    crop_points_json = Column(Text, nullable=True)
    inbound_captured_image_path = Column(String(500), nullable=True)
    inbound_cropped_image_path = Column(String(500), nullable=True)
    inbound_crop_points_json = Column(Text, nullable=True)
    outbound_captured_image_path = Column(String(500), nullable=True)
    outbound_cropped_image_path = Column(String(500), nullable=True)
    outbound_crop_points_json = Column(Text, nullable=True)
    capture_timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
