from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional
from datetime import datetime, timedelta
import base64
import json
import redis.asyncio as redis

from app.config import get_settings
from app.services.cache import CacheService
from app.workers.tasks import fan_out_post, store_celebrity_post

settings = get_settings()


class FeedService:
    """
    Feed service for 1M tier.
    Implements hybrid fan-out strategy:
    - Push for regular users (< celebrity_threshold followers)
    - Pull for celebrities (>= celebrity_threshold followers)
    """

    def __init__(self, db: AsyncSession, redis_client: redis.Redis):
        self.db = db
        self.cache = CacheService(redis_client)

    async def get_feed(
        self,
        user_id: int,
        limit: int = 20,
        cursor: Optional[str] = None,
    ) -> tuple[list[dict], Optional[str], bool, bool, Optional[int], int]:
        """
        Get personalized feed with hybrid strategy.
        Returns: (posts, next_cursor, has_more, cached, cache_age_seconds, celebrity_posts_count)
        """
        # Try cache first (for first page)
        if not cursor:
            cached_result = await self.cache.get_cached_feed(user_id, cursor)
            if cached_result:
                posts, cache_age = cached_result
                return posts[:limit], None, len(posts) > limit, True, cache_age, 0

        # Hybrid feed assembly
        posts, next_cursor, has_more, celebrity_count = await self._assemble_hybrid_feed(
            user_id, limit, cursor
        )

        # Cache first page
        if not cursor:
            await self.cache.set_cached_feed(
                user_id, posts, cursor, next_cursor, has_more
            )

        return posts, next_cursor, has_more, False, None, celebrity_count

    async def _assemble_hybrid_feed(
        self,
        user_id: int,
        limit: int,
        cursor: Optional[str],
    ) -> tuple[list[dict], Optional[str], bool, int]:
        """Assemble feed from pushed content + celebrity posts."""

        # 1. Get pre-pushed post IDs from cache
        feed_ids = await self.cache.get_feed_ids(user_id, 0, limit * 3)

        # 2. Get celebrity followees
        celebrity_ids = await self._get_celebrity_followees(user_id)

        # 3. Get recent posts from celebrities
        celebrity_post_ids = []
        for celeb_id in celebrity_ids[:20]:  # Limit to 20 celebrities
            posts = await self.cache.get_celebrity_posts(celeb_id, 10)
            celebrity_post_ids.extend(posts)

        # 4. Merge and dedupe
        all_ids = list(dict.fromkeys(feed_ids + celebrity_post_ids))

        # 5. If not enough posts, fall back to database query
        if len(all_ids) < limit:
            db_posts = await self._get_feed_from_db(user_id, limit * 2, cursor)
            db_ids = [p["id"] for p in db_posts]
            all_ids = list(dict.fromkeys(all_ids + db_ids))

        # 6. Fetch full post data
        posts = await self._hydrate_posts(all_ids[:limit * 2], user_id)

        # 7. Sort by created_at
        posts.sort(key=lambda p: p["created_at"], reverse=True)

        # 8. Apply cursor filtering
        if cursor:
            cursor_time = self._decode_cursor(cursor)
            if cursor_time:
                posts = [p for p in posts if p["created_at"] < cursor_time]

        # 9. Limit and generate next cursor
        has_more = len(posts) > limit
        posts = posts[:limit]

        next_cursor = None
        if has_more and posts:
            next_cursor = self._encode_cursor(posts[-1]["created_at"])

        celebrity_count = len([p for p in posts if p["id"] in celebrity_post_ids])

        return posts, next_cursor, has_more, celebrity_count

    async def _get_celebrity_followees(self, user_id: int) -> list[int]:
        """Get list of celebrities this user follows."""
        # Try cache first
        cached = await self.cache.get_celebrity_followees(user_id)
        if cached:
            return cached

        # Query database
        result = await self.db.execute(
            text("""
                SELECT u.id FROM users u
                JOIN follows f ON u.id = f.followee_id
                WHERE f.follower_id = :user_id AND u.is_celebrity = TRUE
            """),
            {"user_id": user_id},
        )
        celebrity_ids = [row[0] for row in result.fetchall()]

        # Cache result
        await self.cache.set_celebrity_followees(user_id, celebrity_ids)

        return celebrity_ids

    async def _get_feed_from_db(
        self,
        user_id: int,
        limit: int,
        cursor: Optional[str],
    ) -> list[dict]:
        """Fallback: get feed from database."""
        cursor_time = self._decode_cursor(cursor) if cursor else None
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
                    SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = :user_id
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
            LIMIT :limit
        """

        params = {"user_id": user_id, "days_ago": days_ago, "limit": limit}
        if cursor_time:
            params["cursor_time"] = cursor_time

        result = await self.db.execute(text(query), params)
        return [self._row_to_dict(row) for row in result.fetchall()]

    async def _hydrate_posts(self, post_ids: list[int], viewer_id: int) -> list[dict]:
        """Fetch full post data for given IDs."""
        if not post_ids:
            return []

        # Try cache first
        cached = await self.cache.batch_get_posts(post_ids)
        missing_ids = [pid for pid in post_ids if pid not in cached]

        # Fetch missing from database
        if missing_ids:
            placeholders = ", ".join([f":id_{i}" for i in range(len(missing_ids))])
            params = {f"id_{i}": pid for i, pid in enumerate(missing_ids)}
            params["viewer_id"] = viewer_id

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
                        SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = :viewer_id
                    ) as is_liked
                FROM posts p
                JOIN users u ON p.user_id = u.id
                WHERE p.id IN ({placeholders}) AND p.is_deleted = FALSE
            """

            result = await self.db.execute(text(query), params)
            for row in result.fetchall():
                post_data = self._row_to_dict(row)
                cached[post_data["id"]] = post_data
                # Cache without is_liked
                await self.cache.set_cached_post(
                    post_data["id"],
                    {**post_data, "is_liked": False}
                )

        # Return in order
        return [cached[pid] for pid in post_ids if pid in cached]

    def _row_to_dict(self, row) -> dict:
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

    def _decode_cursor(self, cursor: str) -> Optional[datetime]:
        try:
            decoded = base64.b64decode(cursor).decode('utf-8')
            cursor_data = json.loads(decoded)
            return datetime.fromisoformat(cursor_data['created_at'])
        except Exception:
            return None

    def _encode_cursor(self, created_at: datetime) -> str:
        cursor_data = {"created_at": created_at.isoformat()}
        return base64.b64encode(
            json.dumps(cursor_data).encode('utf-8')
        ).decode('utf-8')


