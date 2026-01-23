from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timedelta
import base64
import json
import redis.asyncio as redis

from app.config import get_settings
from app.services.cache import CacheService

settings = get_settings()


class FeedService:
    """
    Feed service for 100K tier.
    Uses Redis caching with pull-based feed generation.
    """

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def get_feed(
        self,
        user_id: int,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str], bool, bool, Optional[int]]:
        """
        Get personalized feed for a user with caching.

        Returns: (posts, next_cursor, has_more, cached, cache_age_seconds)
        """
        # Try cache first
        cached_result = await self.cache.get_cached_feed(user_id, cursor)
        if cached_result:
            posts, cache_age = cached_result
            # Decode next_cursor from cached data
            return posts[:limit], None, len(posts) > limit, True, cache_age

        # Cache miss - generate feed from database
        posts, next_cursor, has_more = await self._generate_feed_from_db(
            user_id, limit, cursor
        )

        # Cache the results
        await self.cache.set_cached_feed(
            user_id, posts, cursor, next_cursor, has_more
        )

        return posts, next_cursor, has_more, False, None

    async def _generate_feed_from_db(
        self,
        user_id: int,
        limit: int,
        cursor: Optional[str],
    ) -> tuple[list[dict], Optional[str], bool]:
        """Generate feed from database (cache miss path)."""
        cursor_time = None
        if cursor:
            try:
                decoded = base64.b64decode(cursor).decode('utf-8')
                cursor_data = json.loads(decoded)
                cursor_time = datetime.fromisoformat(cursor_data['created_at'])
            except Exception:
                cursor_time = None

        days_ago = datetime.utcnow() - timedelta(days=settings.feed_days_limit)
        time_filter = "AND p.created_at < :cursor_time" if cursor_time else ""

        query = f"""
            SELECT
                p.id,
                p.user_id,
                u.username,
                u.avatar_url as user_avatar_url,
                p.caption,
                p.media_url,
                p.media_type,
                p.like_count,
                p.comment_count,
                p.created_at,
                EXISTS(
                    SELECT 1 FROM likes l
                    WHERE l.post_id = p.id AND l.user_id = :user_id
                ) as is_liked
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.user_id IN (
                SELECT followee_id FROM follows WHERE follower_id = :user_id
            )
            AND p.is_deleted = FALSE
            AND p.created_at > :days_ago
            {time_filter}
            ORDER BY p.created_at DESC
            LIMIT :limit_plus_one
        """

        params = {
            "user_id": user_id,
            "days_ago": days_ago,
            "limit_plus_one": limit + 1,
        }
        if cursor_time:
            params["cursor_time"] = cursor_time

        result = await self.db.execute(text(query), params)
        rows = result.fetchall()

        has_more = len(rows) > limit
        posts = []

        for row in rows[:limit]:
            posts.append({
                "id": row.id,
                "user_id": row.user_id,
                "username": row.username,
                "user_avatar_url": row.user_avatar_url,
                "caption": row.caption,
                "media_url": row.media_url,
                "media_type": row.media_type,
                "like_count": row.like_count,
                "comment_count": row.comment_count,
                "is_liked": row.is_liked,
                "created_at": row.created_at,
            })

        next_cursor = None
        if has_more and posts:
            last_post = posts[-1]
            cursor_data = {"created_at": last_post["created_at"].isoformat()}
            next_cursor = base64.b64encode(
                json.dumps(cursor_data).encode('utf-8')
            ).decode('utf-8')

        return posts, next_cursor, has_more


