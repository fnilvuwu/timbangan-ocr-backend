import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import cv2
import numpy as np
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_access_token, verify_ocr_token
from app.db.session import get_db
from app.models.ramp import Ramp
from app.models.store import Store
from app.models.transaction import Transaction
from app.models.user import User
from app.schemas.transaction import (
    SummarySeriesItem,
    TransactionHistoryResponse,
    TransactionOut,
    TransactionSummaryResponse,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])
bearer_scheme = HTTPBearer(auto_error=False)
PeriodFilter = Literal["daily", "monthly"]
FlowType = Literal["brondolan", "ramp"]
StageType = Literal["draft", "inbound_confirmed", "outbound_confirmed", "completed"]


class StartTransactionRequest(BaseModel):
    flow_type: FlowType = "brondolan"
    vehicle_no: str = Field(min_length=1, max_length=30)
    ramp_id: int | None = None
    relation_name: str | None = Field(default=None, max_length=120)
    driver_name: str | None = Field(default=None, max_length=120)
    bruto_weight: float | None = Field(default=None, gt=0)
    origin_tbs: str | None = Field(default=None, max_length=120)
    entry_timestamp: datetime | None = None
    ocr_token: str = Field(...)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


def resolve_employee_ramp_id(current_user: User) -> int | None:
    if current_user.role == "admin":
        return None
    if current_user.ramp_id is None:
        raise HTTPException(
            status_code=400, detail="Ramp assignment is required for employees"
        )
    return current_user.ramp_id


def parse_positive_float(value: str, field_name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be a number"
        ) from exc

    if number <= 0:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be greater than zero"
        )

    return number


def parse_non_negative_float(value: str, field_name: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be a number"
        ) from exc

    if number < 0:
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be greater than or equal to zero",
        )

    return number


def compute_netto(bruto: float, tara: float) -> float:
    netto = round(float(bruto) - float(tara), 2)
    if netto < 0:
        raise HTTPException(
            status_code=400,
            detail="netto_weight cannot be negative. Ensure bruto_weight >= tara_weight",
        )
    return netto


def compute_netto_brondolan(
    bruto: float, tara: float, potongan_percent: float | None
) -> float:
    base_netto = compute_netto(bruto, tara)
    if potongan_percent is None:
        return base_netto
    potongan_value = (float(potongan_percent) / 100.0) * float(bruto)
    result = round(base_netto - potongan_value, 2)
    if result < 0:
        raise HTTPException(
            status_code=400,
            detail="netto_weight cannot be negative after potongan. Verify potongan_percent",
        )
    return result


def compute_exit_deductions(
    bruto: float,
    tara: float,
    percentages: dict[str, float],
) -> dict[str, float]:
    netto_1 = compute_netto(bruto, tara)
    total_percent = round(sum(percentages.values()), 2)
    if total_percent > 100:
        raise HTTPException(
            status_code=400,
            detail="Total potongan percent cannot exceed 100",
        )

    weights = {
        key: round(netto_1 * value / 100.0, 2) for key, value in percentages.items()
    }
    total_weight = round(sum(weights.values()), 2)
    netto_2 = round(netto_1 - total_weight, 2)
    if netto_2 < 0:
        raise HTTPException(
            status_code=400,
            detail="Netto 2 cannot be negative",
        )

    return {
        "netto_1": netto_1,
        "total_percent": total_percent,
        "total_weight": total_weight,
        "netto_2": netto_2,
        **weights,
    }


def parse_period(period: str) -> PeriodFilter:
    normalized = period.strip().lower()
    if normalized not in {"daily", "monthly"}:
        raise HTTPException(
            status_code=400,
            detail="period must be either daily or monthly",
        )
    return normalized  # type: ignore[return-value]


def resolve_date_range(
    period: PeriodFilter,
    target_date: date | None,
    target_month: str | None,
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date]:
    if start_date or end_date:
        range_start = start_date or end_date
        range_end = end_date or start_date
        if range_start is None or range_end is None:
            raise HTTPException(status_code=400, detail="Invalid date range")
        if range_start > range_end:
            raise HTTPException(
                status_code=400, detail="start_date must be <= end_date"
            )
        return range_start, range_end

    if period == "daily":
        selected_date = target_date or date.today()
        return selected_date, selected_date

    month_token = (target_month or date.today().strftime("%Y-%m")).strip()
    try:
        month_start = datetime.strptime(f"{month_token}-01", "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="target_month must be in YYYY-MM format",
        ) from exc

    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_end = next_month - timedelta(days=1)
    return month_start, month_end


def resolve_capture_timestamp(raw_value: str | None) -> datetime:
    if raw_value is None or not raw_value.strip():
        return datetime.utcnow()

    cleaned = raw_value.strip()
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="capture_timestamp must be a valid ISO datetime",
        ) from exc


