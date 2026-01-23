from celery import Celery
import redis
import json
import time
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.config import get_settings

settings = get_settings()

# Create Celery app
celery_app = Celery(
    "instagram_feed",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "app.workers.tasks.fan_out_post": {"queue": "fanout"},
        "app.workers.tasks.process_media": {"queue": "media"},
    },
)

# Sync database connection for Celery workers
engine = create_engine(
    settings.database_url.replace("+asyncpg", "+psycopg2"),
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine)

# Redis client for workers
redis_client = redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(bind=True, max_retries=3)
def fan_out_post(self, post_id: int, author_id: int, follower_count: int):
    """
    Async fan-out task for distributing posts to follower feeds.
    Only called for non-celebrity users (< celebrity_threshold followers).
    """
    try:
        db = SessionLocal()

        # Get followers in batches
        batch_size = settings.fan_out_batch_size
        offset = 0

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

            # Push to each follower's feed cache
            pipe = redis_client.pipeline()
            for follower_id in followers:
                feed_key = f"feed_ids:{follower_id}"
                pipe.lpush(feed_key, post_id)
                pipe.ltrim(feed_key, 0, 499)  # Keep last 500
                pipe.expire(feed_key, 600)  # 10 min TTL
            pipe.execute()

            # Invalidate cached feed results
            for follower_id in followers:
                redis_client.delete(f"feed:{follower_id}:first")

            offset += batch_size

            # Rate limit to avoid overwhelming Redis
            time.sleep(0.01)  # 100 batches/sec max

        db.close()

        return {
            "status": "completed",
            "post_id": post_id,
            "author_id": author_id,
            "followers_notified": follower_count,
        }

    except Exception as exc:
        self.retry(exc=exc, countdown=60)


@celery_app.task(bind=True, max_retries=3)
def store_celebrity_post(self, post_id: int, author_id: int):
    """
    Store celebrity post in a special sorted set for pull-based retrieval.
    """
    try:
        # Store in celebrity posts sorted set (by timestamp for recency)
        celebrity_key = f"celebrity_posts:{author_id}"
        timestamp = time.time()

        redis_client.zadd(celebrity_key, {post_id: timestamp})
        redis_client.zremrangebyrank(celebrity_key, 0, -101)  # Keep last 100 posts
        redis_client.expire(celebrity_key, 7 * 24 * 3600)  # 7 day TTL

        return {
            "status": "completed",
            "post_id": post_id,
            "author_id": author_id,
            "type": "celebrity_post",
        }

    except Exception as exc:
        self.retry(exc=exc, countdown=60)


@celery_app.task(bind=True, max_retries=3)
def process_media(self, upload_id: str, media_type: str):
    """
    Placeholder for media processing task.
    In production, this would:
    - Download from temp S3 location
    - Resize images to multiple sizes
    - Transcode videos to multiple qualities
    - Upload processed files to permanent S3 location
    - Update post with CDN URLs
    """
    try:
        # Simulate processing time
        time.sleep(2)

        return {
            "status": "completed",
            "upload_id": upload_id,
            "media_type": media_type,
            "sizes_generated": ["thumbnail", "small", "medium", "large"],
        }

    except Exception as exc:
        self.retry(exc=exc, countdown=60)


@celery_app.task
def cleanup_old_feed_cache():
    """
    Periodic task to clean up old feed caches.
    Run via Celery Beat every hour.
    """
    # This is handled by Redis TTLs, but could do additional cleanup here
    return {"status": "completed"}


# Celery Beat schedule
celery_app.conf.beat_schedule = {
    "cleanup-feed-cache-hourly": {
        "task": "app.workers.tasks.cleanup_old_feed_cache",
        "schedule": 3600.0,  # Every hour
    },
}
