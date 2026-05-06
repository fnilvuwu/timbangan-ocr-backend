from fastapi import APIRouter, HTTPException

from app.db.session import ping_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health_check() -> dict:
    return {"status": "ok", "service": "api"}


@router.get("/db")
def health_db() -> dict:
    if not ping_db():
        raise HTTPException(status_code=503, detail="Database is not reachable")

    return {"status": "ok", "database": "sqlite"}
