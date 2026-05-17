from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.transactions import get_current_user
from app.db.session import get_db
from app.models.ramp import Ramp
from app.models.user import User
from app.schemas.admin import RampOut, RampUpdateRequest
from app.api.admin import normalize_optional_text
from sqlalchemy import func

router = APIRouter(
    prefix="/employee", tags=["employee"]
)


@router.get("/ramp", response_model=RampOut)
def get_assigned_ramp(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RampOut:
    if current_user.role != "employee":
        raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
        
    if not current_user.ramp_id:
        raise HTTPException(status_code=404, detail="No ramp assigned")
        
    ramp = db.query(Ramp).filter(Ramp.id == current_user.ramp_id).first()
    if not ramp:
        raise HTTPException(status_code=404, detail="Assigned ramp not found")
        
    return RampOut.model_validate(ramp)


@router.put("/ramp", response_model=RampOut)
def update_assigned_ramp(
    payload: RampUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RampOut:
    if current_user.role != "employee":
        raise HTTPException(status_code=403, detail="Only employees can access this endpoint")
        
    if not current_user.ramp_id:
        raise HTTPException(status_code=404, detail="No ramp assigned")
        
    ramp = db.query(Ramp).filter(Ramp.id == current_user.ramp_id).first()
    if not ramp:
        raise HTTPException(status_code=404, detail="Assigned ramp not found")

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Ramp name cannot be empty")

    name_owner = (
        db.query(Ramp)
        .filter(func.lower(Ramp.name) == name.lower(), Ramp.id != ramp.id)
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
