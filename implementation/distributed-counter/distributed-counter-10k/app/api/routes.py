from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime
import time

from app.api.deps import get_session, check_rate_limit
from app.services.counter import CounterService
from app.schemas import (
    LikeRequest,
    UnlikeRequest,
    LikeResponse,
    CountRequest,
    CountResponse,
    BatchCountRequest,
    BatchCountResponse,
    HasLikedRequest,
    HasLikedResponse,
    HealthResponse,
    ItemType,
)

router = APIRouter()


@router.post("/v1/like", response_model=LikeResponse)
async def like_item(
    request: LikeRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Like an item. Idempotent - returns success if already liked.
    """
    # Rate limit check
    check_rate_limit(request.user_id)

    service = CounterService(db)
    status, count = await service.like(
        user_id=request.user_id,
        item_id=request.item_id,
        item_type=request.item_type,
    )

    return LikeResponse(
        status=status,
        count=count,
        user_liked=True,
    )


@router.delete("/v1/like", response_model=LikeResponse)
async def unlike_item(
    request: UnlikeRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Unlike an item. Returns success if not liked.
    """
    # Rate limit check
    check_rate_limit(request.user_id)

    service = CounterService(db)
    status, count = await service.unlike(
        user_id=request.user_id,
        item_id=request.item_id,
        item_type=request.item_type,
    )

    return LikeResponse(
        status=status,
        count=count,
        user_liked=False,
    )


@router.get("/v1/count", response_model=CountResponse)
async def get_count(
    item_id: int = Query(..., description="Item ID"),
    item_type: ItemType = Query(default="post", description="Item type"),
    db: AsyncSession = Depends(get_session),
):
    """
    Get like count for a single item.
    """
    service = CounterService(db)
    count = await service.get_count(item_id=item_id, item_type=item_type)

    return CountResponse(
        item_id=item_id,
        item_type=item_type,
        count=count,
        cached=False,
        as_of=datetime.utcnow(),
    )


@router.post("/v1/counts", response_model=BatchCountResponse)
async def get_counts_batch(
    request: BatchCountRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Get like counts for multiple items in a single request.
    """
    start_time = time.time()

    service = CounterService(db)
    items = [(item.item_id, item.item_type) for item in request.items]
    counts = await service.get_counts_batch(items)

    elapsed_ms = (time.time() - start_time) * 1000

    return BatchCountResponse(
        counts=counts,
        took_ms=round(elapsed_ms, 2),
    )


@router.post("/v1/has_liked", response_model=HasLikedResponse)
async def has_user_liked(
    request: HasLikedRequest,
    db: AsyncSession = Depends(get_session),
):
    """
    Check if user has liked multiple items.
    """
    service = CounterService(db)
    liked = await service.has_user_liked(
        user_id=request.user_id,
        item_ids=request.item_ids,
        item_type=request.item_type,
    )

    return HasLikedResponse(liked=liked)


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_session)):
    """
    Health check endpoint.
    """
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception:
        db_status = "unhealthy"

    return HealthResponse(
        status="ok" if db_status == "healthy" else "degraded",
        database=db_status,
    )
