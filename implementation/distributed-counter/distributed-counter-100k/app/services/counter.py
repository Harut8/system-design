from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
import redis.asyncio as redis
from typing import Optional

from app.schemas import item_type_to_int
from app.config import get_settings

settings = get_settings()


class CounterService:
    """Counter service with Redis caching."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    def _counter_key(self, item_type: str, item_id: int) -> str:
        return f"counter:{item_type}:{item_id}"

    def _recent_likes_key(self, user_id: int) -> str:
        return f"recent_likes:{user_id}"

    async def _check_recent_like(self, user_id: int, item_id: int, item_type: str) -> bool:
        """Check if like exists in recent likes cache."""
        key = self._recent_likes_key(user_id)
        member = f"{item_type}:{item_id}"
        return await self.redis.sismember(key, member)

    async def _add_recent_like(self, user_id: int, item_id: int, item_type: str) -> None:
        """Add like to recent likes cache."""
        key = self._recent_likes_key(user_id)
        member = f"{item_type}:{item_id}"

        pipe = self.redis.pipeline()
        pipe.sadd(key, member)
        pipe.expire(key, settings.recent_likes_ttl_seconds)

        # Trim to max size if needed (probabilistic, not exact)
        if await self.redis.scard(key) > settings.recent_likes_max_size:
            # Remove a random member
            pipe.spop(key)

        await pipe.execute()

    async def _remove_recent_like(self, user_id: int, item_id: int, item_type: str) -> None:
        """Remove like from recent likes cache."""
        key = self._recent_likes_key(user_id)
        member = f"{item_type}:{item_id}"
        await self.redis.srem(key, member)

    async def like(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int]:
        """Like an item with caching."""
        item_type_int = item_type_to_int(item_type)

        # 1. Quick dedup check in Redis
        if await self._check_recent_like(user_id, item_id, item_type):
            count = await self.get_count(item_id, item_type)
            return "already_liked", count

        # 2. Try to insert into PostgreSQL
        try:
            await self.db.execute(
                text("""
                    INSERT INTO user_likes (user_id, item_id, item_type)
                    VALUES (:user_id, :item_id, :item_type)
                """),
                {"user_id": user_id, "item_id": item_id, "item_type": item_type_int},
            )

            await self.db.execute(
                text("""
                    INSERT INTO counters (item_id, item_type, like_count)
                    VALUES (:item_id, :item_type, 1)
                    ON CONFLICT (item_id, item_type)
                    DO UPDATE SET like_count = counters.like_count + 1,
                                  updated_at = NOW()
                """),
                {"item_id": item_id, "item_type": item_type_int},
            )

            await self.db.commit()

            # 3. Update Redis caches
            await self._add_recent_like(user_id, item_id, item_type)
            counter_key = self._counter_key(item_type, item_id)
            await self.redis.incr(counter_key)
            await self.redis.expire(counter_key, settings.cache_ttl_seconds)

            count = await self.get_count(item_id, item_type)
            return "liked", count

        except IntegrityError:
            await self.db.rollback()
            # Update cache for next time
            await self._add_recent_like(user_id, item_id, item_type)
            count = await self.get_count(item_id, item_type)
            return "already_liked", count

    async def unlike(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int]:
        """Unlike an item."""
        item_type_int = item_type_to_int(item_type)

        result = await self.db.execute(
            text("""
                DELETE FROM user_likes
                WHERE user_id = :user_id AND item_id = :item_id AND item_type = :item_type
            """),
            {"user_id": user_id, "item_id": item_id, "item_type": item_type_int},
        )

        if result.rowcount == 0:
            count = await self.get_count(item_id, item_type)
            return "not_liked", count

        await self.db.execute(
            text("""
                UPDATE counters
                SET like_count = GREATEST(like_count - 1, 0), updated_at = NOW()
                WHERE item_id = :item_id AND item_type = :item_type
            """),
            {"item_id": item_id, "item_type": item_type_int},
        )

        await self.db.commit()

        # Update Redis
        await self._remove_recent_like(user_id, item_id, item_type)
        counter_key = self._counter_key(item_type, item_id)
        await self.redis.decr(counter_key)

        count = await self.get_count(item_id, item_type)
        return "unliked", count

    async def get_count(self, item_id: int, item_type: str) -> int:
        """Get count with cache."""
        counter_key = self._counter_key(item_type, item_id)

        # Try cache first
        cached = await self.redis.get(counter_key)
        if cached is not None:
            return int(cached)

        # Cache miss - query DB
        item_type_int = item_type_to_int(item_type)
        result = await self.db.execute(
            text("SELECT like_count FROM counters WHERE item_id = :item_id AND item_type = :item_type"),
            {"item_id": item_id, "item_type": item_type_int},
        )
        row = result.fetchone()
        count = row[0] if row else 0

        # Populate cache
        await self.redis.setex(counter_key, settings.cache_ttl_seconds, count)

        return count

    async def get_count_cached(self, item_id: int, item_type: str) -> tuple[int, bool]:
        """Get count and whether it was cached."""
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

    async def get_counts_batch(self, items: list[tuple[int, str]]) -> tuple[dict[str, int], int, int]:
        """Get counts for multiple items. Returns (counts, hits, misses)."""
        if not items:
            return {}, 0, 0

        # Try to get all from Redis first (MGET)
        keys = [self._counter_key(item_type, item_id) for item_id, item_type in items]
        cached_values = await self.redis.mget(keys)

        counts = {}
        cache_hits = 0
        cache_misses = 0
        missing_items = []

        for i, (item_id, item_type) in enumerate(items):
            key = f"{item_type}:{item_id}"
            if cached_values[i] is not None:
                counts[key] = int(cached_values[i])
                cache_hits += 1
            else:
                missing_items.append((item_id, item_type))
                cache_misses += 1

        # Query DB for misses
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

            # Fill zeros for items not in DB
            for item_id, item_type in missing_items:
                key = f"{item_type}:{item_id}"
                if key not in counts:
                    counts[key] = 0

        return counts, cache_hits, cache_misses

    async def has_user_liked(self, user_id: int, item_ids: list[int], item_type: str) -> dict[str, bool]:
        """Check if user has liked items."""
        if not item_ids:
            return {}

        # Check recent likes cache first
        recent_key = self._recent_likes_key(user_id)
        liked = {}

        # Check cache for all items
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

        # Check DB for items not in cache
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
