"""
Kafka Worker Service for 10M Tier.

Consumes events from Kafka topics and performs:
- Fan-out to follower feeds
- Celebrity post indexing
- Feed cache invalidation
"""
import json
import time
import logging
from kafka import KafkaConsumer
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import redis

from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection
engine = create_engine(settings.database_url, pool_size=5)
SessionLocal = sessionmaker(bind=engine)

# Redis connection
redis_client = redis.from_url(settings.redis_url, decode_responses=True)


def process_post_created(event: dict):
    """Handle post.created event."""
    post_id = event["post_id"]
    author_id = event["author_id"]
    follower_count = event.get("follower_count", 0)
    is_celebrity = event.get("is_celebrity", False)

    logger.info(f"Processing post.created: post_id={post_id}, author_id={author_id}, is_celebrity={is_celebrity}")

    if is_celebrity:
        # Store in celebrity posts sorted set
        store_celebrity_post(post_id, author_id)
    else:
        # Fan-out to followers
        fan_out_to_followers(post_id, author_id, follower_count)


def store_celebrity_post(post_id: int, author_id: int):
    """Store celebrity post for pull-based retrieval."""
    celebrity_key = f"celebrity_posts:{author_id}"
    timestamp = time.time()

    redis_client.zadd(celebrity_key, {post_id: timestamp})
    redis_client.zremrangebyrank(celebrity_key, 0, -101)  # Keep last 100
    redis_client.expire(celebrity_key, 7 * 24 * 3600)  # 7 day TTL

    logger.info(f"Stored celebrity post: {post_id} for author {author_id}")


def fan_out_to_followers(post_id: int, author_id: int, follower_count: int):
    """Fan-out post to follower feed caches."""
    db = SessionLocal()
    batch_size = settings.fan_out_batch_size
    offset = 0
    total_notified = 0

    try:
        while True:
            result = db.execute(
                text("""
                    SELECT follower_id FROM follows
                    WHERE followee_id = :author_id
                    LIMIT :batch_size OFFSET :offset
                """),
                {"author_id": author_id, "batch_size": batch_size, "offset": offset}
            )
            followers = [row[0] for row in result.fetchall()]

            if not followers:
                break

            # Push to each follower's feed
            pipe = redis_client.pipeline()
            for follower_id in followers:
                feed_key = f"feed_ids:{follower_id}"
                pipe.lpush(feed_key, post_id)
                pipe.ltrim(feed_key, 0, 499)
                pipe.expire(feed_key, 600)
                # Invalidate cached feed
                pipe.delete(f"feed:{follower_id}:first")
            pipe.execute()

            total_notified += len(followers)
            offset += batch_size

            # Rate limit
            time.sleep(0.01)

        logger.info(f"Fan-out complete: post_id={post_id}, notified={total_notified}")

    finally:
        db.close()


def process_post_deleted(event: dict):
    """Handle post.deleted event."""
    post_id = event["post_id"]
    author_id = event["author_id"]

    logger.info(f"Processing post.deleted: post_id={post_id}")

    # Remove from celebrity posts if exists
    celebrity_key = f"celebrity_posts:{author_id}"
    redis_client.zrem(celebrity_key, post_id)

    # Invalidate post cache
    redis_client.delete(f"post:{post_id}")


def main():
    """Main worker loop."""
    logger.info("Starting Kafka worker service...")

    consumer = KafkaConsumer(
        "posts.created",
        "posts.deleted",
        bootstrap_servers=settings.kafka_bootstrap_servers.split(","),
        group_id="fanout-workers",
        auto_offset_reset="latest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    )

    logger.info("Connected to Kafka, waiting for messages...")

    for message in consumer:
        try:
            event = message.value
            topic = message.topic

            if topic == "posts.created":
                process_post_created(event)
            elif topic == "posts.deleted":
                process_post_deleted(event)

        except Exception as e:
            logger.error(f"Error processing message: {e}")


if __name__ == "__main__":
    main()
