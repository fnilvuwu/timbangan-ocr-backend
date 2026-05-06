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

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


settings = Settings()
