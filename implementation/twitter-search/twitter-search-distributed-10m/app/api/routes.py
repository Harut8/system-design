"""API routes for Twitter Search."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import (
    AutocompleteServiceDep,
    SearchServiceDep,
    TrendingServiceDep,
    TweetServiceDep,
)
from app.config import get_settings
from app.schemas import (
    AutocompleteResponse,
    EngagementResponse,
    EngagementUpdate,
    ErrorResponse,
    SearchResponse,
    TrendingResponse,
    TweetCreate,
    TweetResponse,
)

settings = get_settings()
router = APIRouter()


# --- Search Endpoints ---


@router.get(
    "/v1/search",
    response_model=SearchResponse,
    summary="Search tweets",
    description="Search tweets using full-text search with AND logic",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid query"},
    },
)
async def search_tweets(
    search_service: SearchServiceDep,
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    user_id: Optional[str] = Query(None, max_length=32, description="Filter by user ID"),
    language: Optional[str] = Query(None, max_length=5, description="Filter by language"),
    since: Optional[datetime] = Query(None, description="Start of time range"),
    until: Optional[datetime] = Query(None, description="End of time range"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
) -> SearchResponse:
    """
    Search tweets with full-text search.

    - Uses PostgreSQL full-text search with AND logic (all terms must match)
    - Results ranked by relevance, recency, and engagement
    - Cached for 30 seconds for performance
    """
    return await search_service.search(
        query=q,
        user_id=user_id,
        language=language,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )


# --- Trending Endpoints ---


@router.get(
    "/v1/trending",
    response_model=TrendingResponse,
    summary="Get trending topics",
    description="Get trending hashtags from the last hour",
)
async def get_trending(
    trending_service: TrendingServiceDep,
    region: str = Query("global", max_length=20, description="Region for trends"),
    limit: int = Query(10, ge=1, le=50, description="Number of trends to return"),
) -> TrendingResponse:
    """
    Get trending topics.

    - Shows most popular hashtags from the last hour
    - Updated every 30 seconds by background task
    - Supports regional filtering (future enhancement)
    """
    return await trending_service.get_trending(region=region, limit=limit)


@router.get(
    "/v1/trending/{term}/velocity",
    summary="Get trend velocity",
    description="Get hourly tweet counts for a trending term",
)
async def get_trend_velocity(
    trending_service: TrendingServiceDep,
    term: str,
    hours: int = Query(24, ge=1, le=168, description="Hours of history"),
) -> list[dict]:
    """Get hourly tweet counts for a term to show trend trajectory."""
    return await trending_service.get_trending_velocity(term, hours)


# --- Autocomplete Endpoints ---


@router.get(
    "/v1/autocomplete",
    response_model=AutocompleteResponse,
    summary="Get autocomplete suggestions",
    description="Get search suggestions based on prefix",
)
async def get_autocomplete(
    autocomplete_service: AutocompleteServiceDep,
    q: str = Query(..., min_length=1, max_length=100, description="Query prefix"),
    limit: int = Query(3, ge=1, le=10, description="Number of suggestions"),
) -> AutocompleteResponse:
    """
    Get autocomplete suggestions.

    - Based on trending terms, popular queries, and hashtags
    - Updated every 5 minutes by background task
    - Returns top 3 suggestions by default
    """
    return await autocomplete_service.get_suggestions(query=q, limit=limit)


# --- Tweet Endpoints ---


@router.post(
    "/v1/tweets",
    response_model=TweetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tweet",
    description="Create a new tweet",
)
async def create_tweet(
    tweet_service: TweetServiceDep,
    tweet_data: TweetCreate,
) -> TweetResponse:
    """
    Create a new tweet.

    - Automatically extracts hashtags from text
    - Indexed for full-text search immediately
    """
    return await tweet_service.create_tweet(tweet_data)


@router.get(
    "/v1/tweets/{tweet_id}",
    response_model=TweetResponse,
    summary="Get a tweet",
    description="Get a tweet by ID",
    responses={
        404: {"model": ErrorResponse, "description": "Tweet not found"},
    },
)
async def get_tweet(
    tweet_service: TweetServiceDep,
    tweet_id: str,
) -> TweetResponse:
    """Get a single tweet by ID."""
    tweet = await tweet_service.get_tweet(tweet_id)
    if not tweet:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tweet not found",
        )
    return tweet


@router.delete(
    "/v1/tweets/{tweet_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tweet",
    description="Soft delete a tweet",
    responses={
        404: {"model": ErrorResponse, "description": "Tweet not found"},
    },
)
async def delete_tweet(
    tweet_service: TweetServiceDep,
    tweet_id: str,
) -> None:
    """
    Delete a tweet (soft delete).

    - Tweet is marked as deleted immediately
    - Hard deleted after 24 hours by cleanup job
    """
    deleted = await tweet_service.delete_tweet(tweet_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tweet not found",
        )


@router.get(
    "/v1/users/{user_id}/tweets",
    response_model=list[TweetResponse],
    summary="Get user tweets",
    description="Get tweets by a specific user",
)
async def get_user_tweets(
    tweet_service: TweetServiceDep,
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[TweetResponse]:
    """Get tweets by a specific user."""
    return await tweet_service.get_user_tweets(user_id, limit, offset)


# --- Engagement Endpoints ---


@router.post(
    "/v1/tweets/{tweet_id}/engagement",
    response_model=EngagementResponse,
    summary="Update engagement",
    description="Like, unlike, retweet, or record a view",
    responses={
        404: {"model": ErrorResponse, "description": "Tweet not found"},
    },
)
async def update_engagement(
    tweet_service: TweetServiceDep,
    tweet_id: str,
    engagement: EngagementUpdate,
) -> EngagementResponse:
    """
    Update engagement metrics for a tweet.

    Actions:
    - like: Increment like count
    - unlike: Decrement like count
    - retweet: Increment retweet count
    - unretweet: Decrement retweet count
    - view: Buffer view count (flushed every 10 seconds)
    """
    result = None

    if engagement.action == "like":
        result = await tweet_service.increment_like(tweet_id)
    elif engagement.action == "unlike":
        result = await tweet_service.decrement_like(tweet_id)
    elif engagement.action == "retweet":
        result = await tweet_service.increment_retweet(tweet_id)
    elif engagement.action == "view":
        await tweet_service.increment_view(tweet_id)
        # For views, return current state
        tweet = await tweet_service.get_tweet(tweet_id)
        if tweet:
            result = EngagementResponse(
                tweet_id=tweet_id,
                likes=tweet.likes,
                retweets=tweet.retweets,
                views=tweet.views,
            )

    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tweet not found",
        )

    return result
