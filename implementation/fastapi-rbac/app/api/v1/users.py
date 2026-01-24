from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.auth.password import hash_password
from app.rbac.models import User
from app.rbac.schemas import AuthContext, UserResponse, UserCreate
from app.rbac.service import RBACService
from app.rbac.dependencies import (
    get_current_user,
    require_permission,
    require_superuser
)
from app.rbac.decorators import check_permission
from app.rbac.exceptions import PermissionDenied

router = APIRouter(prefix="/users", tags=["Users"])


@router.get("/", response_model=List[UserResponse])
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    auth: AuthContext = Depends(require_permission("users:read")),
    db: AsyncSession = Depends(get_db)
):
    """
    List all users in tenant.
    Requires: users:read permission
    """
    query = (
        select(User)
        .where(User.tenant_id == auth.tenant_id)
        .options(selectinload(User.roles))
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get current user profile.
    Requires: Authentication only
    """
    query = (
        select(User)
        .where(User.id == auth.user_id)
        .options(selectinload(User.roles))
    )
    result = await db.execute(query)
    return result.scalar_one()


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get user by ID.
    Authorization: Can view own profile, or needs users:read for others
    """
    # Self-access always allowed
    if auth.user_id != user_id and not auth.has_permission("users:read"):
        raise PermissionDenied("Cannot view other users without users:read permission")

    query = (
        select(User)
        .where(User.id == user_id, User.tenant_id == auth.tenant_id)
        .options(selectinload(User.roles))
    )
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if not user:
        raise PermissionDenied("User not found")

    return user


@router.post("/", response_model=UserResponse, status_code=201)
async def create_user(
    user_data: UserCreate,
    auth: AuthContext = Depends(require_permission("users:write")),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new user.
    Requires: users:write permission
    """
    user = User(
        tenant_id=auth.tenant_id,
        email=user_data.email,
        hashed_password=hash_password(user_data.password),
        is_active=True
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    auth: AuthContext = Depends(require_permission("users:delete")),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a user.
    Requires: users:delete permission
    """
    query = select(User).where(User.id == user_id, User.tenant_id == auth.tenant_id)
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if not user:
        raise PermissionDenied("User not found")

    await db.delete(user)
    await db.commit()


@router.post("/{user_id}/roles/{role_id}", status_code=200)
async def assign_role(
    user_id: UUID,
    role_id: UUID,
    auth: AuthContext = Depends(require_permission("roles:write")),
    db: AsyncSession = Depends(get_db)
):
    """
    Assign role to user.
    Requires: roles:write permission
    """
    rbac = RBACService(db)
    success = await rbac.assign_role_to_user(
        user_id=user_id,
        role_id=role_id,
        tenant_id=auth.tenant_id,
        assigned_by=auth.user_id
    )

    if not success:
        raise PermissionDenied("Failed to assign role")

    return {"message": "Role assigned successfully"}


@router.delete("/{user_id}/roles/{role_id}", status_code=200)
async def revoke_role(
    user_id: UUID,
    role_id: UUID,
    auth: AuthContext = Depends(require_permission("roles:write")),
    db: AsyncSession = Depends(get_db)
):
    """
    Revoke role from user.
    Requires: roles:write permission
    """
    rbac = RBACService(db)
    success = await rbac.revoke_role_from_user(
        user_id=user_id,
        role_id=role_id,
        tenant_id=auth.tenant_id,
        revoked_by=auth.user_id
    )

    if not success:
        raise PermissionDenied("Failed to revoke role")

    return {"message": "Role revoked successfully"}


@router.get("/{user_id}/audit-log")
@check_permission("admin:access")
async def get_user_audit_log(
    user_id: UUID,
    auth: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get audit log for user.
    Requires: admin:access permission
    """
    from app.rbac.models import PermissionAuditLog

    query = (
        select(PermissionAuditLog)
        .where(
            PermissionAuditLog.target_type == "user",
            PermissionAuditLog.target_id == user_id
        )
        .order_by(PermissionAuditLog.created_at.desc())
        .limit(100)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/system/sync")
async def sync_all_users(
    auth: AuthContext = Depends(require_superuser),
    db: AsyncSession = Depends(get_db)
):
    """
    System synchronization endpoint.
    Requires: Superuser access
    """
    # Invalidate all permission caches
    from app.rbac.cache import permission_cache
    await permission_cache.invalidate_all()

    return {"message": "User cache synchronized"}
