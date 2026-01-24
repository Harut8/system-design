from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis
from datetime import datetime
import time

from app.api.deps import get_session, get_redis_client, check_rate_limit
from app.services.counter import CounterService
from app.schemas import (
    LikeRequest, UnlikeRequest, LikeResponse,
    CountRequest, CountResponse, BatchCountRequest, BatchCountResponse,
    HasLikedRequest, HasLikedResponse, HealthResponse, ItemType,
)

router = APIRouter()


@router.post("/v1/like", response_model=LikeResponse)
async def like_item(
    request: LikeRequest,
    db: AsyncSession = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """Like an item (async via Kafka)."""
    await check_rate_limit(request.user_id, redis_client)

    service = CounterService(db, redis_client)
    status, count = await service.like(
        user_id=request.user_id,
        item_id=request.item_id,
        item_type=request.item_type,
    )

    return LikeResponse(status=status, count=count, user_liked=True)


@router.delete("/v1/like", response_model=LikeResponse)
async def unlike_item(
    request: UnlikeRequest,
    db: AsyncSession = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """Unlike an item (async via Kafka)."""
    await check_rate_limit(request.user_id, redis_client)

    service = CounterService(db, redis_client)
    status, count = await service.unlike(
        user_id=request.user_id,
        item_id=request.item_id,
        item_type=request.item_type,
    )

    return LikeResponse(status=status, count=count, user_liked=False)


@router.get("/v1/count", response_model=CountResponse)
async def get_count(
    item_id: int = Query(...),
    item_type: ItemType = Query(default="post"),
    db: AsyncSession = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """Get like count (with caching)."""
    service = CounterService(db, redis_client)
    count, cached = await service.get_count_cached(item_id=item_id, item_type=item_type)

    return CountResponse(
        item_id=item_id,
        item_type=item_type,
        count=count,
        cached=cached,
        as_of=datetime.utcnow(),
    )


@router.post("/v1/counts", response_model=BatchCountResponse)
async def get_counts_batch(
    request: BatchCountRequest,
    db: AsyncSession = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """Get counts for multiple items."""
    start_time = time.time()

    service = CounterService(db, redis_client)
    items = [(item.item_id, item.item_type) for item in request.items]
    counts = await service.get_counts_batch(items)

    elapsed_ms = (time.time() - start_time) * 1000

    return BatchCountResponse(counts=counts, took_ms=round(elapsed_ms, 2))


@router.post("/v1/has_liked", response_model=HasLikedResponse)
async def has_user_liked(
    request: HasLikedRequest,
    db: AsyncSession = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """Check if user has liked items."""
    service = CounterService(db, redis_client)
    liked = await service.has_user_liked(
        user_id=request.user_id,
        item_ids=request.item_ids,
        item_type=request.item_type,
    )

    return HasLikedResponse(liked=liked)


@router.get("/health", response_model=HealthResponse)
async def health_check(
    db: AsyncSession = Depends(get_session),
    redis_client: redis.Redis = Depends(get_redis_client),
):
    """Health check."""
    from sqlalchemy import text

    try:
        await db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception:
        db_status = "unhealthy"

    try:
        await redis_client.ping()
        redis_status = "healthy"
    except Exception:
        redis_status = "unhealthy"

    # Kafka health is assumed healthy if producer initialized
    kafka_status = "healthy"

    status = "ok" if all(s == "healthy" for s in [db_status, redis_status, kafka_status]) else "degraded"

    return HealthResponse(status=status, database=db_status, redis=redis_status, kafka=kafka_status)
