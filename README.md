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
