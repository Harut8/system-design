from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from typing import Optional

from app.schemas import item_type_to_int


class CounterService:
    """Service for counter operations."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def like(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int]:
        """
        Like an item. Returns (status, count).
        Uses PostgreSQL transaction for atomicity.
        """
        item_type_int = item_type_to_int(item_type)

        try:
            # Insert like record (will fail if duplicate due to UNIQUE constraint)
            await self.db.execute(
                text("""
                    INSERT INTO user_likes (user_id, item_id, item_type)
                    VALUES (:user_id, :item_id, :item_type)
                """),
                {"user_id": user_id, "item_id": item_id, "item_type": item_type_int},
            )

            # Increment counter (upsert)
            await self.db.execute(
                text("""
                    INSERT INTO counters (item_id, item_type, like_count)
                    VALUES (:item_id, :item_type, 1)
                    ON CONFLICT (item_id, item_type)
                    DO UPDATE SET like_count = counters.like_count + 1,
                                  updated_at = NOW()
                """),
                {"item_id": item_id, "item_type": item_type_int},
            )

            await self.db.commit()

            # Get current count
            count = await self.get_count(item_id, item_type)
            return "liked", count

        except IntegrityError:
            # Duplicate like - already liked
            await self.db.rollback()
            count = await self.get_count(item_id, item_type)
            return "already_liked", count

    async def unlike(self, user_id: int, item_id: int, item_type: str) -> tuple[str, int]:
        """
        Unlike an item. Returns (status, count).
        """
        item_type_int = item_type_to_int(item_type)

        # Delete like record
        result = await self.db.execute(
            text("""
                DELETE FROM user_likes
                WHERE user_id = :user_id AND item_id = :item_id AND item_type = :item_type
            """),
            {"user_id": user_id, "item_id": item_id, "item_type": item_type_int},
        )

        if result.rowcount == 0:
            # Like didn't exist
            count = await self.get_count(item_id, item_type)
            return "not_liked", count

        # Decrement counter
        await self.db.execute(
            text("""
                UPDATE counters
                SET like_count = GREATEST(like_count - 1, 0),
                    updated_at = NOW()
                WHERE item_id = :item_id AND item_type = :item_type
            """),
            {"item_id": item_id, "item_type": item_type_int},
        )

        await self.db.commit()

        count = await self.get_count(item_id, item_type)
        return "unliked", count

    async def get_count(self, item_id: int, item_type: str) -> int:
        """Get like count for an item."""
        item_type_int = item_type_to_int(item_type)

        result = await self.db.execute(
            text("""
                SELECT like_count FROM counters
                WHERE item_id = :item_id AND item_type = :item_type
            """),
            {"item_id": item_id, "item_type": item_type_int},
        )
        row = result.fetchone()
        return row[0] if row else 0

    async def get_counts_batch(
        self, items: list[tuple[int, str]]
    ) -> dict[str, int]:
        """
        Get counts for multiple items in a single query.
        Returns dict of "item_type:item_id" -> count
        """
        if not items:
            return {}

        # Build query parameters
        conditions = []
        params = {}
        for i, (item_id, item_type) in enumerate(items):
            item_type_int = item_type_to_int(item_type)
            conditions.append(f"(item_id = :item_id_{i} AND item_type = :item_type_{i})")
            params[f"item_id_{i}"] = item_id
            params[f"item_type_{i}"] = item_type_int

        query = f"""
            SELECT item_id, item_type, like_count
            FROM counters
            WHERE {" OR ".join(conditions)}
        """

        result = await self.db.execute(text(query), params)
        rows = result.fetchall()

        # Build response dict
        counts = {}
        type_map = {1: "post", 2: "reel", 3: "comment"}

        for item_id, item_type_int, count in rows:
            item_type_str = type_map.get(item_type_int, "post")
            key = f"{item_type_str}:{item_id}"
            counts[key] = count

        # Fill in zeros for items not found
        for item_id, item_type in items:
            key = f"{item_type}:{item_id}"
            if key not in counts:
                counts[key] = 0

        return counts

    async def has_user_liked(
        self, user_id: int, item_ids: list[int], item_type: str
    ) -> dict[str, bool]:
        """
        Check if user has liked multiple items.
        Returns dict of item_id -> liked status
        """
        if not item_ids:
            return {}

        item_type_int = item_type_to_int(item_type)

        result = await self.db.execute(
            text("""
                SELECT item_id FROM user_likes
                WHERE user_id = :user_id
                  AND item_type = :item_type
                  AND item_id = ANY(:item_ids)
            """),
            {
                "user_id": user_id,
                "item_type": item_type_int,
                "item_ids": item_ids,
            },
        )
        liked_items = {row[0] for row in result.fetchall()}

        return {str(item_id): item_id in liked_items for item_id in item_ids}

    async def has_liked_single(
        self, user_id: int, item_id: int, item_type: str
    ) -> bool:
        """Check if user has liked a single item."""
        item_type_int = item_type_to_int(item_type)

        result = await self.db.execute(
            text("""
                SELECT 1 FROM user_likes
                WHERE user_id = :user_id AND item_id = :item_id AND item_type = :item_type
                LIMIT 1
            """),
            {"user_id": user_id, "item_id": item_id, "item_type": item_type_int},
        )
        return result.fetchone() is not None
