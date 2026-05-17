from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ScaleScan API"
    app_env: str = "development"
    database_url: str = "sqlite:///./scalescan.db"
    upload_dir: str = "uploads"
    camera_only_mode: bool = False
    secret_key: str = "dev-only-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24
    frontend_origin: str = "http://localhost:3000"
    # Optional API key for Google Gemini (Gemma) OCR engine
    gemini_api_key: Optional[str] = None
    # Default Gemini model to use; can be overridden via env var GEMINI_MODEL
    gemini_model: str = "gemma-4-31b-it"
    admin_email: str = "admin@email.com"
    admin_password: str = "12345678"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
