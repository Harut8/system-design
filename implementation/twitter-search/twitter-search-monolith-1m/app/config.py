"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "Twitter Search Monolith (1M)"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/twitter_search"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Search settings
    search_lookback_days: int = 7
    search_default_limit: int = 20
    search_max_limit: int = 200  # Higher limit for power users

    # Trending settings
    trending_window_hours: int = 1
    trending_refresh_seconds: int = 10  # Very frequent refresh
    trending_top_n: int = 200  # More trending topics

    # Autocomplete settings
    autocomplete_suggestions: int = 10  # More suggestions
    autocomplete_refresh_seconds: int = 60  # 1 minute - fastest refresh
    autocomplete_max_prefix_length: int = 25

    # Cache TTLs (seconds)
    cache_search_ttl: int = 120  # Aggressive caching for scale
    cache_trending_ttl: int = 20
    cache_autocomplete_ttl: int = 180

    # Rate limiting
    rate_limit_requests: int = 1000  # High rate limit for scale
    rate_limit_window: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
