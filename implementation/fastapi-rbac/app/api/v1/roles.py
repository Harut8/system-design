from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.rbac.models import Role, Permission, role_permissions
from app.rbac.schemas import AuthContext, RoleResponse, RoleCreate, RoleUpdate
from app.rbac.cache import permission_cache
from app.rbac.dependencies import (
    get_current_user,
    require_permission
)

router = APIRouter(prefix="/roles", tags=["Roles"])


@router.get("/", response_model=List[RoleResponse])
async def list_roles(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    auth: AuthContext = Depends(require_permission("roles:read")),
    db: AsyncSession = Depends(get_db)
):
    """
    List all roles.
    Requires: roles:read permission
    """
    query = (
        select(Role)
        .where(
            (Role.tenant_id == auth.tenant_id) | (Role.tenant_id.is_(None))
        )
        .options(selectinload(Role.permissions))
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
    role_id: UUID,
    auth: AuthContext = Depends(require_permission("roles:read")),
    db: AsyncSession = Depends(get_db)
):
    """
    Get role by ID.
    Requires: roles:read permission
    """
    query = (
        select(Role)
        .where(Role.id == role_id)
        .options(selectinload(Role.permissions))
    )
    result = await db.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )

    return role


@router.post("/", response_model=RoleResponse, status_code=201)
async def create_role(
    role_data: RoleCreate,
    auth: AuthContext = Depends(require_permission("roles:write")),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new role.
    Requires: roles:write permission
    """
    role = Role(
        tenant_id=auth.tenant_id,
        code=role_data.code,
        name=role_data.name,
        description=role_data.description,
        priority=role_data.priority
    )
    db.add(role)
    await db.flush()

    # Assign permissions
    if role_data.permission_ids:
        for perm_id in role_data.permission_ids:
            stmt = role_permissions.insert().values(
                role_id=role.id,
                permission_id=perm_id,
                granted_by=auth.user_id
            )
            await db.execute(stmt)

    await db.commit()
    await db.refresh(role)

    # Reload with permissions
    query = (
        select(Role)
        .where(Role.id == role.id)
        .options(selectinload(Role.permissions))
    )
    result = await db.execute(query)
    return result.scalar_one()


@router.patch("/{role_id}", response_model=RoleResponse)
async def update_role(
    role_id: UUID,
    role_data: RoleUpdate,
    auth: AuthContext = Depends(require_permission("roles:write")),
    db: AsyncSession = Depends(get_db)
):
    """
    Update a role.
    Requires: roles:write permission
    """
    query = select(Role).where(Role.id == role_id)
    result = await db.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify system roles"
        )

    # Update fields
    if role_data.name is not None:
        role.name = role_data.name
    if role_data.description is not None:
        role.description = role_data.description
    if role_data.priority is not None:
        role.priority = role_data.priority

    # Update permissions
    if role_data.permission_ids is not None:
        # Remove existing
        await db.execute(
            role_permissions.delete().where(role_permissions.c.role_id == role_id)
        )
        # Add new
        for perm_id in role_data.permission_ids:
            stmt = role_permissions.insert().values(
                role_id=role.id,
                permission_id=perm_id,
                granted_by=auth.user_id
            )
            await db.execute(stmt)

    await db.commit()

    # Invalidate cache for all users with this role
    await permission_cache.invalidate_role(role_id)

    # Reload with permissions
    query = (
        select(Role)
        .where(Role.id == role.id)
        .options(selectinload(Role.permissions))
    )
    result = await db.execute(query)
    return result.scalar_one()


@router.delete("/{role_id}", status_code=204)
async def delete_role(
    role_id: UUID,
    auth: AuthContext = Depends(require_permission("roles:write")),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a role.
    Requires: roles:write permission
    """
    query = select(Role).where(Role.id == role_id)
    result = await db.execute(query)
    role = result.scalar_one_or_none()

    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )

    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete system roles"
        )

    await db.delete(role)
    await db.commit()

    # Invalidate cache
    await permission_cache.invalidate_role(role_id)
