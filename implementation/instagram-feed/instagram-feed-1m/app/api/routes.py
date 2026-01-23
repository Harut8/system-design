from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
import redis.asyncio as redis
from typing import Optional

from app.database import get_db, get_redis
from app.api.deps import get_feed_service, get_post_service, get_social_service, get_interaction_service, get_user_service
from app.services.feed import FeedService, PostService
from app.services.social import SocialService, InteractionService
from app.services.user import UserService
from app.schemas import *

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db), redis_client: redis.Redis = Depends(get_redis)):
    try:
        await db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception:
        db_status = "unhealthy"

    try:
        await redis_client.ping()
        redis_status = "healthy"
    except Exception:
        redis_status = "unhealthy"

    # Celery status would require additional check
    celery_status = "unknown"

    overall = "ok" if db_status == "healthy" and redis_status == "healthy" else "degraded"
    return HealthResponse(status=overall, database=db_status, redis=redis_status, celery=celery_status, tier="1M")


@router.get("/v1/feed", response_model=FeedResponse)
async def get_feed(
    user_id: int = Query(...),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: Optional[str] = Query(default=None),
    feed_service: FeedService = Depends(get_feed_service),
):
    posts, next_cursor, has_more, cached, cache_age, celebrity_count = await feed_service.get_feed(user_id, limit, cursor)
    return FeedResponse(
        posts=[PostResponse(**p) for p in posts],
        next_cursor=next_cursor,
        has_more=has_more,
        cached=cached,
        cache_age_seconds=cache_age,
        celebrity_posts_included=celebrity_count,
    )


@router.post("/v1/posts", response_model=PostResponse, status_code=201)
async def create_post(request: PostCreate, post_service: PostService = Depends(get_post_service)):
    post = await post_service.create_post(request.user_id, request.caption, request.media_url, request.media_type)
    return PostResponse(**post)


@router.get("/v1/posts/{post_id}", response_model=PostResponse)
async def get_post(post_id: int, viewer_user_id: Optional[int] = Query(default=None), post_service: PostService = Depends(get_post_service)):
    post = await post_service.get_post(post_id, viewer_user_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return PostResponse(**post)


@router.delete("/v1/posts/{post_id}", status_code=204)
async def delete_post(post_id: int, user_id: int = Query(...), post_service: PostService = Depends(get_post_service)):
    if not await post_service.delete_post(post_id, user_id):
        raise HTTPException(status_code=404, detail="Post not found or not authorized")


@router.post("/v1/posts/{post_id}/like", response_model=LikeResponse)
async def like_post(post_id: int, request: LikeRequest, interaction_service: InteractionService = Depends(get_interaction_service)):
    status, count = await interaction_service.like_post(request.user_id, post_id)
    return LikeResponse(status=status, like_count=count)


@router.delete("/v1/posts/{post_id}/like", response_model=LikeResponse)
async def unlike_post(post_id: int, user_id: int = Query(...), interaction_service: InteractionService = Depends(get_interaction_service)):
    status, count = await interaction_service.unlike_post(user_id, post_id)
    return LikeResponse(status=status, like_count=count)


@router.post("/v1/posts/{post_id}/comments", response_model=CommentResponse, status_code=201)
async def add_comment(post_id: int, request: CommentCreate, interaction_service: InteractionService = Depends(get_interaction_service)):
    comment = await interaction_service.add_comment(request.user_id, post_id, request.content)
    return CommentResponse(**comment)


@router.get("/v1/posts/{post_id}/comments", response_model=CommentListResponse)
async def get_comments(post_id: int, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), interaction_service: InteractionService = Depends(get_interaction_service)):
    comments = await interaction_service.get_comments(post_id, limit, offset)
    return CommentListResponse(comments=[CommentResponse(**c) for c in comments], has_more=len(comments) == limit)


@router.delete("/v1/comments/{comment_id}", status_code=204)
async def delete_comment(comment_id: int, user_id: int = Query(...), interaction_service: InteractionService = Depends(get_interaction_service)):
    if not await interaction_service.delete_comment(comment_id, user_id):
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")


@router.post("/v1/follow", response_model=FollowResponse)
async def follow_user(request: FollowRequest, social_service: SocialService = Depends(get_social_service)):
    status, count = await social_service.follow(request.follower_id, request.followee_id)
    return FollowResponse(status=status, follower_count=count)


@router.delete("/v1/follow", response_model=FollowResponse)
async def unfollow_user(follower_id: int = Query(...), followee_id: int = Query(...), social_service: SocialService = Depends(get_social_service)):
    status, count = await social_service.unfollow(follower_id, followee_id)
    return FollowResponse(status=status, follower_count=count)


@router.post("/v1/users", response_model=UserResponse, status_code=201)
async def create_user(request: UserCreate, user_service: UserService = Depends(get_user_service)):
    user = await user_service.create_user(request.username, request.email, request.display_name, request.avatar_url, request.bio)
    if not user:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    return UserResponse(**user)


@router.get("/v1/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: int, user_service: UserService = Depends(get_user_service)):
    user = await user_service.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(**user)


@router.get("/v1/users/{user_id}/posts", response_model=PostListResponse)
async def get_user_posts(user_id: int, viewer_user_id: Optional[int] = Query(default=None), limit: int = Query(default=20, ge=1, le=50), cursor: Optional[str] = Query(default=None), post_service: PostService = Depends(get_post_service)):
    posts, next_cursor, has_more = await post_service.get_user_posts(user_id, viewer_user_id, limit, cursor)
    return PostListResponse(posts=[PostResponse(**p) for p in posts], next_cursor=next_cursor, has_more=has_more)


@router.get("/v1/users/{user_id}/followers")
async def get_followers(user_id: int, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), social_service: SocialService = Depends(get_social_service)):
    followers = await social_service.get_followers(user_id, limit, offset)
    return {"followers": followers, "has_more": len(followers) == limit}


@router.get("/v1/users/{user_id}/following")
async def get_following(user_id: int, limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), social_service: SocialService = Depends(get_social_service)):
    following = await social_service.get_following(user_id, limit, offset)
    return {"following": following, "has_more": len(following) == limit}


@router.get("/v1/users/search")
async def search_users(q: str = Query(..., min_length=1), limit: int = Query(default=20, ge=1, le=50), user_service: UserService = Depends(get_user_service)):
    users = await user_service.search_users(q, limit)
    return {"users": users}
