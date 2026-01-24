"""Database connection and session management."""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# SQLAlchemy async engine
engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

# Session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


# Redis connection pool
redis_pool: redis.ConnectionPool | None = None
redis_client: redis.Redis | None = None


async def init_redis() -> redis.Redis:
    """Initialize Redis connection."""
    global redis_pool, redis_client
    redis_pool = redis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=50,
        decode_responses=True,
    )
    redis_client = redis.Redis(connection_pool=redis_pool)
    return redis_client


async def close_redis() -> None:
    """Close Redis connection."""
    global redis_pool, redis_client
    if redis_client:
        await redis_client.close()
    if redis_pool:
        await redis_pool.disconnect()


def get_redis() -> redis.Redis:
    """Get Redis client instance."""
    if redis_client is None:
        raise RuntimeError("Redis not initialized")
    return redis_client


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting database session."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for database session (for background tasks)."""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database tables and extensions."""
    async with engine.begin() as conn:
        # Create pg_trgm extension for fuzzy search (optional)
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        # Create tables
        await conn.run_sync(Base.metadata.create_all)
