from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "FastAPI RBAC"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://user:pass@localhost/rbac_db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    PERMISSION_CACHE_TTL: int = 300  # 5 minutes

    # JWT
    SECRET_KEY: str = "your-secret-key-here"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # RBAC
    ENABLE_PERMISSION_CACHE: bool = True
    SUPERUSER_ROLE: str = "super_admin"
    DEFAULT_ROLE: str = "user"

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
