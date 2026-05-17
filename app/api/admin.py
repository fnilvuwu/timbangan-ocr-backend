from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.transactions import get_current_user
from app.core.security import hash_password
from app.db.session import get_db
from app.models.ramp import Ramp
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.admin import (
    EmployeeCreateRequest,
    EmployeeOut,
    EmployeeUpdateRequest,
    RampCreateRequest,
    RampOut,
    RampUpdateRequest,
)


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)
VALID_ROLES = {"admin", "employee"}


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


@router.get("/ramps", response_model=list[RampOut])
def list_ramps(db: Session = Depends(get_db)) -> list[RampOut]:
    ramps = db.query(Ramp).order_by(Ramp.created_at.desc()).all()
    return [RampOut.model_validate(ramp) for ramp in ramps]


@router.post("/ramps", response_model=RampOut, status_code=status.HTTP_201_CREATED)
def create_ramp(payload: RampCreateRequest, db: Session = Depends(get_db)) -> RampOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Ramp name cannot be empty")

    existing = db.query(Ramp).filter(func.lower(Ramp.name) == name.lower()).first()
    if existing:
        raise HTTPException(status_code=400, detail="Ramp name already exists")

    ramp = Ramp(
        name=name,
        description=normalize_optional_text(payload.description),
        is_active=payload.is_active,
    )
    db.add(ramp)
    db.commit()
    db.refresh(ramp)
    return RampOut.model_validate(ramp)


@router.put("/ramps/{ramp_id}", response_model=RampOut)
def update_ramp(
    ramp_id: int, payload: RampUpdateRequest, db: Session = Depends(get_db)
) -> RampOut:
    ramp = db.query(Ramp).filter(Ramp.id == ramp_id).first()
    if not ramp:
        raise HTTPException(status_code=404, detail="Ramp not found")

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Ramp name cannot be empty")

    name_owner = (
        db.query(Ramp)
        .filter(func.lower(Ramp.name) == name.lower(), Ramp.id != ramp_id)
        .first()
    )
    if name_owner:
        raise HTTPException(status_code=400, detail="Ramp name already exists")

    ramp.name = name
    ramp.description = normalize_optional_text(payload.description)
    ramp.is_active = payload.is_active

    db.commit()
    db.refresh(ramp)
    return RampOut.model_validate(ramp)


@router.delete("/ramps/{ramp_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ramp(ramp_id: int, db: Session = Depends(get_db)) -> None:
    ramp = db.query(Ramp).filter(Ramp.id == ramp_id).first()
    if not ramp:
        raise HTTPException(status_code=404, detail="Ramp not found")

    used_transaction = (
        db.query(Transaction).filter(Transaction.ramp_id == ramp_id).first()
    )
    if used_transaction:
        raise HTTPException(
            status_code=400,
            detail="Ramp is already used by transactions and cannot be deleted",
        )

    db.delete(ramp)
    db.commit()


@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(db: Session = Depends(get_db)) -> list[EmployeeOut]:
    users = db.query(User).order_by(User.created_at.desc()).all()
    ramps = db.query(Ramp).all()
    ramp_map = {ramp.id: ramp.name for ramp in ramps}

    return [
        EmployeeOut(
            id=user.id,
            name=user.name,
            email=user.email,
            role=user.role,
            ramp_id=user.ramp_id,
            ramp_name=ramp_map.get(user.ramp_id) if user.ramp_id else None,
            created_at=user.created_at,
        )
        for user in users
    ]


@router.post(
    "/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED
)
def create_employee(
    payload: EmployeeCreateRequest, db: Session = Depends(get_db)
) -> EmployeeOut:
    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Role must be admin or employee")

    if payload.role == "employee" and payload.ramp_id is None:
        raise HTTPException(status_code=400, detail="Ramp is required for employees")

    existing_user = db.query(User).filter(User.email == payload.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    ramp = None
    if payload.ramp_id is not None:
        ramp = db.query(Ramp).filter(Ramp.id == payload.ramp_id).first()
        if not ramp:
            raise HTTPException(status_code=404, detail="Ramp not found")
        if not ramp.is_active:
            raise HTTPException(status_code=400, detail="Ramp is inactive")

    user = User(
        name=payload.name.strip(),
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
        ramp_id=payload.ramp_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    ramp_name = ramp.name if ramp else None

    return EmployeeOut(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        ramp_id=user.ramp_id,
        ramp_name=ramp_name,
        created_at=user.created_at,
    )


@router.put("/employees/{employee_id}", response_model=EmployeeOut)
def update_employee(
    employee_id: int, payload: EmployeeUpdateRequest, db: Session = Depends(get_db)
) -> EmployeeOut:
    user = db.query(User).filter(User.id == employee_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    if payload.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Role must be admin or employee")

    if payload.role == "employee" and payload.ramp_id is None:
        raise HTTPException(status_code=400, detail="Ramp is required for employees")

    email_owner = (
        db.query(User)
        .filter(User.email == payload.email, User.id != employee_id)
        .first()
    )
    if email_owner:
        raise HTTPException(status_code=400, detail="Email already registered")

    ramp = None
    if payload.ramp_id is not None:
        ramp = db.query(Ramp).filter(Ramp.id == payload.ramp_id).first()
        if not ramp:
            raise HTTPException(status_code=404, detail="Ramp not found")
        if not ramp.is_active:
            raise HTTPException(status_code=400, detail="Ramp is inactive")

    user.name = payload.name.strip()
    user.email = payload.email
    user.role = payload.role
    user.ramp_id = payload.ramp_id

    db.commit()
    db.refresh(user)

    ramp_name = ramp.name if ramp else None

    return EmployeeOut(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role,
        ramp_id=user.ramp_id,
        ramp_name=ramp_name,
        created_at=user.created_at,
    )


@router.delete("/employees/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_employee(employee_id: int, db: Session = Depends(get_db)) -> None:
    user = db.query(User).filter(User.id == employee_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    db.delete(user)
    db.commit()
