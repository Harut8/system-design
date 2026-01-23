from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"
    post_service_url: str = "http://localhost:8002"
    feed_cache_ttl: int = 60
    celebrity_threshold: int = 10000

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