def parse_optional_int(value: str | None, field_name: str) -> int | None:
    if value is None or not value.strip():
        return None

    cleaned = value.strip()
    try:
        number = int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be an integer"
        ) from exc

    if number <= 0:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be greater than zero"
        )
    return number


def parse_optional_float(value: str | None, field_name: str) -> float | None:
    if value is None or not value.strip():
        return None

    cleaned = value.strip()
    try:
        number = float(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be a number"
        ) from exc

    return number


def normalize_vehicle_no(vehicle_no: str) -> str:
    cleaned = vehicle_no.strip().upper()
    if not cleaned:
        raise HTTPException(status_code=400, detail="vehicle_no is required")
    return cleaned


def parse_flow_type(value: str | None) -> FlowType:
    cleaned = (value or "brondolan").strip().lower()
    if cleaned not in {"brondolan", "ramp"}:
        raise HTTPException(
            status_code=400, detail="flow_type must be brondolan or ramp"
        )
    return cleaned  # type: ignore[return-value]


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def generate_serial_no(db: Session, entry_date: datetime) -> str:
    date_token = entry_date.strftime("%Y%m%d")
    like_prefix = f"{date_token}-%"
    existing = (
        db.query(Transaction.serial_no)
        .filter(Transaction.serial_no.like(like_prefix))
        .order_by(Transaction.serial_no.desc())
        .first()
    )
    if existing and existing[0]:
        try:
            suffix = int(str(existing[0]).split("-")[-1]) + 1
        except (ValueError, IndexError):
            suffix = 1
    else:
        suffix = 1
    return f"{date_token}-{suffix:04d}"


async def save_image_file(
    image: UploadFile,
    now: datetime,
    *,
    prefix: str,
) -> str:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    save_dir = (
        Path(settings.upload_dir)
        / "transactions"
        / now.strftime("%Y")
        / now.strftime("%m")
    )
    save_dir.mkdir(parents=True, exist_ok=True)

    extension = Path(image.filename or "upload.jpg").suffix or ".jpg"
    file_name = f"{prefix}_{uuid4().hex}{extension.lower()}"
    save_path = save_dir / file_name

    content = await image.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")

    save_path.write_bytes(content)

    upload_root = Path(settings.upload_dir).resolve()
    try:
        relative_path = save_path.resolve().relative_to(upload_root)
        return f"uploads/{relative_path.as_posix()}"
    except ValueError:
        return f"uploads/{save_path.name}"


def _decode_uploaded_image(content: bytes) -> np.ndarray:
    np_arr = np.frombuffer(content, np.uint8)
    img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="Invalid image file or format")
    return img


def _order_points(points: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype="float32")
    point_sum = points.sum(axis=1)
    rect[0] = points[np.argmin(point_sum)]
    rect[2] = points[np.argmax(point_sum)]

    point_diff = np.diff(points, axis=1)
    rect[1] = points[np.argmin(point_diff)]
    rect[3] = points[np.argmax(point_diff)]
    return rect


def _parse_crop_points(
    raw_points: str, image_width: int, image_height: int
) -> np.ndarray:
    try:
        payload = json.loads(raw_points)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="crop_points must be valid JSON"
        ) from exc

    if not isinstance(payload, list) or len(payload) != 4:
        raise HTTPException(status_code=400, detail="crop_points must contain 4 points")

    points: list[list[float]] = []
    for point in payload:
        if isinstance(point, dict):
            x = point.get("x")
            y = point.get("y")
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x, y = point[0], point[1]
        else:
            raise HTTPException(
                status_code=400, detail="Each crop point must have x and y"
            )

        try:
            point_x = float(x)
            point_y = float(y)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="crop_points values must be numbers"
            ) from exc

        if (
            point_x < 0
            or point_y < 0
            or point_x > image_width
            or point_y > image_height
        ):
            raise HTTPException(
                status_code=400,
                detail="crop_points must be inside image bounds",
            )

        points.append(
            [
                min(point_x, image_width - 1),
                min(point_y, image_height - 1),
            ]
        )

    return np.array(points, dtype="float32")


def _align_by_perspective(image: np.ndarray, points: np.ndarray) -> np.ndarray:
    ordered = _order_points(points)
    top_left, top_right, bottom_right, bottom_left = ordered

    width_a = np.linalg.norm(bottom_right - bottom_left)
    width_b = np.linalg.norm(top_right - top_left)
    max_width = max(int(round(width_a)), int(round(width_b)))

    height_a = np.linalg.norm(top_right - bottom_right)
    height_b = np.linalg.norm(top_left - bottom_left)
    max_height = max(int(round(height_a)), int(round(height_b)))

    if max_width < 2 or max_height < 2:
        raise HTTPException(status_code=400, detail="Invalid crop area")

    destination = np.array(
        [
            [0, 0],
            [max_width - 1, 0],
            [max_width - 1, max_height - 1],
            [0, max_height - 1],
        ],
        dtype="float32",
    )

    transform_matrix = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(image, transform_matrix, (max_width, max_height))


