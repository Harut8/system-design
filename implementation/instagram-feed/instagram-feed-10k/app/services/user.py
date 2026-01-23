from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from typing import Optional


class UserService:
    """User service for 10K tier."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_user(
        self,
        username: str,
        email: str,
        display_name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        bio: Optional[str] = None,
    ) -> Optional[dict]:
        """Create a new user."""
        try:
            result = await self.db.execute(
                text("""
                    INSERT INTO users (username, email, display_name, avatar_url, bio)
                    VALUES (:username, :email, :display_name, :avatar_url, :bio)
                    RETURNING id, username, email, display_name, avatar_url, bio,
                              follower_count, following_count, post_count, created_at
                """),
                {
                    "username": username,
                    "email": email,
                    "display_name": display_name,
                    "avatar_url": avatar_url,
                    "bio": bio,
                },
            )
            await self.db.commit()
            row = result.fetchone()

            return {
                "id": row.id,
                "username": row.username,
                "email": row.email,
                "display_name": row.display_name,
                "avatar_url": row.avatar_url,
                "bio": row.bio,
                "follower_count": row.follower_count,
                "following_count": row.following_count,
                "post_count": row.post_count,
                "created_at": row.created_at,
            }
        except IntegrityError:
            await self.db.rollback()
            return None

    async def get_user(self, user_id: int) -> Optional[dict]:
        """Get user by ID."""
        result = await self.db.execute(
            text("""
                SELECT id, username, display_name, avatar_url, bio,
                       follower_count, following_count, post_count, created_at
                FROM users
                WHERE id = :user_id
            """),
            {"user_id": user_id},
        )
        row = result.fetchone()

        if not row:
            return None

        return {
            "id": row.id,
            "username": row.username,
            "display_name": row.display_name,
            "avatar_url": row.avatar_url,
            "bio": row.bio,
            "follower_count": row.follower_count,
            "following_count": row.following_count,
            "post_count": row.post_count,
            "created_at": row.created_at,
        }

    async def get_user_by_username(self, username: str) -> Optional[dict]:
        """Get user by username."""
        result = await self.db.execute(
            text("""
                SELECT id, username, display_name, avatar_url, bio,
                       follower_count, following_count, post_count, created_at
                FROM users
                WHERE username = :username
            """),
            {"username": username},
        )
        row = result.fetchone()

        if not row:
            return None

        return {
            "id": row.id,
            "username": row.username,
            "display_name": row.display_name,
            "avatar_url": row.avatar_url,
            "bio": row.bio,
            "follower_count": row.follower_count,
            "following_count": row.following_count,
            "post_count": row.post_count,
            "created_at": row.created_at,
        }

    async def update_user(
        self,
        user_id: int,
        display_name: Optional[str] = None,
        avatar_url: Optional[str] = None,
        bio: Optional[str] = None,
    ) -> Optional[dict]:
        """Update user profile."""
        updates = []
        params = {"user_id": user_id}

        if display_name is not None:
            updates.append("display_name = :display_name")
            params["display_name"] = display_name
        if avatar_url is not None:
            updates.append("avatar_url = :avatar_url")
            params["avatar_url"] = avatar_url
        if bio is not None:
            updates.append("bio = :bio")
            params["bio"] = bio

        if not updates:
            return await self.get_user(user_id)

        updates.append("updated_at = NOW()")

        query = f"""
            UPDATE users
            SET {", ".join(updates)}
            WHERE id = :user_id
            RETURNING id, username, display_name, avatar_url, bio,
                      follower_count, following_count, post_count, created_at
        """

        result = await self.db.execute(text(query), params)
        await self.db.commit()
        row = result.fetchone()

        if not row:
            return None

        return {
            "id": row.id,
            "username": row.username,
            "display_name": row.display_name,
            "avatar_url": row.avatar_url,
            "bio": row.bio,
            "follower_count": row.follower_count,
            "following_count": row.following_count,
            "post_count": row.post_count,
            "created_at": row.created_at,
        }

    async def search_users(
        self, query: str, limit: int = 20
    ) -> list[dict]:
        """Search users by username or display name."""
        result = await self.db.execute(
            text("""
                SELECT id, username, display_name, avatar_url,
                       follower_count, following_count
                FROM users
                WHERE username ILIKE :query OR display_name ILIKE :query
                ORDER BY follower_count DESC
                LIMIT :limit
            """),
            {"query": f"%{query}%", "limit": limit},
        )
        rows = result.fetchall()

        return [
            {
                "id": row.id,
                "username": row.username,
                "display_name": row.display_name,
                "avatar_url": row.avatar_url,
                "follower_count": row.follower_count,
                "following_count": row.following_count,
            }
            for row in rows
        ]