class PostService:
    """Post service for 1M tier with async fan-out."""

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
        """Create a new post and trigger async fan-out."""
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
            text("SELECT username, avatar_url, follower_count, is_celebrity FROM users WHERE id = :user_id"),
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

        # Trigger async fan-out based on user type
        if user:
            if user.is_celebrity:
                # Store in celebrity posts for pull-based retrieval
                store_celebrity_post.delay(row.id, user_id)
            else:
                # Fan-out to followers asynchronously
                fan_out_post.delay(row.id, user_id, user.follower_count)

        # Cache the post
        await self.cache.set_cached_post(row.id, post_data)

        return post_data

    async def get_post(self, post_id: int, viewer_user_id: Optional[int] = None) -> Optional[dict]:
        """Get a single post by ID."""
        cached = await self.cache.get_cached_post(post_id)
        if cached:
            if viewer_user_id:
                result = await self.db.execute(
                    text("SELECT 1 FROM likes WHERE user_id = :uid AND post_id = :pid"),
                    {"uid": viewer_user_id, "pid": post_id},
                )
                cached["is_liked"] = result.fetchone() is not None
            return cached

        query = """
            SELECT
                p.id, p.user_id, u.username, u.avatar_url as user_avatar_url,
                p.caption, p.media_url, p.media_type, p.like_count, p.comment_count,
                p.created_at,
                CASE
                    WHEN :viewer_id IS NOT NULL THEN EXISTS(
                        SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = :viewer_id
                    )
                    ELSE FALSE
                END as is_liked
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.id = :post_id AND p.is_deleted = FALSE
        """

        result = await self.db.execute(
            text(query), {"post_id": post_id, "viewer_id": viewer_user_id}
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

        await self.cache.set_cached_post(post_id, {**post_data, "is_liked": False})
        return post_data

    async def delete_post(self, post_id: int, user_id: int) -> bool:
        """Soft delete a post."""
        result = await self.db.execute(
            text("""
                UPDATE posts SET is_deleted = TRUE, updated_at = NOW()
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
                pass

        time_filter = "AND p.created_at < :cursor_time" if cursor_time else ""

        query = f"""
            SELECT
                p.id, p.user_id, u.username, u.avatar_url as user_avatar_url,
                p.caption, p.media_url, p.media_type, p.like_count, p.comment_count,
                p.created_at,
                CASE
                    WHEN :viewer_id IS NOT NULL THEN EXISTS(
                        SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = :viewer_id
                    )
                    ELSE FALSE
                END as is_liked
            FROM posts p
            JOIN users u ON p.user_id = u.id
            WHERE p.user_id = :user_id AND p.is_deleted = FALSE
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
        posts = [
            {
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
            for row in rows[:limit]
        ]

        next_cursor = None
        if has_more and posts:
            cursor_data = {"created_at": posts[-1]["created_at"].isoformat()}
            next_cursor = base64.b64encode(
                json.dumps(cursor_data).encode('utf-8')
            ).decode('utf-8')

        return posts, next_cursor, has_more
