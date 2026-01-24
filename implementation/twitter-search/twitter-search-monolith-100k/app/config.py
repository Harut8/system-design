"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "Twitter Search Monolith (100K)"
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
    trending_refresh_seconds: int = 15  # More frequent refresh for higher volume
    trending_top_n: int = 100  # More trending topics

    # Autocomplete settings
    autocomplete_suggestions: int = 5  # More suggestions
    autocomplete_refresh_seconds: int = 180  # 3 minutes - faster refresh
    autocomplete_max_prefix_length: int = 20

    # Cache TTLs (seconds)
    cache_search_ttl: int = 60  # Longer cache for scale
    cache_trending_ttl: int = 30
    cache_autocomplete_ttl: int = 300

    # Rate limiting
    rate_limit_requests: int = 500  # Higher rate limit
    rate_limit_window: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
