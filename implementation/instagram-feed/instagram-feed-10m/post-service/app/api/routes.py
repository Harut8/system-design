from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel, Field
from typing import Optional

from app.database import get_db, redis_client
from app.services.post import PostService

router = APIRouter()


class PostCreate(BaseModel):
    user_id: int
    caption: Optional[str] = None
    media_url: str
    media_type: str = "image"


class BatchPostRequest(BaseModel):
    post_ids: list[int]
    viewer_id: Optional[int] = None


def get_post_service(db: AsyncSession = Depends(get_db)) -> PostService:
    return PostService(db, redis_client)


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        await redis_client.ping()
        return {"status": "healthy", "service": "post-service", "tier": "10M"}
    except Exception:
        return {"status": "unhealthy", "service": "post-service"}


@router.post("/v1/posts")
async def create_post(request: PostCreate, service: PostService = Depends(get_post_service)):
    post = await service.create_post(request.user_id, request.caption, request.media_url, request.media_type)
    return post


@router.get("/v1/posts/{post_id}")
async def get_post(post_id: int, viewer_user_id: Optional[int] = Query(default=None), service: PostService = Depends(get_post_service)):
    post = await service.get_post(post_id, viewer_user_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.delete("/v1/posts/{post_id}")
async def delete_post(post_id: int, user_id: int = Query(...), service: PostService = Depends(get_post_service)):
    if not await service.delete_post(post_id, user_id):
        raise HTTPException(status_code=404, detail="Post not found or not authorized")
    return {"status": "deleted"}


# Internal endpoint for Feed Service
@router.post("/internal/posts/batch")
async def batch_get_posts(request: BatchPostRequest, service: PostService = Depends(get_post_service)):
    posts = await service.batch_get_posts(request.post_ids, request.viewer_id)
    return {"posts": posts}
