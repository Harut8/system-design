from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
import json
import redis.asyncio as redis

from app.config import get_settings
from app.services.kafka_producer import kafka_producer

settings = get_settings()


class PostService:
    """Post Service for 10M tier."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.redis = redis_client

    async def create_post(self, user_id: int, caption: Optional[str], media_url: str, media_type: str = "image") -> dict:
        """Create post and publish Kafka event."""
        # Get user info
        user_result = await self.db.execute(
            text("SELECT username, avatar_url, follower_count, is_celebrity FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        user = user_result.fetchone()

        # Create post
        result = await self.db.execute(
            text("""
                INSERT INTO posts (user_id, caption, media_url, media_type)
                VALUES (:user_id, :caption, :media_url, :media_type)
                RETURNING id, user_id, caption, media_url, media_type, like_count, comment_count, created_at
            """),
            {"user_id": user_id, "caption": caption, "media_url": media_url, "media_type": media_type},
        )
        await self.db.commit()
        row = result.fetchone()

        post_data = {
            "id": row.id,
            "user_id": row.user_id,
            "username": user.username if user else "unknown",
            "user_avatar_url": user.avatar_url if user else None,
            "caption": row.caption,
            "media_url": row.media_url,
            "media_type": row.media_type,
            "like_count": row.like_count,
            "comment_count": row.comment_count,
            "is_liked": False,
            "created_at": row.created_at,
        }

        # Cache the post
        await self.redis.setex(f"post:{row.id}", 3600, json.dumps(post_data, default=str))

        # Publish Kafka event for async fan-out
        if user:
            await kafka_producer.publish_post_created(
                row.id, user_id, user.follower_count, user.is_celebrity
            )

        return post_data

    async def get_post(self, post_id: int, viewer_id: Optional[int] = None) -> Optional[dict]:
        """Get single post."""
        # Try cache
        cached = await self.redis.get(f"post:{post_id}")
        if cached:
            data = json.loads(cached)
            if viewer_id:
                result = await self.db.execute(
                    text("SELECT 1 FROM likes WHERE user_id = :uid AND post_id = :pid"),
                    {"uid": viewer_id, "pid": post_id},
                )
                data["is_liked"] = result.fetchone() is not None
            return data

        # Fetch from DB
        result = await self.db.execute(
            text("""
                SELECT p.id, p.user_id, u.username, u.avatar_url as user_avatar_url,
                       p.caption, p.media_url, p.media_type, p.like_count, p.comment_count,
                       p.created_at
                FROM posts p JOIN users u ON p.user_id = u.id
                WHERE p.id = :post_id AND p.is_deleted = FALSE
            """),
            {"post_id": post_id},
        )
        row = result.fetchone()
        if not row:
            return None

        is_liked = False
        if viewer_id:
            like_result = await self.db.execute(
                text("SELECT 1 FROM likes WHERE user_id = :uid AND post_id = :pid"),
                {"uid": viewer_id, "pid": post_id},
            )
            is_liked = like_result.fetchone() is not None

        return {
            "id": row.id,
            "user_id": row.user_id,
            "username": row.username,
            "user_avatar_url": row.user_avatar_url,
            "caption": row.caption,
            "media_url": row.media_url,
            "media_type": row.media_type,
            "like_count": row.like_count,
            "comment_count": row.comment_count,
            "is_liked": is_liked,
            "created_at": row.created_at,
        }

    async def batch_get_posts(self, post_ids: list[int], viewer_id: Optional[int] = None) -> list[dict]:
        """Get multiple posts for feed assembly."""
        if not post_ids:
            return []

        posts = []
        for pid in post_ids:
            post = await self.get_post(pid, viewer_id)
            if post:
                posts.append(post)
        return posts

    async def delete_post(self, post_id: int, user_id: int) -> bool:
        """Delete post and publish event."""
        result = await self.db.execute(
            text("""
                UPDATE posts SET is_deleted = TRUE
                WHERE id = :post_id AND user_id = :user_id AND is_deleted = FALSE
            """),
            {"post_id": post_id, "user_id": user_id},
        )
        await self.db.commit()

        if result.rowcount > 0:
            await self.redis.delete(f"post:{post_id}")
            await kafka_producer.publish_post_deleted(post_id, user_id)
            return True
        return False
