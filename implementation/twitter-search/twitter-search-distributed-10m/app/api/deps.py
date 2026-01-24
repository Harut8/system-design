"""API dependencies for dependency injection."""

from typing import Annotated, AsyncGenerator

import redis.asyncio as redis
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, get_redis
from app.services.autocomplete import AutocompleteService
from app.services.search import SearchService
from app.services.trending import TrendingService
from app.services.tweet import TweetService


# Type aliases for cleaner dependency injection
DBSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[redis.Redis, Depends(get_redis)]


async def get_search_service(
    db: DBSession,
    redis_client: RedisClient,
) -> SearchService:
    """Get search service instance."""
    return SearchService(db, redis_client)


async def get_trending_service(
    db: DBSession,
    redis_client: RedisClient,
) -> TrendingService:
    """Get trending service instance."""
    return TrendingService(db, redis_client)


async def get_autocomplete_service(
    db: DBSession,
    redis_client: RedisClient,
) -> AutocompleteService:
    """Get autocomplete service instance."""
    return AutocompleteService(db, redis_client)


async def get_tweet_service(
    db: DBSession,
    redis_client: RedisClient,
) -> TweetService:
    """Get tweet service instance."""
    return TweetService(db, redis_client)


# Service type aliases
SearchServiceDep = Annotated[SearchService, Depends(get_search_service)]
TrendingServiceDep = Annotated[TrendingService, Depends(get_trending_service)]
AutocompleteServiceDep = Annotated[AutocompleteService, Depends(get_autocomplete_service)]
TweetServiceDep = Annotated[TweetService, Depends(get_tweet_service)]
