"""Autocomplete service using Redis sorted sets for prefix matching."""

from datetime import datetime, timedelta, timezone

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.schemas import AutocompleteResponse, AutocompleteSuggestion

settings = get_settings()


class AutocompleteService:
    """Service for autocomplete suggestions using Redis prefix index."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    async def get_suggestions(
        self,
        query: str,
        limit: int = 3,
    ) -> AutocompleteResponse:
        """
        Get autocomplete suggestions for a prefix.

        Uses Redis sorted sets for O(log n) prefix lookup.
        """
        if len(query) < 1:
            return AutocompleteResponse(suggestions=[], query=query)

        prefix = query.lower().strip()
        cache_key = f"ac:{prefix}"

        # Get top suggestions from Redis sorted set
        results = await self.redis.zrevrange(
            cache_key, 0, limit - 1, withscores=True
        )

        suggestions = [
            AutocompleteSuggestion(term=term, score=round(score, 2))
            for term, score in results
        ]

        return AutocompleteResponse(suggestions=suggestions, query=query)

    async def rebuild_autocomplete_index(self) -> int:
        """
        Rebuild the autocomplete index from multiple sources.

        Sources (in priority order):
        1. Trending terms (weight: 1.0)
        2. Popular search queries from last 24h (weight: 0.7)
        3. Popular hashtags (weight: 0.5)

        Called by background task every 5 minutes.
        """
        terms: dict[str, float] = {}

        # 1. Get trending terms (highest priority)
        trending = await self.redis.zrevrange("trending:global", 0, 100, withscores=True)
        for term, score in trending:
            normalized = term.lower().strip()
            terms[normalized] = max(terms.get(normalized, 0), score * 1.0)

        # 2. Get popular search queries from last 24 hours
        popular_queries = await self._get_popular_queries()
        for query, count in popular_queries:
            normalized = query.lower().strip()
            # Scale query counts to be comparable with trending
            terms[normalized] = max(terms.get(normalized, 0), count * 0.7)

        # 3. Get popular hashtags (all time, from recent tweets)
        popular_hashtags = await self._get_popular_hashtags()
        for hashtag, count in popular_hashtags:
            normalized = hashtag.lower().strip()
            terms[normalized] = max(terms.get(normalized, 0), count * 0.5)

        # Build prefix index in Redis
        pipe = self.redis.pipeline()

        # Track all prefixes to set TTL
        all_prefixes = set()

        for term, score in terms.items():
            if not term or len(term) < 2:
                continue

            # Create entries for all prefixes of this term
            max_prefix_len = min(len(term), settings.autocomplete_max_prefix_length)
            for i in range(1, max_prefix_len + 1):
                prefix = term[:i]
                cache_key = f"ac:{prefix}"
                pipe.zadd(cache_key, {term: score})
                all_prefixes.add(cache_key)

        # Set TTL for all prefix keys
        for cache_key in all_prefixes:
            pipe.expire(cache_key, settings.cache_autocomplete_ttl)

        await pipe.execute()

        return len(terms)

    async def _get_popular_queries(self, limit: int = 500) -> list[tuple[str, int]]:
        """Get popular search queries from the last 24 hours."""
        window_start = datetime.now(timezone.utc) - timedelta(hours=24)

        sql = text("""
            SELECT query, COUNT(*) as count
            FROM search_logs
            WHERE created_at >= :window_start
            GROUP BY query
            HAVING COUNT(*) >= 2
            ORDER BY count DESC
            LIMIT :limit
        """)

        result = await self.db.execute(
            sql,
            {"window_start": window_start, "limit": limit},
        )

        return [(row.query, row.count) for row in result.fetchall()]

    async def _get_popular_hashtags(self, limit: int = 200) -> list[tuple[str, int]]:
        """Get popular hashtags from recent tweets."""
        window_start = datetime.now(timezone.utc) - timedelta(days=7)

        sql = text("""
            SELECT LOWER(unnest(hashtags)) AS hashtag, COUNT(*) as count
            FROM tweets
            WHERE
                created_at >= :window_start
                AND is_deleted = false
                AND array_length(hashtags, 1) > 0
            GROUP BY LOWER(unnest(hashtags))
            ORDER BY count DESC
            LIMIT :limit
        """)

        result = await self.db.execute(
            sql,
            {"window_start": window_start, "limit": limit},
        )

        return [(row.hashtag, row.count) for row in result.fetchall()]

    async def add_term(self, term: str, score: float = 1.0) -> None:
        """
        Add a single term to the autocomplete index.

        Useful for real-time updates when new trending terms emerge.
        """
        normalized = term.lower().strip()
        if not normalized or len(normalized) < 2:
            return

        pipe = self.redis.pipeline()

        max_prefix_len = min(len(normalized), settings.autocomplete_max_prefix_length)
        for i in range(1, max_prefix_len + 1):
            prefix = normalized[:i]
            cache_key = f"ac:{prefix}"
            # Use ZADD with NX to not overwrite higher scores
            pipe.zadd(cache_key, {normalized: score}, nx=True)
            pipe.expire(cache_key, settings.cache_autocomplete_ttl)

        await pipe.execute()

    async def clear_index(self) -> int:
        """Clear the entire autocomplete index."""
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await self.redis.scan(cursor, match="ac:*", count=100)
            if keys:
                deleted += await self.redis.delete(*keys)
            if cursor == 0:
                break
        return deleted
