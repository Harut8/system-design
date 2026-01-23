from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings."""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/instagram_db"
    database_url_sync: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/instagram_db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/1"
    debug: bool = False
    feed_limit_default: int = 20
    feed_days_limit: int = 7
    feed_cache_ttl: int = 60
    user_cache_ttl: int = 300
    post_cache_ttl: int = 3600
    celebrity_threshold: int = 10000
    fan_out_batch_size: int = 1000

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings."""
    return Settings()
