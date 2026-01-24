"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application - 10M Distributed Scale
    app_name: str = "Twitter Search Distributed (10M)"
    debug: bool = False

    # Database (Sharded PostgreSQL)
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/twitter_search"

    # Elasticsearch
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_index: str = "tweets"

    # Kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_tweets: str = "tweet-events"
    kafka_topic_index: str = "search-index"
    kafka_partitions: int = 256

    # Redis Cluster
    redis_url: str = "redis://localhost:6379/0"

    # Search settings
    search_lookback_days: int = 14  # Longer lookback for scale
    search_default_limit: int = 20
    search_max_limit: int = 500  # Higher limit for 10M scale

    # Trending settings
    trending_window_hours: int = 1
    trending_refresh_seconds: int = 5  # Very frequent refresh
    trending_top_n: int = 500  # Many trending topics

    # Autocomplete settings
    autocomplete_suggestions: int = 15  # More suggestions
    autocomplete_refresh_seconds: int = 30  # 30 seconds - fastest refresh
    autocomplete_max_prefix_length: int = 30

    # Cache TTLs (seconds)
    cache_search_ttl: int = 300  # 5 min - aggressive caching
    cache_trending_ttl: int = 10
    cache_autocomplete_ttl: int = 60

    # Rate limiting
    rate_limit_requests: int = 5000  # Very high rate limit for 10M scale
    rate_limit_window: int = 60

    # Worker settings
    index_batch_size: int = 1000
    index_flush_interval_seconds: int = 5

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
