"""Search service with PostgreSQL full-text search and Redis caching."""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import redis.asyncio as redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import SearchLog
from app.schemas import SearchResponse, TweetSearchResult

settings = get_settings()


class SearchService:
    """Service for searching tweets using PostgreSQL full-text search."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    def _cache_key(self, query: str, filters: dict) -> str:
        """Generate cache key for search query."""
        filter_str = json.dumps(filters, sort_keys=True, default=str)
        key_data = f"{query}:{filter_str}"
        return f"search:{hashlib.md5(key_data.encode()).hexdigest()}"

    async def search(
        self,
        query: str,
        user_id: Optional[str] = None,
        language: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResponse:
        """
        Search tweets using PostgreSQL full-text search.

        The search uses:
        1. ts_rank for relevance scoring
        2. Time decay for recency boost
        3. Engagement signals for popularity boost
        """
        start_time = time.time()

        # Build filters dict for caching
        filters = {
            "user_id": user_id,
            "language": language,
            "since": since,
            "until": until,
            "limit": limit,
            "offset": offset,
        }

        # Check cache first
        cache_key = self._cache_key(query, filters)
        cached = await self.redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            # Update took_ms for cache hit
            data["took_ms"] = int((time.time() - start_time) * 1000)
            return SearchResponse(**data)

        # Build the search query
        results, total = await self._execute_search(
            query=query,
            user_id=user_id,
            language=language,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

        took_ms = int((time.time() - start_time) * 1000)

        # Build response
        response = SearchResponse(
            results=results,
            total=total,
            took_ms=took_ms,
            query=query,
            next_offset=offset + limit if offset + limit < total else None,
        )

        # Cache results (short TTL for freshness)
        await self.redis.setex(
            cache_key,
            settings.cache_search_ttl,
            response.model_dump_json(),
        )

        # Log search asynchronously (fire and forget style within same session)
        await self._log_search(query, user_id, len(results), took_ms)

        return response

    async def _execute_search(
        self,
        query: str,
        user_id: Optional[str],
        language: Optional[str],
        since: Optional[datetime],
        until: Optional[datetime],
        limit: int,
        offset: int,
    ) -> tuple[list[TweetSearchResult], int]:
        """Execute the actual search query against PostgreSQL."""

        # Default time range: last 7 days
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=settings.search_lookback_days)
        if until is None:
            until = datetime.now(timezone.utc)

        # Build the raw SQL for full-text search with ranking
        # Using plainto_tsquery for AND behavior (all terms must match)
        sql = text("""
            WITH search_results AS (
                SELECT
                    t.tweet_id,
                    t.user_id,
                    t.text,
                    t.hashtags,
                    t.language,
                    t.geo_lat,
                    t.geo_lon,
                    t.likes,
                    t.retweets,
                    t.views,
                    t.created_at,
                    ts_rank(t.search_vector, plainto_tsquery('english', :query)) AS relevance,
                    (
                        -- Relevance score (30%)
                        ts_rank(t.search_vector, plainto_tsquery('english', :query)) * 0.3 +
                        -- Recency score (40%) - exponential decay with 6hr half-life
                        (1.0 / (1 + EXTRACT(EPOCH FROM NOW() - t.created_at) / 21600)) * 0.4 +
                        -- Engagement score (30%) - log-scaled
                        (
                            LN(1 + t.likes) * 0.012 +
                            LN(1 + t.retweets) * 0.012 +
                            LN(1 + t.views / 1000.0) * 0.006
                        ) * 0.3
                    ) AS score
                FROM tweets t
                WHERE
                    t.search_vector @@ plainto_tsquery('english', :query)
                    AND t.is_deleted = false
                    AND t.created_at >= :since
                    AND t.created_at <= :until
                    AND (:user_id IS NULL OR t.user_id = :user_id)
                    AND (:language IS NULL OR t.language = :language)
            )
            SELECT *, COUNT(*) OVER() AS total_count
            FROM search_results
            ORDER BY score DESC, created_at DESC
            LIMIT :limit
            OFFSET :offset
        """)

        result = await self.db.execute(
            sql,
            {
                "query": query,
                "since": since,
                "until": until,
                "user_id": user_id,
                "language": language,
                "limit": limit,
                "offset": offset,
            },
        )

        rows = result.fetchall()

        if not rows:
            return [], 0

        # Extract total from first row
        total = rows[0].total_count if rows else 0

        # Convert to response objects
        results = [
            TweetSearchResult(
                tweet_id=row.tweet_id,
                user_id=row.user_id,
                text=row.text,
                hashtags=row.hashtags or [],
                language=row.language,
                geo_lat=float(row.geo_lat) if row.geo_lat else None,
                geo_lon=float(row.geo_lon) if row.geo_lon else None,
                likes=row.likes,
                retweets=row.retweets,
                views=row.views,
                created_at=row.created_at,
                relevance=float(row.relevance) if row.relevance else None,
                score=float(row.score) if row.score else None,
            )
            for row in rows
        ]

        return results, total

    async def _log_search(
        self, query: str, user_id: Optional[str], results_count: int, latency_ms: int
    ) -> None:
        """Log search query for analytics and autocomplete."""
        try:
            log = SearchLog(
                query=query.lower().strip(),
                user_id=user_id,
                results_count=results_count,
                latency_ms=latency_ms,
            )
            self.db.add(log)
            await self.db.commit()
        except Exception:
            # Don't fail search if logging fails
            await self.db.rollback()

    async def invalidate_cache(self, pattern: str = "search:*") -> int:
        """Invalidate search cache entries matching pattern."""
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
            if keys:
                deleted += await self.redis.delete(*keys)
            if cursor == 0:
                break
        return deleted
