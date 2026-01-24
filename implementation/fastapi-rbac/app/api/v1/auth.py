from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.auth.jwt import create_access_token
from app.auth.password import verify_password
from app.rbac.models import User
from app.rbac.schemas import TokenResponse
from app.rbac.service import RBACService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/token", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db)
):
    """Authenticate user and return access token"""
    # Find user by email
    query = (
        select(User)
        .where(User.email == form_data.username)
        .options(selectinload(User.roles))
    )
    result = await db.execute(query)
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled"
        )

    # Get permissions
    rbac = RBACService(db)
    permissions = await rbac.get_user_permissions(user.id, user.tenant_id)
    roles = [role.code for role in user.roles]

    # Create token
    access_token = create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        roles=roles,
        permissions=list(permissions)
    )

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=30 * 60  # 30 minutes in seconds
    )
