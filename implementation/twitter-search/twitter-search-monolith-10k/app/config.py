"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "Twitter Search Monolith (10K)"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/twitter_search"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Search settings
    search_lookback_days: int = 7
    search_default_limit: int = 20
    search_max_limit: int = 100

    # Trending settings
    trending_window_hours: int = 1
    trending_refresh_seconds: int = 30
    trending_top_n: int = 50

    # Autocomplete settings
    autocomplete_suggestions: int = 3
    autocomplete_refresh_seconds: int = 300  # 5 minutes
    autocomplete_max_prefix_length: int = 15

    # Cache TTLs (seconds)
    cache_search_ttl: int = 30
    cache_trending_ttl: int = 60
    cache_autocomplete_ttl: int = 600

    # Rate limiting
    rate_limit_requests: int = 100
    rate_limit_window: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
