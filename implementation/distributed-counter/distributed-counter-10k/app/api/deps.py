from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from collections import defaultdict
import time

from app.database import get_db
from app.config import get_settings

settings = get_settings()

# Simple in-memory rate limiter
# In production, use Redis for distributed rate limiting
rate_limit_store: dict[int, list[float]] = defaultdict(list)


async def get_session() -> AsyncSession:
    """Get database session."""
    async for session in get_db():
        yield session


def check_rate_limit(user_id: int) -> None:
    """Check if user is rate limited."""
    current_time = time.time()
    window_start = current_time - settings.rate_limit_window_seconds

    # Clean old entries
    rate_limit_store[user_id] = [
        t for t in rate_limit_store[user_id] if t > window_start
    ]

    # Check limit
    if len(rate_limit_store[user_id]) >= settings.rate_limit_requests:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "retry_after": settings.rate_limit_window_seconds,
            },
        )

    # Record request
    rate_limit_store[user_id].append(current_time)
