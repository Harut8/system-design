from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import redis.asyncio as redis
from typing import Optional

from app.schemas import item_type_to_int, LikeEvent
from app.services.kafka_producer import produce_like_event
from app.config import get_settings

settings = get_settings()


class CounterService:
    """Counter service with async Kafka writes."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    def _counter_key(self, item_type: str, item_id: int) -> str:
        return f"counter:{item_type}:{item_id}"

    def _recent_likes_key(self, user_id: int) -> str:
        return f"recent_likes:{user_id}"

    async def _check_recent_like(self, user_id: int, item_id: int, item_type: str) -> bool:
        key = self._recent_likes_key(user_id)
        return await self.redis.sismember(key, f"{item_type}:{item_id}")

    async def _add_recent_like(self, user_id: int, item_id: int, item_type: str) -> None:
        key = self._recent_likes_key(user_id)
        pipe = self.redis.pipeline()
        pipe.sadd(key, f"{item_type}:{item_id}")
        pipe.expire(key, settings.recent_likes_ttl_seconds)
        await pipe.execute()

    async def _remove_recent_like(self, user_id: int, item_id: int, item_type: str) -> None:
        key = self._recent_likes_key(user_id)
        await self.redis.srem(key, f"{item_type}:{item_id}")

    async def like(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int]:
        """
        Like an item asynchronously via Kafka.
        Returns immediately with optimistic count update.
        """
        item_type_int = item_type_to_int(item_type)

        # Quick dedup check
        if await self._check_recent_like(user_id, item_id, item_type):
            count = await self.get_count(item_id, item_type)
            return "already_liked", count

        # Produce event to Kafka (fire and forget)
        event = LikeEvent(
            user_id=user_id,
            item_id=item_id,
            item_type=item_type_int,
            action="like",
        )
        await produce_like_event(event)

        # Optimistic cache updates
        await self._add_recent_like(user_id, item_id, item_type)
        counter_key = self._counter_key(item_type, item_id)
        new_count = await self.redis.incr(counter_key)
        await self.redis.expire(counter_key, settings.cache_ttl_seconds)

        return "queued", new_count

    async def unlike(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int]:
        """Unlike an item asynchronously via Kafka."""
        item_type_int = item_type_to_int(item_type)

        # Check if user has liked
        if not await self._check_recent_like(user_id, item_id, item_type):
            # Could still be in DB but not cache - produce event anyway
            pass

        # Produce event to Kafka
        event = LikeEvent(
            user_id=user_id,
            item_id=item_id,
            item_type=item_type_int,
            action="unlike",
        )
        await produce_like_event(event)

        # Optimistic cache updates
        await self._remove_recent_like(user_id, item_id, item_type)
        counter_key = self._counter_key(item_type, item_id)
        new_count = await self.redis.decr(counter_key)

        return "queued", max(0, new_count)

    async def get_count(self, item_id: int, item_type: str) -> int:
        counter_key = self._counter_key(item_type, item_id)

        cached = await self.redis.get(counter_key)
        if cached is not None:
            return int(cached)

        item_type_int = item_type_to_int(item_type)
        result = await self.db.execute(
            text("SELECT like_count FROM counters WHERE item_id = :item_id AND item_type = :item_type"),
            {"item_id": item_id, "item_type": item_type_int},
        )
        row = result.fetchone()
        count = row[0] if row else 0

        await self.redis.setex(counter_key, settings.cache_ttl_seconds, count)
        return count

    async def get_count_cached(self, item_id: int, item_type: str) -> tuple[int, bool]:
        counter_key = self._counter_key(item_type, item_id)

        cached = await self.redis.get(counter_key)
        if cached is not None:
            return int(cached), True

        item_type_int = item_type_to_int(item_type)
        result = await self.db.execute(
            text("SELECT like_count FROM counters WHERE item_id = :item_id AND item_type = :item_type"),
            {"item_id": item_id, "item_type": item_type_int},
        )
        row = result.fetchone()
        count = row[0] if row else 0

        await self.redis.setex(counter_key, settings.cache_ttl_seconds, count)
        return count, False

    async def get_counts_batch(self, items: list[tuple[int, str]]) -> dict[str, int]:
        if not items:
            return {}

        keys = [self._counter_key(item_type, item_id) for item_id, item_type in items]
        cached_values = await self.redis.mget(keys)

        counts = {}
        missing_items = []

        for i, (item_id, item_type) in enumerate(items):
            key = f"{item_type}:{item_id}"
            if cached_values[i] is not None:
                counts[key] = int(cached_values[i])
            else:
                missing_items.append((item_id, item_type))

        if missing_items:
            conditions = []
            params = {}
            for i, (item_id, item_type) in enumerate(missing_items):
                item_type_int = item_type_to_int(item_type)
                conditions.append(f"(item_id = :item_id_{i} AND item_type = :item_type_{i})")
                params[f"item_id_{i}"] = item_id
                params[f"item_type_{i}"] = item_type_int

            query = f"SELECT item_id, item_type, like_count FROM counters WHERE {' OR '.join(conditions)}"
            result = await self.db.execute(text(query), params)

            type_map = {1: "post", 2: "reel", 3: "comment"}
            pipe = self.redis.pipeline()

            for item_id, item_type_int, count in result.fetchall():
                item_type_str = type_map.get(item_type_int, "post")
                key = f"{item_type_str}:{item_id}"
                counts[key] = count
                cache_key = self._counter_key(item_type_str, item_id)
                pipe.setex(cache_key, settings.cache_ttl_seconds, count)

            await pipe.execute()

            for item_id, item_type in missing_items:
                key = f"{item_type}:{item_id}"
                if key not in counts:
                    counts[key] = 0

        return counts

    async def has_user_liked(self, user_id: int, item_ids: list[int], item_type: str) -> dict[str, bool]:
        if not item_ids:
            return {}

        recent_key = self._recent_likes_key(user_id)
        liked = {}

        pipe = self.redis.pipeline()
        for item_id in item_ids:
            pipe.sismember(recent_key, f"{item_type}:{item_id}")
        cache_results = await pipe.execute()

        items_to_check_db = []
        for i, item_id in enumerate(item_ids):
            if cache_results[i]:
                liked[str(item_id)] = True
            else:
                items_to_check_db.append(item_id)

        if items_to_check_db:
            item_type_int = item_type_to_int(item_type)
            result = await self.db.execute(
                text("""
                    SELECT item_id FROM user_likes
                    WHERE user_id = :user_id AND item_type = :item_type AND item_id = ANY(:item_ids)
                """),
                {"user_id": user_id, "item_type": item_type_int, "item_ids": items_to_check_db},
            )
            liked_in_db = {row[0] for row in result.fetchall()}

            for item_id in items_to_check_db:
                liked[str(item_id)] = item_id in liked_in_db

        return liked
