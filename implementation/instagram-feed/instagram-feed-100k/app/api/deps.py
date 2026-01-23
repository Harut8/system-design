from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

from app.database import get_db, get_redis
from app.services.feed import FeedService, PostService
from app.services.social import SocialService, InteractionService
from app.services.user import UserService


async def get_feed_service(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
) -> FeedService:
    """Dependency for FeedService."""
    return FeedService(db, redis_client)


async def get_post_service(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
) -> PostService:
    """Dependency for PostService."""
    return PostService(db, redis_client)


async def get_social_service(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
) -> SocialService:
    """Dependency for SocialService."""
    return SocialService(db, redis_client)


async def get_interaction_service(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
) -> InteractionService:
    """Dependency for InteractionService."""
    return InteractionService(db, redis_client)


async def get_user_service(
    db: AsyncSession = Depends(get_db),
    redis_client: redis.Redis = Depends(get_redis),
) -> UserService:
    """Dependency for UserService."""
    return UserService(db, redis_client)
