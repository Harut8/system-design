import json
from typing import Optional, Set
from uuid import UUID

import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()


class PermissionCache:
    """Redis-based permission cache for fast authorization checks"""

    def __init__(self):
        self.redis: Optional[redis.Redis] = None
        self.ttl = settings.PERMISSION_CACHE_TTL
        self.enabled = settings.ENABLE_PERMISSION_CACHE

    async def connect(self):
        """Initialize Redis connection"""
        if self.enabled:
            self.redis = redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True
            )

    async def disconnect(self):
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()

    def _user_key(self, user_id: UUID, tenant_id: UUID) -> str:
        """Generate cache key for user permissions"""
        return f"rbac:user:{tenant_id}:{user_id}:permissions"

    def _role_key(self, role_id: UUID) -> str:
        """Generate cache key for role permissions"""
        return f"rbac:role:{role_id}:permissions"

    async def get_user_permissions(
        self,
        user_id: UUID,
        tenant_id: UUID
    ) -> Optional[Set[str]]:
        """Get cached user permissions"""
        if not self.enabled or not self.redis:
            return None

        try:
            key = self._user_key(user_id, tenant_id)
            data = await self.redis.get(key)
            if data:
                return set(json.loads(data))
            return None
        except Exception:
            return None

    async def set_user_permissions(
        self,
        user_id: UUID,
        tenant_id: UUID,
        permissions: Set[str]
    ) -> None:
        """Cache user permissions"""
        if not self.enabled or not self.redis:
            return

        try:
            key = self._user_key(user_id, tenant_id)
            await self.redis.setex(
                key,
                self.ttl,
                json.dumps(list(permissions))
            )
        except Exception:
            pass  # Cache failures shouldn't break authorization

    async def invalidate_user(self, user_id: UUID, tenant_id: UUID) -> None:
        """Invalidate user permission cache"""
        if not self.enabled or not self.redis:
            return

        try:
            key = self._user_key(user_id, tenant_id)
            await self.redis.delete(key)
        except Exception:
            pass

    async def invalidate_role(self, role_id: UUID) -> None:
        """Invalidate all users with a specific role"""
        if not self.enabled or not self.redis:
            return

        try:
            # Use pattern matching to invalidate related caches
            pattern = "rbac:user:*"
            cursor = 0
            while True:
                cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
                if keys:
                    await self.redis.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass

    async def invalidate_all(self) -> None:
        """Invalidate entire RBAC cache"""
        if not self.enabled or not self.redis:
            return

        try:
            pattern = "rbac:*"
            cursor = 0
            while True:
                cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
                if keys:
                    await self.redis.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass


# Singleton instance
permission_cache = PermissionCache()
