import os
import base64
import json
from typing import Any, Dict

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.transactions import get_current_user

# Work around Paddle CPU runtime issues on some Windows setups
# (OneDNN + PIR conversion errors during OCR execution).
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")

try:
    from paddleocr import PaddleOCR
except ImportError:
    PaddleOCR = None

router = APIRouter(prefix="/ocr", tags=["ocr"])

ocr_model = None
OCR_MAX_LONG_SIDE = 960
OCR_ENABLE_PADDING = False
OCR_PADDING_MULTIPLE = 32


def get_ocr_model():
    global ocr_model
    if ocr_model is None and PaddleOCR is not None:
        mobile_base = {
            "lang": "en",
            "text_detection_model_name": "PP-OCRv5_mobile_det",
            "text_recognition_model_name": "en_PP-OCRv5_mobile_rec",
        }
        init_variants = [
            {
                # Prefer lightweight text OCR only for faster scale-number reads.
                **mobile_base,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
                "enable_mkldnn": False,
                "cpu_threads": 1,
            },
            {
                **mobile_base,
                "use_textline_orientation": False,
                "enable_mkldnn": False,
            },
            {**mobile_base, "use_textline_orientation": False},
            {**mobile_base},
        ]

        last_error = None
        for kwargs in init_variants:
            try:
                ocr_model = PaddleOCR(**kwargs)
                break
            except ValueError as e:
                error_text = str(e)
                if (
                    "Unknown argument" in error_text
                    or "mutually exclusive" in error_text
                ):
                    last_error = e
                    continue
                raise

        if ocr_model is None and last_error is not None:
            raise RuntimeError(f"Failed to initialize PaddleOCR: {last_error}")
    return ocr_model


def _run_ocr(model, img):
    try:
        return model.ocr(img, cls=False)
    except TypeError as e:
        if "unexpected keyword argument 'cls'" in str(e):
            return model.ocr(img)
        raise


def _extract_ocr_texts(result: Any) -> list[str]:
    extracted_texts: list[str] = []
    if not result:
        return extracted_texts

    # Legacy output: [[[[x,y],...], (text, score)], ...]
    if isinstance(result, list) and result and isinstance(result[0], list):
        for line in result[0]:
            if (
                len(line) >= 2
                and isinstance(line[1], (list, tuple))
                and len(line[1]) >= 1
            ):
                extracted_texts.append(str(line[1][0]))
        return extracted_texts

    # Newer output can be list[dict] with rec_texts.
    if isinstance(result, list) and result and isinstance(result[0], dict):
        for item in result:
            rec_texts = item.get("rec_texts") or []
            for text in rec_texts:
                extracted_texts.append(str(text))

    return extracted_texts


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


def _resize_for_ocr(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    if h <= 0 or w <= 0:
        return img

    # Keep aspect ratio and only downscale oversized images.
    long_side = max(h, w)
    scale = min(1.0, OCR_MAX_LONG_SIDE / float(long_side))
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))

    resized = cv2.resize(
        img,
        (new_w, new_h),
        interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR,
    )

    if not OCR_ENABLE_PADDING:
        return resized

    pad_w = (
        OCR_PADDING_MULTIPLE - (new_w % OCR_PADDING_MULTIPLE)
    ) % OCR_PADDING_MULTIPLE
    pad_h = (
        OCR_PADDING_MULTIPLE - (new_h % OCR_PADDING_MULTIPLE)
    ) % OCR_PADDING_MULTIPLE
    if pad_w == 0 and pad_h == 0:
        return resized

    return cv2.copyMakeBorder(
        resized,
        0,
        pad_h,
        0,
        pad_w,
        cv2.BORDER_CONSTANT,
        value=(255, 255, 255),
    )


@router.post("/process")
async def process_ocr(
    image: UploadFile = File(...), current_user=Depends(get_current_user)
) -> Dict[str, Any]:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    model = get_ocr_model()
    if model is None:
        raise HTTPException(
            status_code=500, detail="OCR engine not available (paddleocr not installed)"
        )

    content = await image.read()
    img = _decode_uploaded_image(content)

    img = _resize_for_ocr(img)

    try:
        # OCR inference is CPU-bound and blocks; run it in threadpool to keep API responsive.
        result = await run_in_threadpool(_run_ocr, model, img)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {str(e)}")

    extracted_texts = _extract_ocr_texts(result)

    return {"ocr_output": extracted_texts}


@router.post("/process-aligned")
async def process_aligned_ocr(
    image: UploadFile = File(...),
    crop_points: str = Form(...),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    model = get_ocr_model()
    if model is None:
        raise HTTPException(
            status_code=500,
            detail="OCR engine not available (paddleocr not installed)",
        )

    content = await image.read()
    raw_image = _decode_uploaded_image(content)
    image_height, image_width = raw_image.shape[:2]

    parsed_points = _parse_crop_points(crop_points, image_width, image_height)
    aligned_image = _align_by_perspective(raw_image, parsed_points)

    ocr_input = _resize_for_ocr(aligned_image)
    try:
        result = await run_in_threadpool(_run_ocr, model, ocr_input)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {str(e)}")

    encoded_ok, encoded_image = cv2.imencode(".jpg", aligned_image)
    if not encoded_ok:
        raise HTTPException(status_code=500, detail="Failed to encode aligned image")

    return {
        "ocr_output": _extract_ocr_texts(result),
        "aligned_image_base64": base64.b64encode(encoded_image.tobytes()).decode(
            "ascii"
        ),
        "aligned_mime_type": "image/jpeg",
    }
