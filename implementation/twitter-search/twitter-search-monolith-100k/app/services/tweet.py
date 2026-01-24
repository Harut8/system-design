"""Tweet service for CRUD operations and engagement updates."""

from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import get_settings
from app.models import Tweet
from app.schemas import (
    EngagementResponse,
    TweetCreate,
    TweetResponse,
    extract_hashtags,
)

settings = get_settings()


class TweetService:
    """Service for tweet operations."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    async def create_tweet(self, tweet_data: TweetCreate) -> TweetResponse:
        """Create a new tweet."""
        # Extract hashtags from text
        hashtags = extract_hashtags(tweet_data.text)

        tweet = Tweet(
            user_id=tweet_data.user_id,
            text=tweet_data.text,
            hashtags=hashtags,
            language=tweet_data.language,
            geo_lat=tweet_data.geo_lat,
            geo_lon=tweet_data.geo_lon,
        )

        self.db.add(tweet)
        await self.db.commit()
        await self.db.refresh(tweet)

        return TweetResponse(
            tweet_id=tweet.tweet_id,
            user_id=tweet.user_id,
            text=tweet.text,
            hashtags=tweet.hashtags,
            language=tweet.language,
            geo_lat=float(tweet.geo_lat) if tweet.geo_lat else None,
            geo_lon=float(tweet.geo_lon) if tweet.geo_lon else None,
            likes=tweet.likes,
            retweets=tweet.retweets,
            views=tweet.views,
            created_at=tweet.created_at,
        )

    async def get_tweet(self, tweet_id: str) -> Optional[TweetResponse]:
        """Get a tweet by ID."""
        # Check cache first
        cached = await self.redis.get(f"tweet:{tweet_id}")
        if cached:
            import json
            data = json.loads(cached)
            return TweetResponse(**data)

        # Query database
        stmt = select(Tweet).where(
            Tweet.tweet_id == tweet_id,
            Tweet.is_deleted == False,
        )
        result = await self.db.execute(stmt)
        tweet = result.scalar_one_or_none()

        if not tweet:
            return None

        response = TweetResponse(
            tweet_id=tweet.tweet_id,
            user_id=tweet.user_id,
            text=tweet.text,
            hashtags=tweet.hashtags,
            language=tweet.language,
            geo_lat=float(tweet.geo_lat) if tweet.geo_lat else None,
            geo_lon=float(tweet.geo_lon) if tweet.geo_lon else None,
            likes=tweet.likes,
            retweets=tweet.retweets,
            views=tweet.views,
            created_at=tweet.created_at,
        )

        # Cache the tweet
        await self.redis.setex(
            f"tweet:{tweet_id}",
            300,  # 5 minutes
            response.model_dump_json(),
        )

        return response

    async def delete_tweet(self, tweet_id: str) -> bool:
        """Soft delete a tweet."""
        stmt = (
            update(Tweet)
            .where(Tweet.tweet_id == tweet_id, Tweet.is_deleted == False)
            .values(is_deleted=True, deleted_at=datetime.now(timezone.utc))
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        if result.rowcount > 0:
            # Invalidate cache
            await self.redis.delete(f"tweet:{tweet_id}")
            return True

        return False

    async def increment_like(self, tweet_id: str) -> Optional[EngagementResponse]:
        """Increment like count for a tweet."""
        stmt = (
            update(Tweet)
            .where(Tweet.tweet_id == tweet_id, Tweet.is_deleted == False)
            .values(likes=Tweet.likes + 1)
            .returning(Tweet.likes, Tweet.retweets, Tweet.views)
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        row = result.fetchone()
        if not row:
            return None

        # Invalidate cache
        await self.redis.delete(f"tweet:{tweet_id}")

        return EngagementResponse(
            tweet_id=tweet_id,
            likes=row.likes,
            retweets=row.retweets,
            views=row.views,
        )

    async def decrement_like(self, tweet_id: str) -> Optional[EngagementResponse]:
        """Decrement like count for a tweet (unlike)."""
        stmt = (
            update(Tweet)
            .where(
                Tweet.tweet_id == tweet_id,
                Tweet.is_deleted == False,
                Tweet.likes > 0,
            )
            .values(likes=Tweet.likes - 1)
            .returning(Tweet.likes, Tweet.retweets, Tweet.views)
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        row = result.fetchone()
        if not row:
            return None

        await self.redis.delete(f"tweet:{tweet_id}")

        return EngagementResponse(
            tweet_id=tweet_id,
            likes=row.likes,
            retweets=row.retweets,
            views=row.views,
        )

    async def increment_retweet(self, tweet_id: str) -> Optional[EngagementResponse]:
        """Increment retweet count."""
        stmt = (
            update(Tweet)
            .where(Tweet.tweet_id == tweet_id, Tweet.is_deleted == False)
            .values(retweets=Tweet.retweets + 1)
            .returning(Tweet.likes, Tweet.retweets, Tweet.views)
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        row = result.fetchone()
        if not row:
            return None

        await self.redis.delete(f"tweet:{tweet_id}")

        return EngagementResponse(
            tweet_id=tweet_id,
            likes=row.likes,
            retweets=row.retweets,
            views=row.views,
        )

    async def increment_view(self, tweet_id: str) -> None:
        """
        Increment view count using Redis buffer.

        Views are high-volume, so we buffer in Redis and flush periodically.
        """
        await self.redis.hincrby(f"views_buffer:{tweet_id}", "count", 1)

    async def flush_view_buffers(self) -> int:
        """
        Flush view buffers from Redis to PostgreSQL.

        Called by background task every 10 seconds.
        """
        cursor = 0
        flushed = 0

        while True:
            cursor, keys = await self.redis.scan(
                cursor, match="views_buffer:*", count=100
            )

            for key in keys:
                tweet_id = key.split(":")[1]
                count = await self.redis.hget(key, "count")

                if count and int(count) > 0:
                    # Update database
                    stmt = (
                        update(Tweet)
                        .where(Tweet.tweet_id == tweet_id)
                        .values(views=Tweet.views + int(count))
                    )
                    await self.db.execute(stmt)

                    # Clear buffer
                    await self.redis.delete(key)
                    flushed += 1

            if cursor == 0:
                break

        if flushed > 0:
            await self.db.commit()

        return flushed

    async def get_user_tweets(
        self,
        user_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[TweetResponse]:
        """Get tweets by a specific user."""
        stmt = (
            select(Tweet)
            .where(Tweet.user_id == user_id, Tweet.is_deleted == False)
            .order_by(Tweet.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await self.db.execute(stmt)
        tweets = result.scalars().all()

        return [
            TweetResponse(
                tweet_id=t.tweet_id,
                user_id=t.user_id,
                text=t.text,
                hashtags=t.hashtags,
                language=t.language,
                geo_lat=float(t.geo_lat) if t.geo_lat else None,
                geo_lon=float(t.geo_lon) if t.geo_lon else None,
                likes=t.likes,
                retweets=t.retweets,
                views=t.views,
                created_at=t.created_at,
            )
            for t in tweets
        ]

    async def cleanup_deleted_tweets(self, hours: int = 24) -> int:
        """
        Hard delete tweets that were soft deleted more than X hours ago.

        Called by nightly cleanup job.
        """
        from sqlalchemy import delete

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        stmt = delete(Tweet).where(
            Tweet.is_deleted == True,
            Tweet.deleted_at < cutoff,
        )

        result = await self.db.execute(stmt)
        await self.db.commit()

        return result.rowcount
