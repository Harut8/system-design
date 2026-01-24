from typing import List
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.rbac.schemas import AuthContext, TokenPayload
from app.rbac.service import RBACService
from app.rbac.exceptions import PermissionDenied

settings = get_settings()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


async def get_token_payload(
    token: str = Depends(oauth2_scheme)
) -> TokenPayload:
    """Extract and validate JWT token payload"""
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        return TokenPayload(**payload)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    request: Request,
    token: TokenPayload = Depends(get_token_payload),
    db: AsyncSession = Depends(get_db)
) -> AuthContext:
    """Get current authenticated user with full authorization context"""
    rbac = RBACService(db)

    auth_context = await rbac.build_auth_context(
        user_id=UUID(token.sub),
        tenant_id=UUID(token.tenant_id),
        email=token.email,
        is_superuser="super_admin" in token.roles
    )

    # Store in request state for access in middleware/handlers
    request.state.auth = auth_context

    return auth_context


class RequirePermission:
    """Dependency factory for checking single permission"""

    def __init__(self, permission: str):
        self.permission = permission

    async def __call__(
        self,
        auth: AuthContext = Depends(get_current_user)
    ) -> AuthContext:
        if not auth.has_permission(self.permission):
            raise PermissionDenied(
                detail=f"Permission '{self.permission}' required"
            )
        return auth


class RequireAnyPermission:
    """Dependency factory for checking any of multiple permissions"""

    def __init__(self, permissions: List[str]):
        self.permissions = permissions

    async def __call__(
        self,
        auth: AuthContext = Depends(get_current_user)
    ) -> AuthContext:
        if not auth.has_any_permission(self.permissions):
            raise PermissionDenied(
                detail=f"One of permissions {self.permissions} required"
            )
        return auth


class RequireAllPermissions:
    """Dependency factory for checking all permissions"""

    def __init__(self, permissions: List[str]):
        self.permissions = permissions

    async def __call__(
        self,
        auth: AuthContext = Depends(get_current_user)
    ) -> AuthContext:
        if not auth.has_all_permissions(self.permissions):
            raise PermissionDenied(
                detail=f"All permissions {self.permissions} required"
            )
        return auth


class RequireRole:
    """Dependency factory for checking role"""

    def __init__(self, role: str):
        self.role = role

    async def __call__(
        self,
        auth: AuthContext = Depends(get_current_user)
    ) -> AuthContext:
        if not auth.has_role(self.role):
            raise PermissionDenied(
                detail=f"Role '{self.role}' required"
            )
        return auth


class RequireSuperuser:
    """Dependency for superuser-only endpoints"""

    async def __call__(
        self,
        auth: AuthContext = Depends(get_current_user)
    ) -> AuthContext:
        if not auth.is_superuser:
            raise PermissionDenied(
                detail="Superuser access required"
            )
        return auth


# Convenient dependency instances
require_superuser = RequireSuperuser()


def require_permission(permission: str):
    """Factory function for permission dependency"""
    return RequirePermission(permission)


def require_any_permission(permissions: List[str]):
    """Factory function for any-permission dependency"""
    return RequireAnyPermission(permissions)


def require_all_permissions(permissions: List[str]):
    """Factory function for all-permissions dependency"""
    return RequireAllPermissions(permissions)


def require_role(role: str):
    """Factory function for role dependency"""
    return RequireRole(role)
