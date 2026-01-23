from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timedelta
import base64
import json

from app.config import get_settings

settings = get_settings()


class FeedService:
    """
    Feed service for 10K tier.
    Uses pure pull-based feed generation (fan-out-on-read).
    Simple and efficient for small user bases.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_feed(
        self,
        user_id: int,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str], bool]:
        """
        Get personalized feed for a user.

        Pull-based approach:
        1. Get all users this user follows
        2. Query their posts from the last N days
        3. Sort by created_at DESC
        4. Return paginated results

        Returns: (posts, next_cursor, has_more)
        """
        # Decode cursor (contains last post's created_at)
        cursor_time = None
        if cursor:
            try:
                decoded = base64.b64decode(cursor).decode('utf-8')
                cursor_data = json.loads(decoded)
                cursor_time = datetime.fromisoformat(cursor_data['created_at'])
            except Exception:
                cursor_time = None

        # Calculate time window for feed
        days_ago = datetime.utcnow() - timedelta(days=settings.feed_days_limit)

        # Build query with cursor
        if cursor_time:
            time_filter = "AND p.created_at < :cursor_time"
        else:
            time_filter = ""

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
            "limit_plus_one": limit + 1,  # Fetch one extra to check has_more
        }
        if cursor_time:
            params["cursor_time"] = cursor_time

        result = await self.db.execute(text(query), params)
        rows = result.fetchall()

        # Check if there are more posts
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

        # Generate next cursor
        next_cursor = None
        if has_more and posts:
            last_post = posts[-1]
            cursor_data = {
                "created_at": last_post["created_at"].isoformat()
            }
            next_cursor = base64.b64encode(
                json.dumps(cursor_data).encode('utf-8')
            ).decode('utf-8')

        return posts, next_cursor, has_more


class PostService:
    """Post service for 10K tier."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_post(
        self,
        user_id: int,
        caption: Optional[str],
        media_url: str,
        media_type: str = "image",
    ) -> dict:
        """Create a new post."""
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

        # Get username
        user_result = await self.db.execute(
            text("SELECT username, avatar_url FROM users WHERE id = :user_id"),
            {"user_id": user_id},
        )
        user = user_result.fetchone()

        return {
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

    async def get_post(self, post_id: int, viewer_user_id: Optional[int] = None) -> Optional[dict]:
        """Get a single post by ID."""
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
            "is_liked": row.is_liked,
            "created_at": row.created_at,
        }

    async def delete_post(self, post_id: int, user_id: int) -> bool:
        """Soft delete a post (only by owner)."""
        result = await self.db.execute(
            text("""
                UPDATE posts
                SET is_deleted = TRUE, updated_at = NOW()
                WHERE id = :post_id AND user_id = :user_id AND is_deleted = FALSE
            """),
            {"post_id": post_id, "user_id": user_id},
        )
        await self.db.commit()
        return result.rowcount > 0

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