def save_image_bytes(
    content: bytes, filename: str, now: datetime, *, prefix: str
) -> str:
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")

    save_dir = (
        Path(settings.upload_dir)
        / "transactions"
        / now.strftime("%Y")
        / now.strftime("%m")
    )
    save_dir.mkdir(parents=True, exist_ok=True)

    extension = Path(filename or "upload.jpg").suffix or ".jpg"
    file_name = f"{prefix}_{uuid4().hex}{extension.lower()}"
    save_path = save_dir / file_name
    save_path.write_bytes(content)

    upload_root = Path(settings.upload_dir).resolve()
    try:
        relative_path = save_path.resolve().relative_to(upload_root)
        return f"uploads/{relative_path.as_posix()}"
    except ValueError:
        return f"uploads/{save_path.name}"


def build_cropped_image_bytes(content: bytes, crop_points_raw: str) -> bytes:
    image = _decode_uploaded_image(content)
    height, width = image.shape[:2]
    points = _parse_crop_points(crop_points_raw, width, height)
    aligned = _align_by_perspective(image, points)
    encoded_ok, encoded_image = cv2.imencode(".jpg", aligned)
    if not encoded_ok:
        raise HTTPException(status_code=500, detail="Failed to encode cropped image")
    return encoded_image.tobytes()


def map_transaction_out(
    transaction: Transaction,
    employee_name: str | None = None,
    store_name: str | None = None,
    ramp_name: str | None = None,
) -> TransactionOut:
    return TransactionOut(
        id=transaction.id,
        store_id=transaction.store_id,
        employee_id=transaction.employee_id,
        employee_name=employee_name,
        store_name=store_name,
        ramp_id=transaction.ramp_id,
        ramp_name=ramp_name,
        flow_type=transaction.flow_type,
        stage=transaction.stage,
        serial_no=transaction.serial_no,
        relation_name=transaction.relation_name,
        driver_name=transaction.driver_name,
        origin_tbs=transaction.origin_tbs,
        entry_timestamp=transaction.entry_timestamp,
        exit_timestamp=transaction.exit_timestamp,
        potongan_percent=transaction.potongan_percent,
        total_potongan_percent=transaction.total_potongan_percent,
        total_potongan_weight=transaction.total_potongan_weight,
        sampah_percent=transaction.sampah_percent,
        air_percent=transaction.air_percent,
        wajib_percent=transaction.wajib_percent,
        t_panjang_percent=transaction.t_panjang_percent,
        j_kosong_percent=transaction.j_kosong_percent,
        pengiriman_brd=transaction.pengiriman_brd,
        inbound_weight=transaction.inbound_weight,
        outbound_weight=transaction.outbound_weight,
        vehicle_no=transaction.vehicle_no,
        bruto_weight=transaction.bruto_weight,
        tara_weight=transaction.tara_weight,
        netto_weight=transaction.netto_weight,
        keterangan=transaction.keterangan,
        captured_image_path=transaction.captured_image_path,
        cropped_image_path=transaction.cropped_image_path,
        inbound_captured_image_path=transaction.inbound_captured_image_path,
        inbound_cropped_image_path=transaction.inbound_cropped_image_path,
        outbound_captured_image_path=transaction.outbound_captured_image_path,
        outbound_cropped_image_path=transaction.outbound_cropped_image_path,
        capture_timestamp=transaction.capture_timestamp,
        created_at=transaction.created_at,
    )


def build_transactions_query(db: Session):
    return (
        db.query(Transaction, User.name, Store.name, Ramp.name)
        .join(User, Transaction.employee_id == User.id, isouter=True)
        .join(Store, Transaction.store_id == Store.id, isouter=True)
        .join(Ramp, Transaction.ramp_id == Ramp.id, isouter=True)
    )


