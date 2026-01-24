"""
Counter Aggregation Worker

Consumes like events from Kafka, deduplicates, and batch updates counters.
Run with: python -m app.workers.aggregator
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Dict, Set, Tuple

from aiokafka import AIOKafkaConsumer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import redis.asyncio as redis

from app.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()


class CounterAggregator:
    """Aggregates like events and batch updates counters."""

    def __init__(self):
        self.pending: Dict[Tuple[int, int], int] = defaultdict(int)  # (item_id, item_type) -> delta
        self.seen: Set[Tuple[int, int, int]] = set()  # (user_id, item_id, item_type)
        self.processed_events: Set[str] = set()

        # Database connection
        self.engine = create_async_engine(settings.database_url, pool_size=10)
        self.session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        # Redis connection
        self.redis = redis.Redis.from_url(settings.redis_url, decode_responses=True)

        # Kafka consumer
        self.consumer = None

    async def start(self):
        """Start the aggregation worker."""
        logger.info("Starting counter aggregation worker...")

        self.consumer = AIOKafkaConsumer(
            settings.kafka_topic,
            bootstrap_servers=settings.kafka_bootstrap_servers,
            group_id="counter-aggregator",
            auto_offset_reset="earliest",
            enable_auto_commit=True,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        )
        await self.consumer.start()

        logger.info(f"Consuming from topic: {settings.kafka_topic}")

        try:
            await self._consume_loop()
        finally:
            await self.consumer.stop()
            await self.engine.dispose()
            await self.redis.close()

    async def _consume_loop(self):
        """Main consumption loop with windowed processing."""
        window_start = asyncio.get_event_loop().time()
        events_buffer = []

        async for message in self.consumer:
            events_buffer.append(message.value)

            current_time = asyncio.get_event_loop().time()
            window_elapsed = current_time - window_start

            # Process when window expires or buffer is large
            if window_elapsed >= settings.aggregation_window_seconds or len(events_buffer) >= 1000:
                await self._process_batch(events_buffer)
                events_buffer = []
                window_start = current_time

    async def _process_batch(self, events: list):
        """Process a batch of events."""
        if not events:
            return

        logger.info(f"Processing batch of {len(events)} events")

        # 1. Deduplicate within batch
        for event in events:
            event_id = event.get("event_id")
            if event_id in self.processed_events:
                continue

            user_id = event["user_id"]
            item_id = event["item_id"]
            item_type = event["item_type"]
            action = event["action"]

            key = (user_id, item_id, item_type)
            if key in self.seen:
                continue

            self.seen.add(key)
            self.processed_events.add(event_id)

            counter_key = (item_id, item_type)
            delta = 1 if action == "like" else -1
            self.pending[counter_key] += delta

        # 2. Verify with database and update
        if self.pending:
            await self._update_database()
            await self._update_redis_cache()

        # 3. Clear batch state
        self.pending.clear()
        self.seen.clear()

        # Keep processed_events bounded (last 10K events)
        if len(self.processed_events) > 10000:
            self.processed_events = set(list(self.processed_events)[-5000:])

    async def _update_database(self):
        """Batch update counters in database."""
        async with self.session_factory() as session:
            # Process likes
            for (item_id, item_type), delta in self.pending.items():
                if delta == 0:
                    continue

                # Update counter
                await session.execute(
                    text("""
                        INSERT INTO counters (item_id, item_type, like_count)
                        VALUES (:item_id, :item_type, GREATEST(0, :delta))
                        ON CONFLICT (item_id, item_type)
                        DO UPDATE SET
                            like_count = GREATEST(0, counters.like_count + :delta),
                            updated_at = NOW()
                    """),
                    {"item_id": item_id, "item_type": item_type, "delta": delta},
                )

            await session.commit()
            logger.info(f"Updated {len(self.pending)} counters in database")

    async def _update_redis_cache(self):
        """Update Redis cache with new counter values."""
        type_map = {1: "post", 2: "reel", 3: "comment"}

        pipe = self.redis.pipeline()
        for (item_id, item_type), delta in self.pending.items():
            if delta == 0:
                continue

            item_type_str = type_map.get(item_type, "post")
            cache_key = f"counter:{item_type_str}:{item_id}"
            pipe.incrby(cache_key, delta)
            pipe.expire(cache_key, settings.cache_ttl_seconds)

        await pipe.execute()
        logger.info(f"Updated {len(self.pending)} counters in Redis")


async def main():
    aggregator = CounterAggregator()
    await aggregator.start()


if __name__ == "__main__":
    asyncio.run(main())
