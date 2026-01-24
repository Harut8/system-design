from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.database import get_db, get_redis
from app.config import get_settings

settings = get_settings()


async def get_session() -> AsyncSession:
    async for session in get_db():
        yield session


async def get_redis_client() -> redis.Redis:
    return await get_redis()


async def check_rate_limit(user_id: int, redis_client: redis.Redis) -> None:
    """Check rate limit using Redis."""
    key = f"rate:{user_id}:likes"

    current = await redis_client.incr(key)
    if current == 1:
        await redis_client.expire(key, settings.rate_limit_window_seconds)

    if current > settings.rate_limit_requests:
        ttl = await redis_client.ttl(key)
        raise HTTPException(
            status_code=429,
            detail={"error": "rate_limited", "retry_after": ttl},
        )
