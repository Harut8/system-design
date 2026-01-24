"""Counter service with sharding support."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import redis.asyncio as redis

from app.schemas import item_type_to_int
from app.services.sharded_counter import ShardedCounter
from app.config import get_settings

settings = get_settings()


class CounterService:
    """Counter service with automatic hot item sharding."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client
        self.sharded_counter = ShardedCounter(redis_client)

    def _recent_likes_key(self, user_id: int) -> str:
        return f"recent_likes:{user_id}"

    async def _check_recent_like(self, user_id: int, item_id: int, item_type: str) -> bool:
        key = self._recent_likes_key(user_id)
        return await self.redis.sismember(key, f"{item_type}:{item_id}")

    async def _add_recent_like(self, user_id: int, item_id: int, item_type: str) -> None:
        key = self._recent_likes_key(user_id)
        pipe = self.redis.pipeline()
        pipe.sadd(key, f"{item_type}:{item_id}")
        pipe.expire(key, 86400)
        await pipe.execute()

    async def _remove_recent_like(self, user_id: int, item_id: int, item_type: str) -> None:
        await self.redis.srem(self._recent_likes_key(user_id), f"{item_type}:{item_id}")

    async def like(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int, bool]:
        """Like an item. Returns (status, count, is_hot)."""
        if await self._check_recent_like(user_id, item_id, item_type):
            count, is_sharded = await self.sharded_counter.get(item_type, item_id)
            return "already_liked", count, is_sharded

        # Update cache
        await self._add_recent_like(user_id, item_id, item_type)
        new_count = await self.sharded_counter.increment(item_type, item_id)

        # Check if now hot
        is_hot = await self.sharded_counter.hot_detector.is_hot(item_type, item_id)

        return "queued", new_count, is_hot

    async def unlike(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int, bool]:
        """Unlike an item."""
        await self._remove_recent_like(user_id, item_id, item_type)
        new_count = await self.sharded_counter.decrement(item_type, item_id)
        is_hot = await self.sharded_counter.hot_detector.is_hot(item_type, item_id)

        return "queued", max(0, new_count), is_hot

    async def get_count(self, item_id: int, item_type: str) -> tuple[int, bool]:
        """Get count. Returns (count, is_sharded)."""
        return await self.sharded_counter.get(item_type, item_id)

    async def get_counts_batch(self, items: list[tuple[int, str]]) -> dict[str, int]:
        """Get counts for multiple items."""
        results = await self.sharded_counter.get_batch(
            [(item_type, item_id) for item_id, item_type in items]
        )
        return {k: v[0] for k, v in results.items()}

    async def has_user_liked(self, user_id: int, item_ids: list[int], item_type: str) -> dict[str, bool]:
        """Check if user has liked items."""
        if not item_ids:
            return {}

        recent_key = self._recent_likes_key(user_id)
        pipe = self.redis.pipeline()
        for item_id in item_ids:
            pipe.sismember(recent_key, f"{item_type}:{item_id}")
        results = await pipe.execute()

        return {str(item_id): bool(results[i]) for i, item_id in enumerate(item_ids)}
