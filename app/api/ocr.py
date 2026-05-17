import os
import base64
import json
from typing import Any, Dict

import cv2
import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.transactions import get_current_user
from app.core.config import settings
from app.core.security import create_ocr_token
import re
import tempfile

# Work around Paddle CPU runtime issues on some Windows setups
# (OneDNN + PIR conversion errors during OCR execution).
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("FLAGS_enable_pir_api", "0")

try:
    from paddleocr import PaddleOCR
except ImportError:
    PaddleOCR = None

import shutil

try:
    import pytesseract

    tess_path = shutil.which("tesseract")
    if tess_path:
        pytesseract.pytesseract.tesseract_cmd = tess_path
    else:
        # Fallback for standard Windows installation
        pytesseract.pytesseract.tesseract_cmd = (
            r"C:\Program Files\Tesseract-OCR\tesseract.exe"
        )
except ImportError:
    pytesseract = None

router = APIRouter(prefix="/ocr", tags=["ocr"])

ocr_model = None
OCR_MAX_LONG_SIDE = 960
OCR_ENABLE_PADDING = False
OCR_PADDING_MULTIPLE = 32


def _get_int_param(params: dict, key: str, default: int, min_value: int = 0) -> int:
    try:
        value = int(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, value)


def _get_float_param(
    params: dict, key: str, default: float, min_value: float = 0.0
) -> float:
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(min_value, value)


