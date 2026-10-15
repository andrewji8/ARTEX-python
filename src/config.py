from typing import Optional
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/artex_db"
    db_echo: bool = False

    # App
    app_name: str = "ARTEX Python"
    debug: bool = False

    # JWT
    jwt_secret_key: str = "change-me-in-production-use-a-real-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24 hours

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

