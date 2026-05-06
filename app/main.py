from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.health import router as health_router
from app.api.admin import router as admin_router
from app.api.transactions import router as transactions_router
from app.api.invoice import router as invoice_router
from app.api.ocr import router as ocr_router
from app.core.config import settings
from app.db.base import Base
from app.db.migrations import run_schema_migrations
from app.db.session import engine
from app.models import Ramp, Store, Transaction, User  # noqa: F401

app = FastAPI(title=settings.app_name)

app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(transactions_router)
app.include_router(invoice_router)
app.include_router(ocr_router)


@app.on_event("startup")
def startup() -> None:
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    run_schema_migrations(engine)
    Base.metadata.create_all(bind=engine)


@app.get("/")
def root() -> dict:
    return {"message": "ScaleScan API running"}
