from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.feed import FeedService, PostService
from app.services.social import SocialService, InteractionService
from app.services.user import UserService


async def get_feed_service(db: AsyncSession = Depends(get_db)) -> FeedService:
    """Dependency for FeedService."""
    return FeedService(db)


async def get_post_service(db: AsyncSession = Depends(get_db)) -> PostService:
    """Dependency for PostService."""
    return PostService(db)


async def get_social_service(db: AsyncSession = Depends(get_db)) -> SocialService:
    """Dependency for SocialService."""
    return SocialService(db)


async def get_interaction_service(db: AsyncSession = Depends(get_db)) -> InteractionService:
    """Dependency for InteractionService."""
    return InteractionService(db)


async def get_user_service(db: AsyncSession = Depends(get_db)) -> UserService:
    """Dependency for UserService."""
    return UserService(db)
