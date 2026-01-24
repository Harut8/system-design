"""
Sharded Counter Service with Hot Item Detection

Handles automatic sharding of hot counters to avoid single-key bottlenecks.
"""

import random
import redis.asyncio as redis
from app.config import get_settings

settings = get_settings()


class HotItemDetector:
    """Detects hot items and triggers sharding."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def record_write(self, item_type: str, item_id: int) -> bool:
        """Record a write and check if item should be sharded."""
        hot_key = f"hot:{item_type}:{item_id}"

        current = await self.redis.incr(hot_key)
        if current == 1:
            await self.redis.expire(hot_key, settings.hot_window_seconds)

        rate = current / settings.hot_window_seconds
        return rate > settings.shard_threshold

    async def is_hot(self, item_type: str, item_id: int) -> bool:
        """Check if an item is currently hot."""
        shard_key = f"counter:{item_type}:{item_id}:shards"
        return await self.redis.exists(shard_key) > 0


class ShardedCounter:
    """Counter that automatically shards for hot items."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.hot_detector = HotItemDetector(redis_client)

    def _base_key(self, item_type: str, item_id: int) -> str:
        return f"counter:{item_type}:{item_id}"

    async def _is_sharded(self, item_type: str, item_id: int) -> int:
        """Check if counter is sharded and return shard count."""
        key = f"{self._base_key(item_type, item_id)}:shards"
        result = await self.redis.get(key)
        return int(result) if result else 0

    async def _create_shards(self, item_type: str, item_id: int) -> None:
        """Convert a single counter to sharded counter."""
        base_key = self._base_key(item_type, item_id)
        shard_key = f"{base_key}:shards"

        # Check if already sharded
        if await self.redis.exists(shard_key):
            return

        # Get current value
        current = int(await self.redis.get(base_key) or 0)

        # Distribute across shards
        per_shard = current // settings.default_shards
        remainder = current % settings.default_shards

        pipe = self.redis.pipeline()
        for i in range(settings.default_shards):
            value = per_shard + (1 if i < remainder else 0)
            pipe.set(f"{base_key}:shard_{i}", value)
            pipe.expire(f"{base_key}:shard_{i}", settings.cache_ttl_seconds)

        pipe.set(shard_key, settings.default_shards)
        pipe.expire(shard_key, settings.cache_ttl_seconds)
        pipe.delete(base_key)
        await pipe.execute()

    async def increment(self, item_type: str, item_id: int) -> int:
        """Increment counter, handling sharding automatically."""
        base_key = self._base_key(item_type, item_id)

        # Record write for hot detection
        should_shard = await self.hot_detector.record_write(item_type, item_id)

        # Check if already sharded
        shard_count = await self._is_sharded(item_type, item_id)

        if shard_count:
            # Hot item: increment random shard
            shard = random.randint(0, shard_count - 1)
            new_value = await self.redis.incr(f"{base_key}:shard_{shard}")
            await self.redis.expire(f"{base_key}:shard_{shard}", settings.cache_ttl_seconds)
            return new_value
        elif should_shard:
            # Trigger sharding
            await self._create_shards(item_type, item_id)
            # Now increment a random shard
            shard = random.randint(0, settings.default_shards - 1)
            new_value = await self.redis.incr(f"{base_key}:shard_{shard}")
            return new_value
        else:
            # Normal item: single counter
            new_value = await self.redis.incr(base_key)
            await self.redis.expire(base_key, settings.cache_ttl_seconds)
            return new_value

    async def decrement(self, item_type: str, item_id: int) -> int:
        """Decrement counter."""
        base_key = self._base_key(item_type, item_id)
        shard_count = await self._is_sharded(item_type, item_id)

        if shard_count:
            # Pick a random shard to decrement
            shard = random.randint(0, shard_count - 1)
            new_value = await self.redis.decr(f"{base_key}:shard_{shard}")
            return max(0, new_value)
        else:
            new_value = await self.redis.decr(base_key)
            return max(0, new_value)

    async def get(self, item_type: str, item_id: int) -> tuple[int, bool]:
        """Get counter value. Returns (count, is_sharded)."""
        base_key = self._base_key(item_type, item_id)
        shard_count = await self._is_sharded(item_type, item_id)

        if shard_count:
            # Sum all shards
            keys = [f"{base_key}:shard_{i}" for i in range(shard_count)]
            values = await self.redis.mget(keys)
            total = sum(int(v or 0) for v in values)
            return total, True
        else:
            value = await self.redis.get(base_key)
            return int(value or 0), False

    async def get_batch(self, items: list[tuple[str, int]]) -> dict[str, tuple[int, bool]]:
        """Get multiple counters efficiently."""
        results = {}

        for item_type, item_id in items:
            count, is_sharded = await self.get(item_type, item_id)
            key = f"{item_type}:{item_id}"
            results[key] = (count, is_sharded)

        return results
