import pytest
from uuid import uuid4

from app.rbac.schemas import AuthContext


class TestAuthContext:
    """Unit tests for AuthContext permission checking"""

    def test_superuser_has_all_permissions(self):
        """Superuser should have access to everything"""
        auth = AuthContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            email="admin@test.com",
            is_superuser=True,
            permissions=set()
        )

        assert auth.has_permission("any:permission") is True
        assert auth.has_permission("users:delete") is True
        assert auth.has_all_permissions(["a", "b", "c"]) is True
        assert auth.has_any_permission(["x", "y"]) is True

    def test_user_permission_check(self):
        """Regular user should only have assigned permissions"""
        auth = AuthContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            email="user@test.com",
            is_superuser=False,
            permissions={"users:read", "posts:read", "posts:write"}
        )

        # Has permission
        assert auth.has_permission("users:read") is True
        assert auth.has_permission("posts:read") is True

        # Doesn't have permission
        assert auth.has_permission("users:write") is False
        assert auth.has_permission("admin:access") is False

    def test_has_any_permission(self):
        """Should return True if user has any of the specified permissions"""
        auth = AuthContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            email="user@test.com",
            permissions={"users:read", "posts:read"}
        )

        # Has one of them
        assert auth.has_any_permission(["users:read", "admin:access"]) is True
        assert auth.has_any_permission(["users:write", "posts:read"]) is True

        # Has none of them
        assert auth.has_any_permission(["users:write", "admin:access"]) is False

    def test_has_all_permissions(self):
        """Should return True only if user has all specified permissions"""
        auth = AuthContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            email="user@test.com",
            permissions={"users:read", "posts:read", "posts:write"}
        )

        # Has all
        assert auth.has_all_permissions(["users:read", "posts:read"]) is True
        assert auth.has_all_permissions(["posts:read", "posts:write"]) is True

        # Missing one
        assert auth.has_all_permissions(["users:read", "users:write"]) is False

    def test_role_check(self):
        """Should check if user has specific role"""
        auth = AuthContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            email="user@test.com",
            roles={"user", "manager"}
        )

        assert auth.has_role("user") is True
        assert auth.has_role("manager") is True
        assert auth.has_role("admin") is False

    def test_empty_permissions(self):
        """User with no permissions should fail all checks"""
        auth = AuthContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            email="user@test.com",
            permissions=set()
        )

        assert auth.has_permission("any:permission") is False
        assert auth.has_any_permission(["a", "b"]) is False
        assert auth.has_all_permissions([]) is True  # Empty set is subset of any set


class TestPermissionPatterns:
    """Test permission code patterns"""

    def test_permission_code_format(self):
        """Permission codes should follow resource:action pattern"""
        valid_codes = [
            "users:read",
            "users:write",
            "posts:delete",
            "admin:access",
            "search:execute",
            "search_ai:execute"
        ]

        for code in valid_codes:
            parts = code.split(":")
            assert len(parts) == 2
            assert len(parts[0]) > 0
            assert len(parts[1]) > 0
