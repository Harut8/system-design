import json
import httpx
from typing import Optional
from datetime import datetime
import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()


class FeedService:
    """
    Feed Service for 10M tier.
    - Assembles feeds from cached post IDs
    - Fetches post data from Post Service
    - Handles celebrity posts separately
    """

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.http_client = httpx.AsyncClient(base_url=settings.post_service_url)

    async def get_feed(
        self,
        user_id: int,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> dict:
        """Get feed with caching."""
        # Check cache
        cache_key = f"feed:{user_id}:{cursor or 'first'}"
        cached = await self.redis.get(cache_key)
        if cached:
            data = json.loads(cached)
            return {**data, "cached": True}

        # Get pre-pushed post IDs
        feed_ids = await self._get_feed_ids(user_id, limit * 2)

        # Get celebrity posts
        celebrity_ids = await self._get_celebrity_followees(user_id)
        celebrity_post_ids = []
        for celeb_id in celebrity_ids[:20]:
            posts = await self._get_celebrity_posts(celeb_id, 10)
            celebrity_post_ids.extend(posts)

        # Merge and dedupe
        all_ids = list(dict.fromkeys(feed_ids + celebrity_post_ids))

        # Fetch post data from Post Service
        posts = await self._fetch_posts(all_ids[:limit], user_id)

        # Sort by created_at
        posts.sort(key=lambda p: p.get("created_at", ""), reverse=True)
        posts = posts[:limit]

        result = {
            "posts": posts,
            "next_cursor": None,
            "has_more": len(all_ids) > limit,
            "cached": False,
        }

        # Cache result
        await self.redis.setex(
            cache_key,
            settings.feed_cache_ttl,
            json.dumps(result, default=str)
        )

        return result

    async def _get_feed_ids(self, user_id: int, limit: int) -> list[int]:
        """Get pre-pushed feed post IDs from Redis."""
        feed_key = f"feed_ids:{user_id}"
        ids = await self.redis.lrange(feed_key, 0, limit - 1)
        return [int(id) for id in ids] if ids else []

    async def _get_celebrity_followees(self, user_id: int) -> list[int]:
        """Get celebrities this user follows."""
        cache_key = f"celebrity_followees:{user_id}"
        data = await self.redis.get(cache_key)
        return json.loads(data) if data else []

    async def _get_celebrity_posts(self, celebrity_id: int, limit: int) -> list[int]:
        """Get recent posts from a celebrity."""
        key = f"celebrity_posts:{celebrity_id}"
        posts = await self.redis.zrevrange(key, 0, limit - 1)
        return [int(p) for p in posts] if posts else []

    async def _fetch_posts(self, post_ids: list[int], viewer_id: int) -> list[dict]:
        """Fetch posts from Post Service."""
        if not post_ids:
            return []

        try:
            response = await self.http_client.post(
                "/internal/posts/batch",
                json={"post_ids": post_ids, "viewer_id": viewer_id},
            )
            if response.status_code == 200:
                return response.json().get("posts", [])
        except Exception:
            pass
        return []

    async def close(self):
        await self.http_client.aclose()
