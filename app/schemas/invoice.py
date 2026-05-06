from datetime import datetime

from pydantic import BaseModel, Field


class InvoiceGenerateRequest(BaseModel):
    invoice_no: str = Field(min_length=3, max_length=64)
    store_name: str = Field(min_length=2, max_length=128)
    customer_name: str | None = Field(default=None, max_length=128)
    cashier_name: str | None = Field(default=None, max_length=128)
    weight: float = Field(ge=0)
    price_per_kg: float = Field(ge=0)
    total_price: float | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=500)
    issued_at: datetime | None = None
