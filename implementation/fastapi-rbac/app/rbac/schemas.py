from datetime import datetime
from typing import List, Optional, Set
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


# ============ Permission Schemas ============

class PermissionBase(BaseModel):
    code: str = Field(..., min_length=3, max_length=100, pattern=r'^[a-z_]+:[a-z_]+$')
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    resource: str = Field(..., min_length=1, max_length=100)
    action: str = Field(..., min_length=1, max_length=50)


class PermissionCreate(PermissionBase):
    pass


class PermissionResponse(PermissionBase):
    id: UUID
    is_system: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ============ Role Schemas ============

class RoleBase(BaseModel):
    code: str = Field(..., min_length=2, max_length=100)
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    priority: int = Field(default=0, ge=0, le=100)


class RoleCreate(RoleBase):
    permission_ids: List[UUID] = []
    parent_role_ids: List[UUID] = []


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    priority: Optional[int] = Field(None, ge=0, le=100)
    permission_ids: Optional[List[UUID]] = None
    parent_role_ids: Optional[List[UUID]] = None


class RoleResponse(RoleBase):
    id: UUID
    tenant_id: Optional[UUID]
    is_system: bool
    permissions: List[PermissionResponse] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============ User Schemas ============

class UserBase(BaseModel):
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)
    role_ids: List[UUID] = []


class UserResponse(UserBase):
    id: UUID
    tenant_id: UUID
    is_active: bool
    is_superuser: bool
    roles: List[RoleResponse] = []
    created_at: datetime

    class Config:
        from_attributes = True


# ============ Authorization Context ============

class AuthContext(BaseModel):
    """User authentication and authorization context"""
    user_id: UUID
    tenant_id: UUID
    email: str
    is_superuser: bool = False
    roles: Set[str] = set()
    permissions: Set[str] = set()

    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission"""
        if self.is_superuser:
            return True
        return permission in self.permissions

    def has_any_permission(self, permissions: List[str]) -> bool:
        """Check if user has any of the specified permissions"""
        if self.is_superuser:
            return True
        return bool(self.permissions & set(permissions))

    def has_all_permissions(self, permissions: List[str]) -> bool:
        """Check if user has all specified permissions"""
        if self.is_superuser:
            return True
        return set(permissions).issubset(self.permissions)

    def has_role(self, role: str) -> bool:
        """Check if user has a specific role"""
        if self.is_superuser:
            return True
        return role in self.roles


# ============ Token Schemas ============

class TokenPayload(BaseModel):
    sub: str  # user_id
    tenant_id: str
    email: str
    exp: datetime
    roles: List[str] = []
    permissions: List[str] = []


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ============ Audit Log Schemas ============

class AuditLogResponse(BaseModel):
    id: UUID
    action: str
    target_type: str
    target_id: UUID
    old_value: Optional[dict]
    new_value: Optional[dict]
    created_at: datetime

    class Config:
        from_attributes = True
