from typing import List, Optional, Set
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.rbac.models import Permission, Role, User, PermissionAuditLog, user_roles
from app.rbac.schemas import AuthContext
from app.rbac.cache import permission_cache


class RBACService:
    """Core RBAC service for permission management and authorization"""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ============ Permission Resolution ============

    async def get_user_permissions(
        self,
        user_id: UUID,
        tenant_id: UUID,
        use_cache: bool = True
    ) -> Set[str]:
        """
        Get all permissions for a user, including inherited permissions.
        Uses cache for performance when available.
        """
        # Try cache first
        if use_cache:
            cached = await permission_cache.get_user_permissions(user_id, tenant_id)
            if cached is not None:
                return cached

        # Fetch from database
        permissions = await self._resolve_user_permissions(user_id, tenant_id)

        # Cache the result
        if use_cache:
            await permission_cache.set_user_permissions(user_id, tenant_id, permissions)

        return permissions

    async def _resolve_user_permissions(
        self,
        user_id: UUID,
        tenant_id: UUID
    ) -> Set[str]:
        """Resolve all permissions for a user from database"""
        # Query user with roles and permissions
        query = (
            select(User)
            .where(User.id == user_id, User.tenant_id == tenant_id)
            .options(
                selectinload(User.roles).selectinload(Role.permissions),
                selectinload(User.roles).selectinload(Role.parent_roles).selectinload(Role.permissions)
            )
        )
        result = await self.db.execute(query)
        user = result.scalar_one_or_none()

        if not user:
            return set()

        permissions: Set[str] = set()

        # Collect permissions from all roles (including inherited)
        for role in user.roles:
            permissions.update(self._get_role_permissions(role, visited=set()))

        return permissions

    def _get_role_permissions(self, role: Role, visited: Set[UUID]) -> Set[str]:
        """Recursively get permissions including inherited ones"""
        if role.id in visited:
            return set()  # Prevent circular inheritance

        visited.add(role.id)
        permissions = {p.code for p in role.permissions}

        # Add inherited permissions from parent roles
        for parent_role in role.parent_roles:
            permissions.update(self._get_role_permissions(parent_role, visited))

        return permissions

    async def get_user_roles(self, user_id: UUID, tenant_id: UUID) -> Set[str]:
        """Get all role codes for a user"""
        query = (
            select(User)
            .where(User.id == user_id, User.tenant_id == tenant_id)
            .options(selectinload(User.roles))
        )
        result = await self.db.execute(query)
        user = result.scalar_one_or_none()

        if not user:
            return set()

        return {role.code for role in user.roles}

    # ============ Authorization Checks ============

    async def check_permission(
        self,
        user_id: UUID,
        tenant_id: UUID,
        permission: str,
        is_superuser: bool = False
    ) -> bool:
        """Check if user has a specific permission"""
        if is_superuser:
            return True

        permissions = await self.get_user_permissions(user_id, tenant_id)
        return permission in permissions

    async def check_any_permission(
        self,
        user_id: UUID,
        tenant_id: UUID,
        permissions: List[str],
        is_superuser: bool = False
    ) -> bool:
        """Check if user has any of the specified permissions"""
        if is_superuser:
            return True

        user_permissions = await self.get_user_permissions(user_id, tenant_id)
        return bool(user_permissions & set(permissions))

    async def check_all_permissions(
        self,
        user_id: UUID,
        tenant_id: UUID,
        permissions: List[str],
        is_superuser: bool = False
    ) -> bool:
        """Check if user has all specified permissions"""
        if is_superuser:
            return True

        user_permissions = await self.get_user_permissions(user_id, tenant_id)
        return set(permissions).issubset(user_permissions)

    async def build_auth_context(
        self,
        user_id: UUID,
        tenant_id: UUID,
        email: str,
        is_superuser: bool = False
    ) -> AuthContext:
        """Build complete authorization context for a user"""
        permissions = await self.get_user_permissions(user_id, tenant_id)
        roles = await self.get_user_roles(user_id, tenant_id)

        return AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            is_superuser=is_superuser,
            roles=roles,
            permissions=permissions
        )

    # ============ Role Management ============

    async def assign_role_to_user(
        self,
        user_id: UUID,
        role_id: UUID,
        tenant_id: UUID,
        assigned_by: Optional[UUID] = None
    ) -> bool:
        """Assign a role to a user"""
        # Insert role assignment
        stmt = user_roles.insert().values(
            user_id=user_id,
            role_id=role_id,
            tenant_id=tenant_id,
            assigned_by=assigned_by
        )

        try:
            await self.db.execute(stmt)
            await self.db.commit()

            # Invalidate cache
            await permission_cache.invalidate_user(user_id, tenant_id)

            # Audit log
            await self._log_audit(
                tenant_id=tenant_id,
                user_id=assigned_by,
                action="role_assigned",
                target_type="user",
                target_id=user_id,
                new_value={"role_id": str(role_id)}
            )

            return True
        except Exception:
            await self.db.rollback()
            return False

    async def revoke_role_from_user(
        self,
        user_id: UUID,
        role_id: UUID,
        tenant_id: UUID,
        revoked_by: Optional[UUID] = None
    ) -> bool:
        """Revoke a role from a user"""
        stmt = user_roles.delete().where(
            user_roles.c.user_id == user_id,
            user_roles.c.role_id == role_id,
            user_roles.c.tenant_id == tenant_id
        )

        try:
            await self.db.execute(stmt)
            await self.db.commit()

            # Invalidate cache
            await permission_cache.invalidate_user(user_id, tenant_id)

            # Audit log
            await self._log_audit(
                tenant_id=tenant_id,
                user_id=revoked_by,
                action="role_revoked",
                target_type="user",
                target_id=user_id,
                old_value={"role_id": str(role_id)}
            )

            return True
        except Exception:
            await self.db.rollback()
            return False

    # ============ Audit Logging ============

    async def _log_audit(
        self,
        tenant_id: Optional[UUID],
        user_id: Optional[UUID],
        action: str,
        target_type: str,
        target_id: UUID,
        old_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None
    ) -> None:
        """Create audit log entry"""
        log = PermissionAuditLog(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            old_value=old_value,
            new_value=new_value,
            ip_address=ip_address,
            user_agent=user_agent
        )
        self.db.add(log)
        await self.db.commit()
