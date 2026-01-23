from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings."""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/instagram_db"
    redis_url: str = "redis://localhost:6379/0"
    debug: bool = False
    feed_limit_default: int = 20
    feed_days_limit: int = 7
    feed_cache_ttl: int = 60  # 1 minute
    user_cache_ttl: int = 300  # 5 minutes
    post_cache_ttl: int = 3600  # 1 hour
    push_threshold: int = 5000  # Max followers for fan-out-on-write

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings."""
    return Settings()
