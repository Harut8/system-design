"""Trending topics service using PostgreSQL aggregation and Redis caching."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.schemas import TrendingItem, TrendingResponse

settings = get_settings()


class TrendingService:
    """Service for computing and caching trending topics."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    async def get_trending(
        self,
        region: str = "global",
        limit: int = 10,
    ) -> TrendingResponse:
        """
        Get trending topics from cache.

        Cache is populated by background task running every 30 seconds.
        Falls back to direct query if cache is empty.
        """
        cache_key = f"trending:{region}"

        # Try to get from cache first
        cached_trends = await self.redis.zrevrange(
            cache_key, 0, limit - 1, withscores=True
        )

        if cached_trends:
            trends = [
                TrendingItem(
                    term=term,
                    tweet_count=int(score),
                    rank=idx + 1,
                )
                for idx, (term, score) in enumerate(cached_trends)
            ]

            # Get the timestamp of last refresh
            as_of_str = await self.redis.get(f"{cache_key}:updated_at")
            as_of = (
                datetime.fromisoformat(as_of_str)
                if as_of_str
                else datetime.now(timezone.utc)
            )

            return TrendingResponse(
                trends=trends,
                region=region,
                as_of=as_of,
            )

        # Cache miss - compute directly (slower, but provides fallback)
        return await self.compute_trending(region, limit)

    async def compute_trending(
        self,
        region: str = "global",
        limit: int = 50,
    ) -> TrendingResponse:
        """
        Compute trending topics directly from database.

        This query:
        1. Gets tweets from the last hour
        2. Unnests hashtags and counts occurrences
        3. Orders by count descending
        """
        window_start = datetime.now(timezone.utc) - timedelta(
            hours=settings.trending_window_hours
        )

        # Query to aggregate hashtags from recent tweets
        sql = text("""
            SELECT
                LOWER(unnest(hashtags)) AS hashtag,
                COUNT(*) AS tweet_count
            FROM tweets
            WHERE
                created_at >= :window_start
                AND is_deleted = false
                AND array_length(hashtags, 1) > 0
            GROUP BY LOWER(unnest(hashtags))
            ORDER BY tweet_count DESC
            LIMIT :limit
        """)

        result = await self.db.execute(
            sql,
            {"window_start": window_start, "limit": limit},
        )

        rows = result.fetchall()

        trends = [
            TrendingItem(
                term=row.hashtag,
                tweet_count=row.tweet_count,
                rank=idx + 1,
            )
            for idx, row in enumerate(rows)
        ]

        return TrendingResponse(
            trends=trends,
            region=region,
            as_of=datetime.now(timezone.utc),
        )

    async def refresh_trending_cache(self, region: str = "global") -> int:
        """
        Refresh trending topics cache.

        Called by background task every 30 seconds.
        Returns number of trends cached.
        """
        # Compute fresh trending data
        trending = await self.compute_trending(region, limit=settings.trending_top_n)

        cache_key = f"trending:{region}"

        # Use pipeline for atomic update
        pipe = self.redis.pipeline()

        # Clear existing data
        pipe.delete(cache_key)

        # Add new trending data
        if trending.trends:
            # Use ZADD with score = tweet_count for sorting
            pipe.zadd(
                cache_key,
                {trend.term: trend.tweet_count for trend in trending.trends},
            )

        # Set TTL
        pipe.expire(cache_key, settings.cache_trending_ttl * 2)  # 2x TTL for safety

        # Store last update timestamp
        pipe.set(
            f"{cache_key}:updated_at",
            datetime.now(timezone.utc).isoformat(),
            ex=settings.cache_trending_ttl * 2,
        )

        await pipe.execute()

        return len(trending.trends)

    async def get_trending_velocity(
        self,
        term: str,
        hours: int = 24,
    ) -> list[dict]:
        """
        Get hourly tweet counts for a trending term.

        Useful for showing trend velocity/trajectory.
        """
        window_start = datetime.now(timezone.utc) - timedelta(hours=hours)

        sql = text("""
            SELECT
                date_trunc('hour', created_at) AS hour,
                COUNT(*) AS count
            FROM tweets
            WHERE
                :term = ANY(hashtags)
                AND created_at >= :window_start
                AND is_deleted = false
            GROUP BY date_trunc('hour', created_at)
            ORDER BY hour
        """)

        result = await self.db.execute(
            sql,
            {"term": term.lower(), "window_start": window_start},
        )

        return [
            {"hour": row.hour.isoformat(), "count": row.count}
            for row in result.fetchall()
        ]