def _get_bool_param(params: dict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


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


def _run_ocr(model, img, engine="paddle"):
    if engine == "tesseract":
        if pytesseract is None:
            raise RuntimeError("pytesseract is not installed")
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        custom_config = (
            r"--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789 "
            r"-c classify_bln_numeric_mode=1"
        )
        text = pytesseract.image_to_string(gray, config=custom_config)
        return [line.strip() for line in text.split("\n") if line.strip()]
    if engine == "gemma" or engine == "gemini":
        return _run_gemma_ocr(img)

    try:
        result = model.ocr(img, cls=False)
        return _extract_ocr_texts(result)
    except TypeError as e:
        if "unexpected keyword argument 'cls'" in str(e):
            result = model.ocr(img)
            return _extract_ocr_texts(result)
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


def extract_digits_from_gemma_output(text: str) -> str:
    cleaned = re.sub(r"[^0-9.]", "", text)
    cleaned = cleaned.replace(".", "")
    cleaned = cleaned.lstrip("0") or "0"
    return cleaned


def _postprocess_ocr_texts(texts: list[str], params: dict) -> list[str]:
    if not texts:
        return []

    digits_only = _get_bool_param(params, "postprocess_digits", True)
    keep_decimal = _get_bool_param(params, "keep_decimal", True)

    cleaned: list[str] = []
    for text in texts:
        text = text.strip()
        if not text:
            continue
        if digits_only:
            pattern = r"[^0-9.]" if keep_decimal else r"[^0-9]"
            text = re.sub(pattern, "", text)
            if not keep_decimal:
                text = text.replace(".", "")
        text = text.strip(".")
        if text:
            cleaned.append(text)

    return cleaned


def _run_gemma_ocr(img: np.ndarray) -> list[str]:
    api_key = settings.gemini_api_key or os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL") or settings.gemini_model
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set for gemma OCR engine")

    try:
        from google import genai
    except Exception as e:
        raise RuntimeError("google-genai is not installed") from e

    client = genai.Client(api_key=api_key)

    prompt = """
You are an OCR system specialized in 7-segment LED displays.

Read only the illuminated digits shown on the display.
Ignore labels, buttons, and background.
Ignore decimal points.
Return ONLY the digits.
Do not include explanations.

Examples:
4.100 -> 4100
03.40 -> 340
000123 -> 123
"""

    # Save image to a temporary file and upload
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        cv2.imwrite(tmp_path, img)
        uploaded = client.files.upload(file=tmp_path)
        response = client.models.generate_content(
            model=model_name,
            contents=[uploaded, prompt],
        )

        raw_text = getattr(response, "text", None)
        if raw_text is None:
            raw_text = str(response)

        digits = extract_digits_from_gemma_output(raw_text.strip())
        return [digits] if digits else []
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass


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


def _preprocess_scale_display(img: np.ndarray, params: dict = None) -> np.ndarray:
    """
    Convert red 7-segment LED display into clean black digits on white background,
    similar to your desired fifth image.
    """
    if params is None:
        params = {}

    # ---------- Parameters ----------
    scale_factor = _get_float_param(params, "scale_factor", 4.0, min_value=1.0)
    hsv_s_min = _get_int_param(params, "hsv_s_min", 80, min_value=0)
    hsv_v_min = _get_int_param(params, "hsv_v_min", 40, min_value=0)
    keep_decimal = _get_bool_param(params, "keep_decimal", True)
    invert_output = _get_bool_param(params, "invert_output", True)
    apply_open = _get_bool_param(params, "apply_open", False)
    blur_kernel = _get_int_param(params, "blur_kernel", 5, min_value=1)
    close_kernel = _get_int_param(params, "close_kernel", 5, min_value=1)
    open_kernel = _get_int_param(params, "open_kernel", 3, min_value=1)

    # ---------- 1. Upscale ----------
    img = cv2.resize(
        img,
        None,
        fx=scale_factor,
        fy=scale_factor,
        interpolation=cv2.INTER_CUBIC,
    )

    # ---------- 2. Extract red channel in HSV ----------
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    lower_red1 = np.array([0, hsv_s_min, hsv_v_min])
    upper_red1 = np.array([10, 255, 255])

    lower_red2 = np.array([170, hsv_s_min, hsv_v_min])
    upper_red2 = np.array([180, 255, 255])

    mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
    mask2 = cv2.inRange(hsv, lower_red2, upper_red2)

    mask = cv2.bitwise_or(mask1, mask2)

    # ---------- 3. Blur ----------
    if blur_kernel % 2 == 0:
        blur_kernel += 1
    mask = cv2.GaussianBlur(mask, (blur_kernel, blur_kernel), 0)

    # ---------- 4. Binary threshold ----------
    _, binary = cv2.threshold(mask, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # ---------- 5. Morphological closing ----------
    kernel_close = cv2.getStructuringElement(
        cv2.MORPH_RECT, (close_kernel, close_kernel)
    )
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel_close)

    # ---------- 6. Morphological opening ----------
    if apply_open:
        kernel_open = cv2.getStructuringElement(
            cv2.MORPH_RECT, (open_kernel, open_kernel)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel_open)

    # ---------- 7. Remove small connected components ----------
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )

    cleaned = np.zeros_like(binary)
    default_min_area = max(30, int(0.0002 * binary.size))
    min_component_area = _get_int_param(
        params, "min_component_area", default_min_area, min_value=1
    )
    decimal_max_size = _get_int_param(
        params, "decimal_max_size", max(2, int(min(binary.shape) * 0.04)), min_value=1
    )

    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        w = stats[i, cv2.CC_STAT_WIDTH]
        h = stats[i, cv2.CC_STAT_HEIGHT]

        # Keep only significant components
        if area >= min_component_area:
            # Ignore tiny decimal dots if requested
            if not keep_decimal and w < decimal_max_size and h < decimal_max_size:
                continue
            cleaned[labels == i] = 255

    # ---------- 8. Final smoothing ----------
    kernel_final = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel_final)

    # ---------- 9. Invert -> black digits on white background ----------
    final = 255 - cleaned if invert_output else cleaned

    # ---------- 10. Convert to BGR ----------
    return cv2.cvtColor(final, cv2.COLOR_GRAY2BGR)


