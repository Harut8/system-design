from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings."""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/instagram_db"
    debug: bool = False
    feed_limit_default: int = 20
    feed_days_limit: int = 7

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings."""
    return Settings()
