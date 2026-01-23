from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from typing import Optional
import redis.asyncio as redis

from app.services.cache import CacheService


class SocialService:
    """Social graph service for 100K tier with caching."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def follow(self, follower_id: int, followee_id: int) -> tuple[str, int]:
        """Follow a user."""
        if follower_id == followee_id:
            count = await self._get_follower_count(followee_id)
            return "cannot_follow_self", count

        try:
            await self.db.execute(
                text("""
                    INSERT INTO follows (follower_id, followee_id)
                    VALUES (:follower_id, :followee_id)
                """),
                {"follower_id": follower_id, "followee_id": followee_id},
            )
            await self.db.commit()

            # Invalidate caches
            await self.cache.invalidate_user_cache(followee_id)
            await self.cache.invalidate_feed_cache(follower_id)

            count = await self._get_follower_count(followee_id)
            return "followed", count

        except IntegrityError:
            await self.db.rollback()
            count = await self._get_follower_count(followee_id)
            return "already_following", count

    async def unfollow(self, follower_id: int, followee_id: int) -> tuple[str, int]:
        """Unfollow a user."""
        result = await self.db.execute(
            text("""
                DELETE FROM follows
                WHERE follower_id = :follower_id AND followee_id = :followee_id
            """),
            {"follower_id": follower_id, "followee_id": followee_id},
        )
        await self.db.commit()

        count = await self._get_follower_count(followee_id)

        if result.rowcount == 0:
            return "not_following", count

        # Invalidate caches
        await self.cache.invalidate_user_cache(followee_id)
        await self.cache.invalidate_feed_cache(follower_id)

        return "unfollowed", count

    async def is_following(self, follower_id: int, followee_id: int) -> bool:
        """Check if a user is following another user."""
        result = await self.db.execute(
            text("""
                SELECT 1 FROM follows
                WHERE follower_id = :follower_id AND followee_id = :followee_id
                LIMIT 1
            """),
            {"follower_id": follower_id, "followee_id": followee_id},
        )
        return result.fetchone() is not None

    async def get_followers(
        self, user_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """Get list of users who follow this user."""
        result = await self.db.execute(
            text("""
                SELECT u.id, u.username, u.display_name, u.avatar_url
                FROM users u
                JOIN follows f ON u.id = f.follower_id
                WHERE f.followee_id = :user_id
                ORDER BY f.created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"user_id": user_id, "limit": limit, "offset": offset},
        )
        rows = result.fetchall()

        return [
            {
                "id": row.id,
                "username": row.username,
                "display_name": row.display_name,
                "avatar_url": row.avatar_url,
            }
            for row in rows
        ]

    async def get_following(
        self, user_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """Get list of users this user follows."""
        result = await self.db.execute(
            text("""
                SELECT u.id, u.username, u.display_name, u.avatar_url
                FROM users u
                JOIN follows f ON u.id = f.followee_id
                WHERE f.follower_id = :user_id
                ORDER BY f.created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {"user_id": user_id, "limit": limit, "offset": offset},
        )
        rows = result.fetchall()

        return [
            {
                "id": row.id,
                "username": row.username,
                "display_name": row.display_name,
                "avatar_url": row.avatar_url,
            }
            for row in rows
        ]

    async def _get_follower_count(self, user_id: int) -> int:
        """Get follower count for a user."""
        result = await self.db.execute(
            text("SELECT follower_count FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        row = result.fetchone()
        return row.follower_count if row else 0


class InteractionService:
    """Interaction service (likes, comments) for 100K tier."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def like_post(self, user_id: int, post_id: int) -> tuple[str, int]:
        """Like a post."""
        try:
            await self.db.execute(
                text("""
                    INSERT INTO likes (user_id, post_id)
                    VALUES (:user_id, :post_id)
                """),
                {"user_id": user_id, "post_id": post_id},
            )
            await self.db.commit()

            # Invalidate post cache
            await self.cache.invalidate_post_cache(post_id)

            count = await self._get_like_count(post_id)
            return "liked", count

        except IntegrityError:
            await self.db.rollback()
            count = await self._get_like_count(post_id)
            return "already_liked", count

    async def unlike_post(self, user_id: int, post_id: int) -> tuple[str, int]:
        """Unlike a post."""
        result = await self.db.execute(
            text("""
                DELETE FROM likes
                WHERE user_id = :user_id AND post_id = :post_id
            """),
            {"user_id": user_id, "post_id": post_id},
        )
        await self.db.commit()

        count = await self._get_like_count(post_id)

        if result.rowcount == 0:
            return "not_liked", count

        # Invalidate post cache
        await self.cache.invalidate_post_cache(post_id)

        return "unliked", count

    async def add_comment(
        self, user_id: int, post_id: int, content: str
    ) -> dict:
        """Add a comment to a post."""
        result = await self.db.execute(
            text("""
                INSERT INTO comments (user_id, post_id, content)
                VALUES (:user_id, :post_id, :content)
                RETURNING id, created_at
            """),
            {"user_id": user_id, "post_id": post_id, "content": content},
        )
        await self.db.commit()
        row = result.fetchone()

        # Invalidate post cache (comment count changed)
        await self.cache.invalidate_post_cache(post_id)

        user_result = await self.db.execute(
            text("SELECT username, avatar_url FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        user = user_result.fetchone()

        return {
            "id": row.id,
            "user_id": user_id,
            "username": user.username if user else "unknown",
            "user_avatar_url": user.avatar_url if user else None,
            "post_id": post_id,
            "content": content,
            "created_at": row.created_at,
        }

    async def get_comments(
        self, post_id: int, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        """Get comments for a post."""
        result = await self.db.execute(
            text("""
                SELECT
                    c.id,
                    c.user_id,
                    u.username,
                    u.avatar_url as user_avatar_url,
                    c.post_id,
                    c.content,
                    c.created_at
                FROM comments c
                JOIN users u ON c.user_id = u.id
                WHERE c.post_id = :post_id AND c.is_deleted = FALSE
                ORDER BY c.created_at ASC
                LIMIT :limit OFFSET :offset
            """),
            {"post_id": post_id, "limit": limit, "offset": offset},
        )
        rows = result.fetchall()

        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "username": row.username,
                "user_avatar_url": row.user_avatar_url,
                "post_id": row.post_id,
                "content": row.content,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    async def delete_comment(self, comment_id: int, user_id: int) -> bool:
        """Soft delete a comment."""
        # Get post_id first for cache invalidation
        result = await self.db.execute(
            text("SELECT post_id FROM comments WHERE id = :comment_id"),
            {"comment_id": comment_id},
        )
        row = result.fetchone()
        post_id = row.post_id if row else None

        result = await self.db.execute(
            text("""
                UPDATE comments
                SET is_deleted = TRUE
                WHERE id = :comment_id AND user_id = :user_id AND is_deleted = FALSE
            """),
            {"comment_id": comment_id, "user_id": user_id},
        )
        await self.db.commit()

        if result.rowcount > 0 and post_id:
            await self.cache.invalidate_post_cache(post_id)
            return True

        return False

    async def _get_like_count(self, post_id: int) -> int:
        """Get like count for a post."""
        result = await self.db.execute(
            text("SELECT like_count FROM posts WHERE id = :post_id"),
            {"post_id": post_id},
        )
        row = result.fetchone()
        return row.like_count if row else 0
