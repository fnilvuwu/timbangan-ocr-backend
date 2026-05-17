from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.ramp import Ramp
from app.models.user import User
from app.schemas.auth import AuthResponse, LoginRequest, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
VALID_ROLES = {"admin", "employee"}




@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(subject=str(user.id), role=user.role)
    return AuthResponse(access_token=token, user=UserOut.model_validate(user))