@router.post("/process")
async def process_ocr(
    image: UploadFile = File(...),
    enable_preprocessing: bool = Form(True),
    preprocess_params: str = Form("{}"),
    ocr_engine: str = Form("paddle"),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    try:
        params = json.loads(preprocess_params)
    except json.JSONDecodeError:
        params = {}

    if ocr_engine == "paddle":
        model = get_ocr_model()
        if model is None:
            raise HTTPException(
                status_code=500,
                detail="OCR engine not available (paddleocr not installed)",
            )
    elif ocr_engine == "tesseract":
        model = None
        if pytesseract is None:
            raise HTTPException(
                status_code=500,
                detail="OCR engine not available (pytesseract not installed)",
            )
    elif ocr_engine in ("gemma", "gemini"):
        model = None
        if not (settings.gemini_api_key or os.getenv("GEMINI_API_KEY")):
            raise HTTPException(
                status_code=500,
                detail="OCR engine not available (GEMINI_API_KEY not set)",
            )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown ocr_engine: {ocr_engine}")

    content = await image.read()
    img = _decode_uploaded_image(content)

    img = _resize_for_ocr(img)

    if enable_preprocessing:
        img = _preprocess_scale_display(img, params)

    try:
        # OCR inference is CPU-bound and blocks; run it in threadpool to keep API responsive.
        extracted_texts = await run_in_threadpool(_run_ocr, model, img, ocr_engine)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {str(e)}")

    extracted_texts = _postprocess_ocr_texts(extracted_texts, params)

    encoded_ok, encoded_image = cv2.imencode(".jpg", img)
    if encoded_ok:
        processed_image_base64 = base64.b64encode(encoded_image.tobytes()).decode(
            "ascii"
        )
    else:
        processed_image_base64 = None

    return {
        "ocr_output": extracted_texts,
        "ocr_token": create_ocr_token(extracted_texts),
        "processed_image_base64": processed_image_base64,
        "processed_mime_type": "image/jpeg" if processed_image_base64 else None,
    }


@router.post("/process-aligned")
async def process_aligned_ocr(
    image: UploadFile = File(...),
    crop_points: str = Form(...),
    enable_preprocessing: bool = Form(True),
    preprocess_params: str = Form("{}"),
    ocr_engine: str = Form("paddle"),
    current_user=Depends(get_current_user),
) -> Dict[str, Any]:
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    try:
        params = json.loads(preprocess_params)
    except json.JSONDecodeError:
        params = {}

    if ocr_engine == "paddle":
        model = get_ocr_model()
        if model is None:
            raise HTTPException(
                status_code=500,
                detail="OCR engine not available (paddleocr not installed)",
            )
    elif ocr_engine == "tesseract":
        model = None
        if pytesseract is None:
            raise HTTPException(
                status_code=500,
                detail="OCR engine not available (pytesseract not installed)",
            )
    elif ocr_engine in ("gemma", "gemini"):
        model = None
        if not (settings.gemini_api_key or os.getenv("GEMINI_API_KEY")):
            raise HTTPException(
                status_code=500,
                detail="OCR engine not available (GEMINI_API_KEY not set)",
            )
    else:
        raise HTTPException(status_code=400, detail=f"Unknown ocr_engine: {ocr_engine}")

    content = await image.read()
    raw_image = _decode_uploaded_image(content)
    image_height, image_width = raw_image.shape[:2]

    parsed_points = _parse_crop_points(crop_points, image_width, image_height)
    aligned_image = _align_by_perspective(raw_image, parsed_points)

    ocr_input = _resize_for_ocr(aligned_image)
    if enable_preprocessing:
        ocr_input = _preprocess_scale_display(ocr_input, params)

    try:
        extracted_texts = await run_in_threadpool(
            _run_ocr, model, ocr_input, ocr_engine
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR processing failed: {str(e)}")

    extracted_texts = _postprocess_ocr_texts(extracted_texts, params)

    encoded_ok, encoded_image = cv2.imencode(".jpg", aligned_image)
    if not encoded_ok:
        raise HTTPException(status_code=500, detail="Failed to encode aligned image")

    proc_encoded_ok, proc_encoded_image = cv2.imencode(".jpg", ocr_input)

    return {
        "ocr_output": extracted_texts,
        "ocr_token": create_ocr_token(extracted_texts),
        "aligned_image_base64": base64.b64encode(encoded_image.tobytes()).decode(
            "ascii"
        ),
        "aligned_mime_type": "image/jpeg",
        "processed_image_base64": (
            base64.b64encode(proc_encoded_image.tobytes()).decode("ascii")
            if proc_encoded_ok
            else None
        ),
        "processed_mime_type": "image/jpeg" if proc_encoded_ok else None,
    }