@router.post("", response_model=TransactionOut, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    vehicle_no: str = Form(...),
    ramp_id: str | None = Form(None),
    bruto_weight: str = Form(...),
    tara_weight: str = Form(...),
    capture_timestamp: str | None = Form(None),
    crop_points: str | None = Form(None),
    captured_image: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    cropped_image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionOut:
    employee_ramp_id = resolve_employee_ramp_id(current_user)
    capture_file = captured_image or image
    if capture_file is None:
        raise HTTPException(status_code=400, detail="captured_image is required")

    cleaned_vehicle_no = normalize_vehicle_no(vehicle_no)
    bruto_value = parse_positive_float(bruto_weight, "bruto_weight")
    tara_value = parse_non_negative_float(tara_weight, "tara_weight")
    netto_value = compute_netto(bruto_value, tara_value)

    ramp_id_value = parse_optional_int(ramp_id, "ramp_id")
    if employee_ramp_id is not None:
        if ramp_id_value is not None and ramp_id_value != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")
        ramp_id_value = employee_ramp_id
    ramp = None
    if ramp_id_value is not None:
        ramp = db.query(Ramp).filter(Ramp.id == ramp_id_value).first()
        if not ramp:
            raise HTTPException(status_code=404, detail="Ramp not found")
        if not ramp.is_active:
            raise HTTPException(status_code=400, detail="Ramp is inactive")

    crop_points_json = None
    if crop_points and crop_points.strip():
        try:
            parsed_points = json.loads(crop_points)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="crop_points must be valid JSON"
            ) from exc
        if not isinstance(parsed_points, list) or len(parsed_points) != 4:
            raise HTTPException(
                status_code=400,
                detail="crop_points must contain exactly 4 points",
            )
        crop_points_json = json.dumps(parsed_points)

    capture_at = resolve_capture_timestamp(capture_timestamp)
    now = datetime.utcnow()
    captured_image_path = await save_image_file(capture_file, now, prefix="capture")

    cropped_image_path = None
    if cropped_image and cropped_image.filename:
        cropped_image_path = await save_image_file(cropped_image, now, prefix="aligned")

    entry_value = capture_at
    serial_no = generate_serial_no(db, entry_value)

    transaction = Transaction(
        store_id=current_user.store_id,
        employee_id=current_user.id,
        vehicle_no=cleaned_vehicle_no,
        ramp_id=ramp_id_value,
        flow_type="brondolan",
        stage="completed",
        serial_no=serial_no,
        entry_timestamp=entry_value,
        bruto_weight=bruto_value,
        tara_weight=tara_value,
        netto_weight=netto_value,
        captured_image_path=captured_image_path,
        cropped_image_path=cropped_image_path,
        crop_points_json=crop_points_json,
        inbound_weight=bruto_value,
        outbound_weight=tara_value,
        inbound_captured_image_path=captured_image_path,
        inbound_cropped_image_path=cropped_image_path,
        inbound_crop_points_json=crop_points_json,
        outbound_captured_image_path=None,
        outbound_cropped_image_path=None,
        outbound_crop_points_json=None,
        capture_timestamp=capture_at,
    )

    db.add(transaction)
    db.commit()
    db.refresh(transaction)

    store_name = None
    if transaction.store_id:
        store = db.query(Store).filter(Store.id == transaction.store_id).first()
        store_name = store.name if store else None

    ramp_name = ramp.name if ramp else None

    return map_transaction_out(
        transaction,
        employee_name=current_user.name,
        store_name=store_name,
        ramp_name=ramp_name,
    )


