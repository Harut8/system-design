from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
import redis.asyncio as redis

from app.services.cache import CacheService


class SocialService:
    """Social graph service for 1M tier."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def follow(self, follower_id: int, followee_id: int) -> tuple[str, int]:
        if follower_id == followee_id:
            count = await self._get_follower_count(followee_id)
            return "cannot_follow_self", count

        try:
            await self.db.execute(
                text("INSERT INTO follows (follower_id, followee_id) VALUES (:follower_id, :followee_id)"),
                {"follower_id": follower_id, "followee_id": followee_id},
            )
            await self.db.commit()

            await self.cache.invalidate_user_cache(followee_id)
            await self.cache.invalidate_feed_cache(follower_id)

            count = await self._get_follower_count(followee_id)
            return "followed", count
        except IntegrityError:
            await self.db.rollback()
            count = await self._get_follower_count(followee_id)
            return "already_following", count

    async def unfollow(self, follower_id: int, followee_id: int) -> tuple[str, int]:
        result = await self.db.execute(
            text("DELETE FROM follows WHERE follower_id = :follower_id AND followee_id = :followee_id"),
            {"follower_id": follower_id, "followee_id": followee_id},
        )
        await self.db.commit()

        count = await self._get_follower_count(followee_id)
        if result.rowcount == 0:
            return "not_following", count

        await self.cache.invalidate_user_cache(followee_id)
        await self.cache.invalidate_feed_cache(follower_id)
        return "unfollowed", count

    async def get_followers(self, user_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
        result = await self.db.execute(
            text("""
                SELECT u.id, u.username, u.display_name, u.avatar_url
                FROM users u JOIN follows f ON u.id = f.follower_id
                WHERE f.followee_id = :user_id
                ORDER BY f.created_at DESC LIMIT :limit OFFSET :offset
            """),
            {"user_id": user_id, "limit": limit, "offset": offset},
        )
        return [{"id": r.id, "username": r.username, "display_name": r.display_name, "avatar_url": r.avatar_url} for r in result.fetchall()]

    async def get_following(self, user_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
        result = await self.db.execute(
            text("""
                SELECT u.id, u.username, u.display_name, u.avatar_url
                FROM users u JOIN follows f ON u.id = f.followee_id
                WHERE f.follower_id = :user_id
                ORDER BY f.created_at DESC LIMIT :limit OFFSET :offset
            """),
            {"user_id": user_id, "limit": limit, "offset": offset},
        )
        return [{"id": r.id, "username": r.username, "display_name": r.display_name, "avatar_url": r.avatar_url} for r in result.fetchall()]

    async def _get_follower_count(self, user_id: int) -> int:
        result = await self.db.execute(
            text("SELECT follower_count FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        row = result.fetchone()
        return row.follower_count if row else 0


class InteractionService:
    """Interaction service for 1M tier."""

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def like_post(self, user_id: int, post_id: int) -> tuple[str, int]:
        try:
            await self.db.execute(
                text("INSERT INTO likes (user_id, post_id) VALUES (:user_id, :post_id)"),
                {"user_id": user_id, "post_id": post_id},
            )
            await self.db.commit()
            await self.cache.invalidate_post_cache(post_id)
            count = await self._get_like_count(post_id)
            return "liked", count
        except IntegrityError:
            await self.db.rollback()
            count = await self._get_like_count(post_id)
            return "already_liked", count

    async def unlike_post(self, user_id: int, post_id: int) -> tuple[str, int]:
        result = await self.db.execute(
            text("DELETE FROM likes WHERE user_id = :user_id AND post_id = :post_id"),
            {"user_id": user_id, "post_id": post_id},
        )
        await self.db.commit()
        count = await self._get_like_count(post_id)
        if result.rowcount == 0:
            return "not_liked", count
        await self.cache.invalidate_post_cache(post_id)
        return "unliked", count

    async def add_comment(self, user_id: int, post_id: int, content: str) -> dict:
        result = await self.db.execute(
            text("INSERT INTO comments (user_id, post_id, content) VALUES (:user_id, :post_id, :content) RETURNING id, created_at"),
            {"user_id": user_id, "post_id": post_id, "content": content},
        )
        await self.db.commit()
        row = result.fetchone()
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

    async def get_comments(self, post_id: int, limit: int = 20, offset: int = 0) -> list[dict]:
        result = await self.db.execute(
            text("""
                SELECT c.id, c.user_id, u.username, u.avatar_url as user_avatar_url, c.post_id, c.content, c.created_at
                FROM comments c JOIN users u ON c.user_id = u.id
                WHERE c.post_id = :post_id AND c.is_deleted = FALSE
                ORDER BY c.created_at ASC LIMIT :limit OFFSET :offset
            """),
            {"post_id": post_id, "limit": limit, "offset": offset},
        )
        return [
            {"id": r.id, "user_id": r.user_id, "username": r.username, "user_avatar_url": r.user_avatar_url, "post_id": r.post_id, "content": r.content, "created_at": r.created_at}
            for r in result.fetchall()
        ]

    async def delete_comment(self, comment_id: int, user_id: int) -> bool:
        result = await self.db.execute(
            text("SELECT post_id FROM comments WHERE id = :comment_id"),
            {"comment_id": comment_id},
        )
        row = result.fetchone()
        post_id = row.post_id if row else None

        result = await self.db.execute(
            text("UPDATE comments SET is_deleted = TRUE WHERE id = :comment_id AND user_id = :user_id AND is_deleted = FALSE"),
            {"comment_id": comment_id, "user_id": user_id},
        )
        await self.db.commit()

        if result.rowcount > 0 and post_id:
            await self.cache.invalidate_post_cache(post_id)
            return True
        return False

    async def _get_like_count(self, post_id: int) -> int:
        result = await self.db.execute(
            text("SELECT like_count FROM posts WHERE id = :post_id"),
            {"post_id": post_id},
        )
        row = result.fetchone()
        return row.like_count if row else 0
