from fastapi import HTTPException, status


class PermissionDenied(HTTPException):
    """Exception raised when user lacks required permission"""

    def __init__(self, detail: str = "Permission denied"):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail
        )


class AuthenticationRequired(HTTPException):
    """Exception raised when authentication is required"""

    def __init__(self, detail: str = "Authentication required"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"}
        )


class RoleNotFound(HTTPException):
    """Exception raised when role is not found"""

    def __init__(self, role_id: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role '{role_id}' not found"
        )


class PermissionNotFound(HTTPException):
    """Exception raised when permission is not found"""

    def __init__(self, permission: str):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Permission '{permission}' not found"
        )
