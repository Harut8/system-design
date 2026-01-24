from fastapi import APIRouter

from app.api.v1 import users, roles, permissions, auth

router = APIRouter(prefix="/api/v1")

router.include_router(auth.router)
router.include_router(users.router)
router.include_router(roles.router)
router.include_router(permissions.router)
