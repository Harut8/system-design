import json
from typing import Optional, Any
from datetime import datetime
import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()


class CacheService:
    """
    Cache service for 100K tier.
    Provides caching utilities for feed, posts, and users.
    """

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    # ----- Feed Cache -----
    async def get_cached_feed(self, user_id: int, cursor: Optional[str] = None) -> Optional[tuple[list, int]]:
        """
        Get cached feed for a user.
        Returns (posts, cache_age_seconds) or None if not cached.
        """
        cache_key = f"feed:{user_id}:{cursor or 'first'}"

        data = await self.redis.get(cache_key)
        if not data:
            return None

        cached = json.loads(data)
        cache_age = int(datetime.utcnow().timestamp() - cached.get("cached_at", 0))

        return cached.get("posts", []), cache_age

    async def set_cached_feed(
        self,
        user_id: int,
        posts: list,
        cursor: Optional[str] = None,
        next_cursor: Optional[str] = None,
        has_more: bool = False,
    ) -> None:
        """Cache feed results."""
        cache_key = f"feed:{user_id}:{cursor or 'first'}"

        data = {
            "posts": posts,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "cached_at": datetime.utcnow().timestamp(),
        }

        await self.redis.setex(
            cache_key,
            settings.feed_cache_ttl,
            json.dumps(data, default=str),
        )

    async def invalidate_feed_cache(self, user_id: int) -> None:
        """Invalidate feed cache for a user."""
        pattern = f"feed:{user_id}:*"
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
            if keys:
                await self.redis.delete(*keys)
            if cursor == 0:
                break

    async def invalidate_follower_feeds(self, author_id: int, follower_ids: list[int]) -> None:
        """Invalidate feed cache for all followers when author posts."""
        if not follower_ids:
            return

        # Delete first page cache for each follower
        keys = [f"feed:{fid}:first" for fid in follower_ids]
        if keys:
            await self.redis.delete(*keys)

    # ----- Feed ID List (for fan-out-on-write) -----
    async def push_to_feed(self, user_id: int, post_id: int) -> None:
        """Push a post ID to user's feed list (fan-out-on-write)."""
        feed_key = f"feed_ids:{user_id}"
        await self.redis.lpush(feed_key, post_id)
        await self.redis.ltrim(feed_key, 0, 499)  # Keep last 500 posts
        await self.redis.expire(feed_key, 600)  # 10 min TTL

    async def get_feed_ids(self, user_id: int, start: int = 0, end: int = 99) -> list[int]:
        """Get pre-pushed feed post IDs."""
        feed_key = f"feed_ids:{user_id}"
        ids = await self.redis.lrange(feed_key, start, end)
        return [int(id) for id in ids] if ids else []

    # ----- Post Cache -----
    async def get_cached_post(self, post_id: int) -> Optional[dict]:
        """Get cached post data."""
        cache_key = f"post:{post_id}"
        data = await self.redis.get(cache_key)
        if data:
            return json.loads(data)
        return None

    async def set_cached_post(self, post_id: int, post_data: dict) -> None:
        """Cache post data."""
        cache_key = f"post:{post_id}"
        await self.redis.setex(
            cache_key,
            settings.post_cache_ttl,
            json.dumps(post_data, default=str),
        )

    async def invalidate_post_cache(self, post_id: int) -> None:
        """Invalidate post cache."""
        cache_key = f"post:{post_id}"
        await self.redis.delete(cache_key)

    async def batch_get_posts(self, post_ids: list[int]) -> dict[int, dict]:
        """Get multiple posts from cache."""
        if not post_ids:
            return {}

        keys = [f"post:{pid}" for pid in post_ids]
        values = await self.redis.mget(keys)

        result = {}
        for pid, value in zip(post_ids, values):
            if value:
                result[pid] = json.loads(value)

        return result

    # ----- User Cache -----
    async def get_cached_user(self, user_id: int) -> Optional[dict]:
        """Get cached user data."""
        cache_key = f"user:{user_id}"
        data = await self.redis.get(cache_key)
        if data:
            return json.loads(data)
        return None

    async def set_cached_user(self, user_id: int, user_data: dict) -> None:
        """Cache user data."""
        cache_key = f"user:{user_id}"
        await self.redis.setex(
            cache_key,
            settings.user_cache_ttl,
            json.dumps(user_data, default=str),
        )

    async def invalidate_user_cache(self, user_id: int) -> None:
        """Invalidate user cache."""
        cache_key = f"user:{user_id}"
        await self.redis.delete(cache_key)

    # ----- Follower Count Cache -----
    async def get_follower_count(self, user_id: int) -> Optional[int]:
        """Get cached follower count."""
        cache_key = f"follower_count:{user_id}"
        count = await self.redis.get(cache_key)
        return int(count) if count else None

    async def set_follower_count(self, user_id: int, count: int) -> None:
        """Cache follower count."""
        cache_key = f"follower_count:{user_id}"
        await self.redis.setex(cache_key, 3600, count)  # 1 hour TTL
