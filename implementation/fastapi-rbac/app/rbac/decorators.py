from functools import wraps
from typing import Callable, List, Optional

from app.rbac.schemas import AuthContext
from app.rbac.exceptions import PermissionDenied


def check_permission(permission: str):
    """
    Decorator to check for a single permission.
    Must be used with FastAPI dependency injection.

    Usage:
        @router.get("/users")
        @check_permission("users:read")
        async def list_users(auth: AuthContext = Depends(get_current_user)):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Get auth from kwargs (injected by FastAPI)
            auth: Optional[AuthContext] = kwargs.get('auth')
            if auth is None:
                raise PermissionDenied("Authentication required")

            if not auth.has_permission(permission):
                raise PermissionDenied(f"Permission '{permission}' required")

            return await func(*args, **kwargs)
        return wrapper
    return decorator


def check_any_permission(permissions: List[str]):
    """Decorator to check for any of the specified permissions"""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth: Optional[AuthContext] = kwargs.get('auth')
            if auth is None:
                raise PermissionDenied("Authentication required")

            if not auth.has_any_permission(permissions):
                raise PermissionDenied(f"One of {permissions} required")

            return await func(*args, **kwargs)
        return wrapper
    return decorator


def check_role(role: str):
    """Decorator to check for a specific role"""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth: Optional[AuthContext] = kwargs.get('auth')
            if auth is None:
                raise PermissionDenied("Authentication required")

            if not auth.has_role(role):
                raise PermissionDenied(f"Role '{role}' required")

            return await func(*args, **kwargs)
        return wrapper
    return decorator


def superuser_required(func: Callable):
    """Decorator to require superuser access"""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        auth: Optional[AuthContext] = kwargs.get('auth')
        if auth is None:
            raise PermissionDenied("Authentication required")

        if not auth.is_superuser:
            raise PermissionDenied("Superuser access required")

        return await func(*args, **kwargs)
    return wrapper
