from fastapi import APIRouter, Query
from typing import Optional
import redis.asyncio as redis

from app.config import get_settings
from app.services.feed import FeedService

settings = get_settings()
router = APIRouter()

redis_client = redis.from_url(settings.redis_url, decode_responses=True)
feed_service = FeedService(redis_client)


@router.get("/health")
async def health():
    try:
        await redis_client.ping()
        return {"status": "healthy", "service": "feed-service", "tier": "10M"}
    except Exception:
        return {"status": "unhealthy", "service": "feed-service"}


@router.get("/v1/feed")
async def get_feed(
    user_id: int = Query(...),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: Optional[str] = Query(default=None),
):
    """Get personalized feed for a user."""
    result = await feed_service.get_feed(user_id, limit, cursor)
    return result
