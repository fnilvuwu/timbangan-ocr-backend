from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class EmployeeCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6)
    role: str = "employee"
    ramp_id: int | None = None


class EmployeeUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    role: str = "employee"
    ramp_id: int | None = None


class EmployeeOut(BaseModel):
    id: int
    name: str
    email: EmailStr
    role: str
    ramp_id: int | None = None
    ramp_name: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RampCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class RampUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=255)
    is_active: bool = True


class RampOut(BaseModel):
    id: int
    name: str
    description: str | None = None
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