@router.post(
    "/start", response_model=TransactionOut, status_code=status.HTTP_201_CREATED
)
def start_transaction(
    payload: StartTransactionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionOut:
    employee_ramp_id = resolve_employee_ramp_id(current_user)
    flow_type = parse_flow_type(payload.flow_type)
    cleaned_vehicle_no = normalize_vehicle_no(payload.vehicle_no)

    ramp_id_value = payload.ramp_id
    if employee_ramp_id is not None:
        if ramp_id_value is not None and ramp_id_value != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")
        ramp_id_value = employee_ramp_id
    ramp = None
    if ramp_id_value is not None:
        ramp = db.query(Ramp).filter(Ramp.id == ramp_id_value).first()
        if not ramp:
            raise HTTPException(status_code=404, detail="Ramp not found")
        if not ramp.is_active:
            raise HTTPException(status_code=400, detail="Ramp is inactive")

    entry_value = payload.entry_timestamp or datetime.utcnow()
    serial_no = generate_serial_no(db, entry_value)

    relation_name = normalize_optional_text(payload.relation_name)
    driver_name = normalize_optional_text(payload.driver_name)
    if not driver_name:
        raise HTTPException(status_code=400, detail="driver_name is required")
    if payload.bruto_weight is None:
        raise HTTPException(status_code=400, detail="bruto_weight is required")

    if not verify_ocr_token(payload.ocr_token, payload.bruto_weight):
        raise HTTPException(status_code=400, detail="bruto_weight tidak sesuai dengan hasil OCR atau token tidak valid")

    bruto_value = parse_positive_float(str(payload.bruto_weight), "bruto_weight")

    transaction = Transaction(
        store_id=current_user.store_id,
        employee_id=current_user.id,
        vehicle_no=cleaned_vehicle_no,
        ramp_id=ramp_id_value,
        flow_type=flow_type,
        stage="draft",
        serial_no=serial_no,
        relation_name=relation_name,
        driver_name=driver_name,
        origin_tbs=normalize_optional_text(payload.origin_tbs),
        entry_timestamp=entry_value,
        capture_timestamp=entry_value,
        bruto_weight=bruto_value,
        inbound_weight=bruto_value,
    )

    db.add(transaction)
    db.commit()
    db.refresh(transaction)

    store_name = None
    if transaction.store_id:
        store = db.query(Store).filter(Store.id == transaction.store_id).first()
        store_name = store.name if store else None

    ramp_name = ramp.name if ramp else None

    return map_transaction_out(
        transaction,
        employee_name=current_user.name,
        store_name=store_name,
        ramp_name=ramp_name,
    )


@router.get("/open", response_model=list[TransactionOut])
def list_open_transactions(
    search: str | None = Query(None, max_length=120),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TransactionOut]:
    query = build_transactions_query(db).order_by(
        Transaction.entry_timestamp.desc(),
        Transaction.created_at.desc(),
    )

    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        query = query.filter(
            Transaction.ramp_id == employee_ramp_id,
        )

    query = query.filter(Transaction.stage != "completed")

    if search and search.strip():
        token = f"%{search.strip()}%"
        query = query.filter(
            (Transaction.serial_no.ilike(token)) | (Transaction.vehicle_no.ilike(token))
        )

    rows = query.limit(100).all()
    return [
        map_transaction_out(
            tx,
            employee_name=employee_name,
            store_name=store_name,
            ramp_name=ramp_name,
        )
        for tx, employee_name, store_name, ramp_name in rows
    ]


@router.post("/close", response_model=TransactionOut)
async def close_transaction(
    serial_no: str = Form(...),
    tara_weight: str = Form(...),
    ocr_token: str = Form(...),
    sampah_percent: str = Form("0"),
    air_percent: str = Form("0"),
    wajib_percent: str = Form("0"),
    t_panjang_percent: str = Form("0"),
    j_kosong_percent: str = Form("0"),
    keterangan: str | None = Form(None),
    exit_timestamp: str | None = Form(None),
    crop_points: str | None = Form(None),
    captured_image: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    cropped_image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionOut:
    serial_no = serial_no.strip()
    transaction = (
        db.query(Transaction).filter(Transaction.serial_no == serial_no).first()
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if transaction.stage == "completed":
        raise HTTPException(status_code=400, detail="Transaction is already closed")
    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        if transaction.ramp_id != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")

    tara_value = parse_positive_float(tara_weight, "tara_weight")
    if not verify_ocr_token(ocr_token, tara_value):
        raise HTTPException(status_code=400, detail="tara_weight tidak sesuai dengan hasil OCR atau token tidak valid")

    percentages = {
        "sampah_percent": parse_non_negative_float(sampah_percent, "sampah_percent"),
        "air_percent": parse_non_negative_float(air_percent, "air_percent"),
        "wajib_percent": parse_non_negative_float(wajib_percent, "wajib_percent"),
        "t_panjang_percent": parse_non_negative_float(
            t_panjang_percent, "t_panjang_percent"
        ),
        "j_kosong_percent": parse_non_negative_float(
            j_kosong_percent, "j_kosong_percent"
        ),
    }
    deductions = compute_exit_deductions(
        transaction.bruto_weight,
        tara_value,
        percentages,
    )

    exit_value = (
        resolve_capture_timestamp(exit_timestamp)
        if exit_timestamp
        else datetime.utcnow()
    )
    transaction.tara_weight = tara_value
    transaction.potongan_percent = deductions["total_percent"]
    transaction.total_potongan_percent = deductions["total_percent"]
    transaction.total_potongan_weight = deductions["total_weight"]
    transaction.sampah_percent = percentages["sampah_percent"]
    transaction.air_percent = percentages["air_percent"]
    transaction.wajib_percent = percentages["wajib_percent"]
    transaction.t_panjang_percent = percentages["t_panjang_percent"]
    transaction.j_kosong_percent = percentages["j_kosong_percent"]
    transaction.netto_weight = deductions["netto_2"]
    transaction.outbound_weight = tara_value
    transaction.keterangan = normalize_optional_text(keterangan)
    transaction.exit_timestamp = exit_value
    transaction.stage = "completed"

    crop_points_json = None
    if crop_points and crop_points.strip():
        try:
            parsed_points = json.loads(crop_points)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="crop_points must be valid JSON"
            ) from exc
        if not isinstance(parsed_points, list) or len(parsed_points) != 4:
            raise HTTPException(
                status_code=400,
                detail="crop_points must contain exactly 4 points",
            )
        crop_points_json = json.dumps(parsed_points)

    capture_file = captured_image or image
    if capture_file is not None:
        if not capture_file.content_type or not capture_file.content_type.startswith(
            "image/"
        ):
            raise HTTPException(
                status_code=400, detail="Only image uploads are allowed"
            )
        now = datetime.utcnow()
        content = await capture_file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded image is empty")
        transaction.outbound_captured_image_path = save_image_bytes(
            content, capture_file.filename or "upload.jpg", now, prefix="outbound"
        )

        if crop_points_json:
            cropped_bytes = build_cropped_image_bytes(content, crop_points_json)
            transaction.outbound_cropped_image_path = save_image_bytes(
                cropped_bytes, "outbound_aligned.jpg", now, prefix="outbound_aligned"
            )
            transaction.outbound_crop_points_json = crop_points_json
        elif cropped_image and cropped_image.filename:
            transaction.outbound_cropped_image_path = await save_image_file(
                cropped_image, now, prefix="outbound_aligned"
            )

    db.commit()
    db.refresh(transaction)

    store_name = None
    if transaction.store_id:
        store = db.query(Store).filter(Store.id == transaction.store_id).first()
        store_name = store.name if store else None

    ramp_name = None
    if transaction.ramp_id:
        ramp = db.query(Ramp).filter(Ramp.id == transaction.ramp_id).first()
        ramp_name = ramp.name if ramp else None

    return map_transaction_out(
        transaction,
        employee_name=current_user.name,
        store_name=store_name,
        ramp_name=ramp_name,
    )


@router.post("/{transaction_id}/inbound", response_model=TransactionOut)
async def capture_inbound(
    transaction_id: int,
    weight: str = Form(...),
    entry_timestamp: str | None = Form(None),
    crop_points: str | None = Form(None),
    captured_image: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    cropped_image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionOut:
    capture_file = captured_image or image
    if capture_file is None:
        raise HTTPException(status_code=400, detail="captured_image is required")
    if not capture_file.content_type or not capture_file.content_type.startswith(
        "image/"
    ):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    transaction = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        if transaction.employee_id != current_user.id:
            raise HTTPException(
                status_code=403, detail="Not allowed to update this transaction"
            )
        if transaction.ramp_id != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")

    flow_type = parse_flow_type(transaction.flow_type)
    weight_value = parse_positive_float(weight, "weight")

    crop_points_json = None
    if crop_points and crop_points.strip():
        try:
            parsed_points = json.loads(crop_points)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="crop_points must be valid JSON"
            ) from exc
        if not isinstance(parsed_points, list) or len(parsed_points) != 4:
            raise HTTPException(
                status_code=400,
                detail="crop_points must contain exactly 4 points",
            )
        crop_points_json = json.dumps(parsed_points)

    entry_value = (
        resolve_capture_timestamp(entry_timestamp) if entry_timestamp else None
    )
    if entry_value is not None:
        transaction.entry_timestamp = entry_value
        transaction.capture_timestamp = entry_value

    now = datetime.utcnow()
    content = await capture_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")
    inbound_captured_path = save_image_bytes(
        content, capture_file.filename or "upload.jpg", now, prefix="inbound"
    )
    inbound_cropped_path = None
    if crop_points_json:
        cropped_bytes = build_cropped_image_bytes(content, crop_points_json)
        inbound_cropped_path = save_image_bytes(
            cropped_bytes, "inbound_aligned.jpg", now, prefix="inbound_aligned"
        )
    elif cropped_image and cropped_image.filename:
        inbound_cropped_path = await save_image_file(
            cropped_image, now, prefix="inbound_aligned"
        )

    transaction.inbound_weight = weight_value
    if flow_type == "brondolan":
        transaction.bruto_weight = weight_value
    else:
        transaction.tara_weight = weight_value

    transaction.inbound_captured_image_path = inbound_captured_path
    transaction.inbound_cropped_image_path = inbound_cropped_path
    transaction.inbound_crop_points_json = crop_points_json
    transaction.captured_image_path = inbound_captured_path
    transaction.cropped_image_path = inbound_cropped_path
    transaction.crop_points_json = crop_points_json
    transaction.stage = "inbound_confirmed"

    db.commit()
    db.refresh(transaction)

    store_name = None
    if transaction.store_id:
        store = db.query(Store).filter(Store.id == transaction.store_id).first()
        store_name = store.name if store else None

    ramp_name = None
    if transaction.ramp_id:
        ramp = db.query(Ramp).filter(Ramp.id == transaction.ramp_id).first()
        ramp_name = ramp.name if ramp else None

    return map_transaction_out(
        transaction,
        employee_name=current_user.name,
        store_name=store_name,
        ramp_name=ramp_name,
    )


@router.post("/{transaction_id}/outbound", response_model=TransactionOut)
async def capture_outbound(
    transaction_id: int,
    weight: str | None = Form(None),
    exit_timestamp: str | None = Form(None),
    potongan_percent: str | None = Form(None),
    pengiriman_brd: str | None = Form(None),
    crop_points: str | None = Form(None),
    captured_image: UploadFile | None = File(None),
    image: UploadFile | None = File(None),
    cropped_image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionOut:
    capture_file = captured_image or image
    if capture_file is None:
        raise HTTPException(status_code=400, detail="captured_image is required")
    if not capture_file.content_type or not capture_file.content_type.startswith(
        "image/"
    ):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    transaction = db.query(Transaction).filter(Transaction.id == transaction_id).first()
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")

    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        if transaction.employee_id != current_user.id:
            raise HTTPException(
                status_code=403, detail="Not allowed to update this transaction"
            )
        if transaction.ramp_id != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")

    flow_type = parse_flow_type(transaction.flow_type)
    weight_value = parse_optional_float(weight, "weight")
    potongan_value = parse_optional_float(potongan_percent, "potongan_percent")
    pengiriman_value = parse_optional_float(pengiriman_brd, "pengiriman_brd")

    if potongan_value is not None and (potongan_value < 0 or potongan_value > 100):
        raise HTTPException(
            status_code=400, detail="potongan_percent must be between 0 and 100"
        )

    crop_points_json = None
    if crop_points and crop_points.strip():
        try:
            parsed_points = json.loads(crop_points)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="crop_points must be valid JSON"
            ) from exc
        if not isinstance(parsed_points, list) or len(parsed_points) != 4:
            raise HTTPException(
                status_code=400,
                detail="crop_points must contain exactly 4 points",
            )
        crop_points_json = json.dumps(parsed_points)

    exit_value = resolve_capture_timestamp(exit_timestamp) if exit_timestamp else None
    if exit_value is not None:
        transaction.exit_timestamp = exit_value

    now = datetime.utcnow()
    content = await capture_file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded image is empty")
    outbound_captured_path = save_image_bytes(
        content, capture_file.filename or "upload.jpg", now, prefix="outbound"
    )
    outbound_cropped_path = None
    if crop_points_json:
        cropped_bytes = build_cropped_image_bytes(content, crop_points_json)
        outbound_cropped_path = save_image_bytes(
            cropped_bytes, "outbound_aligned.jpg", now, prefix="outbound_aligned"
        )
    elif cropped_image and cropped_image.filename:
        outbound_cropped_path = await save_image_file(
            cropped_image, now, prefix="outbound_aligned"
        )

    transaction.outbound_weight = weight_value
    transaction.outbound_captured_image_path = outbound_captured_path
    transaction.outbound_cropped_image_path = outbound_cropped_path
    transaction.outbound_crop_points_json = crop_points_json

    if flow_type == "brondolan":
        if weight_value is None:
            raise HTTPException(
                status_code=400, detail="weight is required for brondolan outbound"
            )
        transaction.tara_weight = parse_non_negative_float(
            str(weight_value), "tara_weight"
        )
        transaction.potongan_percent = potongan_value
        transaction.netto_weight = compute_netto_brondolan(
            transaction.bruto_weight,
            transaction.tara_weight,
            transaction.potongan_percent,
        )
    else:
        if pengiriman_value is None or pengiriman_value <= 0:
            raise HTTPException(
                status_code=400, detail="pengiriman_brd is required for ramp"
            )
        transaction.pengiriman_brd = pengiriman_value
        transaction.netto_weight = round(float(pengiriman_value), 2)

    transaction.stage = "completed"

    db.commit()
    db.refresh(transaction)

    store_name = None
    if transaction.store_id:
        store = db.query(Store).filter(Store.id == transaction.store_id).first()
        store_name = store.name if store else None

    ramp_name = None
    if transaction.ramp_id:
        ramp = db.query(Ramp).filter(Ramp.id == transaction.ramp_id).first()
        ramp_name = ramp.name if ramp else None

    return map_transaction_out(
        transaction,
        employee_name=current_user.name,
        store_name=store_name,
        ramp_name=ramp_name,
    )


@router.get("", response_model=list[TransactionOut])
def list_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[TransactionOut]:
    query = build_transactions_query(db).order_by(
        Transaction.capture_timestamp.desc(),
        Transaction.created_at.desc(),
    )

    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        query = query.filter(
            Transaction.ramp_id == employee_ramp_id,
        )

    query = query.filter(Transaction.stage != "draft")

    rows = query.limit(100).all()
    return [
        map_transaction_out(
            tx,
            employee_name=employee_name,
            store_name=store_name,
            ramp_name=ramp_name,
        )
        for tx, employee_name, store_name, ramp_name in rows
    ]


@router.get("/history", response_model=TransactionHistoryResponse)
def transaction_history(
    period: str = Query("daily"),
    target_date: date | None = Query(None),
    target_month: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    ramp_id: str | None = Query(None),
    employee_id: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionHistoryResponse:
    period_value = parse_period(period)
    range_start, range_end = resolve_date_range(
        period_value,
        target_date,
        target_month,
        start_date,
        end_date,
    )

    start_dt = datetime.combine(range_start, time.min)
    end_dt = datetime.combine(range_end, time.max)

    query = build_transactions_query(db).filter(
        Transaction.capture_timestamp >= start_dt,
        Transaction.capture_timestamp <= end_dt,
    )
    ramp_id_value = parse_optional_int(ramp_id, "ramp_id")
    employee_id_value = parse_optional_int(employee_id, "employee_id")

    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        if ramp_id_value is not None and ramp_id_value != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")
        ramp_id_value = employee_ramp_id

    if ramp_id_value is not None:
        query = query.filter(Transaction.ramp_id == ramp_id_value)
    if employee_id_value is not None:
        query = query.filter(Transaction.employee_id == employee_id_value)

    query = query.filter(Transaction.stage != "draft")

    rows = query.order_by(Transaction.capture_timestamp.desc()).all()
    items = [
        map_transaction_out(
            tx,
            employee_name=employee_name,
            store_name=store_name,
            ramp_name=ramp_name,
        )
        for tx, employee_name, store_name, ramp_name in rows
    ]

    total_netto_weight = round(sum(item.netto_weight for item in items), 2)
    total_vehicles = len({item.vehicle_no for item in items})

    return TransactionHistoryResponse(
        period=period_value,
        total_records=len(items),
        total_vehicles=total_vehicles,
        total_netto_weight=total_netto_weight,
        items=items,
    )


@router.get("/summary", response_model=TransactionSummaryResponse)
def transaction_summary(
    period: str = Query("daily"),
    target_date: date | None = Query(None),
    target_month: str | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    ramp_id: str | None = Query(None),
    employee_id: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TransactionSummaryResponse:
    period_value = parse_period(period)
    range_start, range_end = resolve_date_range(
        period_value,
        target_date,
        target_month,
        start_date,
        end_date,
    )

    start_dt = datetime.combine(range_start, time.min)
    end_dt = datetime.combine(range_end, time.max)

    query = db.query(Transaction).filter(
        Transaction.capture_timestamp >= start_dt,
        Transaction.capture_timestamp <= end_dt,
    )
    ramp_id_value = parse_optional_int(ramp_id, "ramp_id")
    employee_id_value = parse_optional_int(employee_id, "employee_id")

    employee_ramp_id = resolve_employee_ramp_id(current_user)
    if employee_ramp_id is not None:
        if ramp_id_value is not None and ramp_id_value != employee_ramp_id:
            raise HTTPException(status_code=403, detail="Ramp access is not allowed")
        ramp_id_value = employee_ramp_id

    if ramp_id_value is not None:
        query = query.filter(Transaction.ramp_id == ramp_id_value)
    if employee_id_value is not None:
        query = query.filter(Transaction.employee_id == employee_id_value)

    query = query.filter(Transaction.stage != "draft")

    transactions = query.order_by(Transaction.capture_timestamp.asc()).all()

    total_transactions = len(transactions)
    total_vehicles = len({tx.vehicle_no for tx in transactions})
    total_netto_weight = round(sum(tx.netto_weight for tx in transactions), 2)

    grouped: dict[str, dict[str, object]] = defaultdict(
        lambda: {"total_netto_weight": 0.0, "vehicles": set()}
    )
    for tx in transactions:
        if period_value == "daily":
            label = tx.capture_timestamp.strftime("%Y-%m-%d")
        else:
            label = tx.capture_timestamp.strftime("%Y-%m")

        grouped[label]["total_netto_weight"] = (
            float(grouped[label]["total_netto_weight"]) + tx.netto_weight
        )
        grouped[label]["vehicles"].add(tx.vehicle_no)

    series = [
        SummarySeriesItem(
            label=label,
            total_netto_weight=round(float(payload["total_netto_weight"]), 2),
            total_vehicles=len(payload["vehicles"]),
        )
        for label, payload in sorted(grouped.items(), key=lambda item: item[0])
    ]

    return TransactionSummaryResponse(
        period=period_value,
        start_date=range_start,
        end_date=range_end,
        total_transactions=total_transactions,
        total_vehicles=total_vehicles,
        total_netto_weight=total_netto_weight,
        series=series,
    )
