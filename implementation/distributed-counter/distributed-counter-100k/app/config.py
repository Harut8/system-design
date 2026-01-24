from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings."""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/counter_db"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Application
    debug: bool = False

    # Cache settings
    cache_ttl_seconds: int = 3600  # 1 hour
    recent_likes_ttl_seconds: int = 86400  # 24 hours
    recent_likes_max_size: int = 1000

    # Rate limiting
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
