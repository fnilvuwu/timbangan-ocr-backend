# Backend (FastAPI)

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload --port 8000
```

## Health Endpoints

- `GET /health`
- `GET /health/db`

## Gemma (Gemini) OCR

This project optionally supports Google Gemini (Gemma) as an OCR engine for 7-segment LED displays.

1. Install the dependency: `google-generativeai` is already listed in `requirements.txt`.
2. Add `GEMINI_API_KEY` to your `.env` (see `.env.example`).
3. Call the OCR endpoints with form field `ocr_engine=gemma` to use Gemini for OCR.

Note: Using Gemini will upload the image to the Google Generative AI service; ensure you have permissions and API quota.
