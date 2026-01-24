from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.rbac.models import Permission
from app.rbac.schemas import AuthContext, PermissionResponse, PermissionCreate
from app.rbac.cache import permission_cache
from app.rbac.dependencies import (
    require_permission,
    require_superuser
)

router = APIRouter(prefix="/permissions", tags=["Permissions"])


@router.get("/", response_model=List[PermissionResponse])
async def list_permissions(
    resource: str = Query(None, description="Filter by resource"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    auth: AuthContext = Depends(require_permission("roles:read")),
    db: AsyncSession = Depends(get_db)
):
    """
    List all permissions.
    Requires: roles:read permission
    """
    query = select(Permission)

    if resource:
        query = query.where(Permission.resource == resource)

    query = query.offset(skip).limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{permission_id}", response_model=PermissionResponse)
async def get_permission(
    permission_id: UUID,
    auth: AuthContext = Depends(require_permission("roles:read")),
    db: AsyncSession = Depends(get_db)
):
    """
    Get permission by ID.
    Requires: roles:read permission
    """
    query = select(Permission).where(Permission.id == permission_id)
    result = await db.execute(query)
    permission = result.scalar_one_or_none()

    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found"
        )

    return permission


@router.post("/", response_model=PermissionResponse, status_code=201)
async def create_permission(
    permission_data: PermissionCreate,
    auth: AuthContext = Depends(require_superuser),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new permission.
    Requires: Superuser access
    """
    # Check if permission code already exists
    query = select(Permission).where(Permission.code == permission_data.code)
    result = await db.execute(query)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Permission code already exists"
        )

    permission = Permission(
        code=permission_data.code,
        name=permission_data.name,
        description=permission_data.description,
        resource=permission_data.resource,
        action=permission_data.action,
        is_system=False
    )
    db.add(permission)
    await db.commit()
    await db.refresh(permission)

    return permission


@router.delete("/{permission_id}", status_code=204)
async def delete_permission(
    permission_id: UUID,
    auth: AuthContext = Depends(require_superuser),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a permission.
    Requires: Superuser access
    """
    query = select(Permission).where(Permission.id == permission_id)
    result = await db.execute(query)
    permission = result.scalar_one_or_none()

    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found"
        )

    if permission.is_system:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete system permissions"
        )

    await db.delete(permission)
    await db.commit()

    # Invalidate all caches since permission changed
    await permission_cache.invalidate_all()


@router.get("/by-code/{code}", response_model=PermissionResponse)
async def get_permission_by_code(
    code: str,
    auth: AuthContext = Depends(require_permission("roles:read")),
    db: AsyncSession = Depends(get_db)
):
    """
    Get permission by code.
    Requires: roles:read permission
    """
    query = select(Permission).where(Permission.code == code)
    result = await db.execute(query)
    permission = result.scalar_one_or_none()

    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found"
        )

    return permission