class PostService:
    """Post service for 100K tier with caching."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def create_post(
        self,
        user_id: int,
        caption: Optional[str],
        media_url: str,
        media_type: str = "image",
    ) -> dict:
        """Create a new post and fan out to followers if applicable."""
        result = await self.db.execute(
            text("""
                INSERT INTO posts (user_id, caption, media_url, media_type)
                VALUES (:user_id, :caption, :media_url, :media_type)
                RETURNING id, user_id, caption, media_url, media_type,
                          like_count, comment_count, created_at
            """),
            {
                "user_id": user_id,
                "caption": caption,
                "media_url": media_url,
                "media_type": media_type,
            },
        )
        await self.db.commit()
        row = result.fetchone()

        user_result = await self.db.execute(
            text("SELECT username, avatar_url, follower_count FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        user = user_result.fetchone()

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

        # Fan-out-on-write for small accounts
        if user and user.follower_count < settings.push_threshold:
            await self._fan_out_to_followers(row.id, user_id)

        # Cache the post
        await self.cache.set_cached_post(row.id, post_data)

        return post_data

    async def _fan_out_to_followers(self, post_id: int, author_id: int) -> None:
        """Push post to followers' feed caches (for small accounts)."""
        # Get follower IDs
        result = await self.db.execute(
            text("SELECT follower_id FROM follows WHERE followee_id = :author_id"),
            {"author_id": author_id},
        )
        follower_ids = [row[0] for row in result.fetchall()]

        # Push to each follower's feed and invalidate their cached feeds
        for follower_id in follower_ids:
            await self.cache.push_to_feed(follower_id, post_id)

        # Invalidate first page cache for all followers
        await self.cache.invalidate_follower_feeds(author_id, follower_ids)

    async def get_post(self, post_id: int, viewer_user_id: Optional[int] = None) -> Optional[dict]:
        """Get a single post by ID with caching."""
        # Try cache first
        cached = await self.cache.get_cached_post(post_id)
        if cached:
            # Update is_liked based on viewer
            if viewer_user_id:
                result = await self.db.execute(
                    text("SELECT 1 FROM likes WHERE user_id = :uid AND post_id = :pid"),
                    {"uid": viewer_user_id, "pid": post_id},
                )
                cached["is_liked"] = result.fetchone() is not None
            return cached

        # Cache miss
        query = """
            SELECT
                p.id,
                p.user_id,
                u.username,
                u.avatar_url as user_avatar_url,
                p.caption,
                p.media_url,
                p.media_type,
                p.like_count,
                p.comment_count,
                p.created_at,
                CASE
                    WHEN :viewer_id IS NOT NULL THEN EXISTS(
                        SELECT 1 FROM likes l
                        WHERE l.post_id = p.id AND l.user_id = :viewer_id
                    )
                    ELSE FALSE
                END as is_liked
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.id = :post_id AND p.is_deleted = FALSE
        """

        result = await self.db.execute(
            text(query),
            {"post_id": post_id, "viewer_id": viewer_user_id},
        )
        row = result.fetchone()

        if not row:
            return None

        post_data = {
            "id": row.id,
            "user_id": row.user_id,
            "username": row.username,
            "user_avatar_url": row.user_avatar_url,
            "caption": row.caption,
            "media_url": row.media_url,
            "media_type": row.media_type,
            "like_count": row.like_count,
            "comment_count": row.comment_count,
            "is_liked": row.is_liked,
            "created_at": row.created_at,
        }

        # Cache without is_liked (user-specific)
        cache_data = {**post_data, "is_liked": False}
        await self.cache.set_cached_post(post_id, cache_data)

        return post_data

    async def delete_post(self, post_id: int, user_id: int) -> bool:
        """Soft delete a post and invalidate cache."""
        result = await self.db.execute(
            text("""
                UPDATE posts
                SET is_deleted = TRUE, updated_at = NOW()
                WHERE id = :post_id AND user_id = :user_id AND is_deleted = FALSE
            """),
            {"post_id": post_id, "user_id": user_id},
        )
        await self.db.commit()

        if result.rowcount > 0:
            await self.cache.invalidate_post_cache(post_id)
            return True

        return False

    async def get_user_posts(
        self,
        user_id: int,
        viewer_user_id: Optional[int] = None,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str], bool]:
        """Get posts by a specific user."""
        cursor_time = None
        if cursor:
            try:
                decoded = base64.b64decode(cursor).decode('utf-8')
                cursor_data = json.loads(decoded)
                cursor_time = datetime.fromisoformat(cursor_data['created_at'])
            except Exception:
                cursor_time = None

        time_filter = "AND p.created_at < :cursor_time" if cursor_time else ""

        query = f"""
            SELECT
                p.id,
                p.user_id,
                u.username,
                u.avatar_url as user_avatar_url,
                p.caption,
                p.media_url,
                p.media_type,
                p.like_count,
                p.comment_count,
                p.created_at,
                CASE
                    WHEN :viewer_id IS NOT NULL THEN EXISTS(
                        SELECT 1 FROM likes l
                        WHERE l.post_id = p.id AND l.user_id = :viewer_id
                    )
                    ELSE FALSE
                END as is_liked
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.user_id = :user_id
            AND p.is_deleted = FALSE
            {time_filter}
            ORDER BY p.created_at DESC
            LIMIT :limit_plus_one
        """

        params = {
            "user_id": user_id,
            "viewer_id": viewer_user_id,
            "limit_plus_one": limit + 1,
        }
        if cursor_time:
            params["cursor_time"] = cursor_time

        result = await self.db.execute(text(query), params)
        rows = result.fetchall()

        has_more = len(rows) > limit
        posts = []

        for row in rows[:limit]:
            posts.append({
                "id": row.id,
                "user_id": row.user_id,
                "username": row.username,
                "user_avatar_url": row.user_avatar_url,
                "caption": row.caption,
                "media_url": row.media_url,
                "media_type": row.media_type,
                "like_count": row.like_count,
                "comment_count": row.comment_count,
                "is_liked": row.is_liked,
                "created_at": row.created_at,
            })

        next_cursor = None
        if has_more and posts:
            last_post = posts[-1]
            cursor_data = {"created_at": last_post["created_at"].isoformat()}
            next_cursor = base64.b64encode(
                json.dumps(cursor_data).encode('utf-8')
            ).decode('utf-8')

        return posts, next_cursor, has_more
