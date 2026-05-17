from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class TransactionOut(BaseModel):
    id: int
    store_id: int | None = None
    employee_id: int
    employee_name: str | None = None
    store_name: str | None = None
    ramp_id: int | None = None
    ramp_name: str | None = None
    flow_type: str | None = None
    stage: str | None = None
    serial_no: str | None = None
    relation_name: str | None = None
    driver_name: str | None = None
    origin_tbs: str | None = None
    entry_timestamp: datetime | None = None
    exit_timestamp: datetime | None = None
    potongan_percent: float | None = None
    total_potongan_percent: float | None = None
    total_potongan_weight: float | None = None
    sampah_percent: float | None = None
    air_percent: float | None = None
    wajib_percent: float | None = None
    t_panjang_percent: float | None = None
    j_kosong_percent: float | None = None
    pengiriman_brd: float | None = None
    inbound_weight: float | None = None
    outbound_weight: float | None = None
    vehicle_no: str
    bruto_weight: float
    tara_weight: float
    netto_weight: float
    keterangan: str | None = None
    captured_image_path: str
    cropped_image_path: str | None = None
    inbound_captured_image_path: str | None = None
    inbound_cropped_image_path: str | None = None
    outbound_captured_image_path: str | None = None
    outbound_cropped_image_path: str | None = None
    capture_timestamp: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TransactionHistoryResponse(BaseModel):
    period: str
    total_records: int
    total_vehicles: int
    total_netto_weight: float
    items: list[TransactionOut]


class SummarySeriesItem(BaseModel):
    label: str
    total_netto_weight: float
    total_vehicles: int


class TransactionSummaryResponse(BaseModel):
    period: str
    start_date: date | None = None
    end_date: date | None = None
    total_transactions: int
    total_vehicles: int
    total_netto_weight: float
    series: list[SummarySeriesItem] = Field(default_factory=list)
