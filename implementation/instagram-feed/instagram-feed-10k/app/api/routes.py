from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional

from app.database import get_db
from app.api.deps import (
    get_feed_service,
    get_post_service,
    get_social_service,
    get_interaction_service,
    get_user_service,
)
from app.services.feed import FeedService, PostService
from app.services.social import SocialService, InteractionService
from app.services.user import UserService
from app.schemas import (
    PostCreate,
    PostResponse,
    PostListResponse,
    FollowRequest,
    FollowResponse,
    LikeRequest,
    LikeResponse,
    CommentCreate,
    CommentResponse,
    CommentListResponse,
    FeedResponse,
    UserCreate,
    UserResponse,
    HealthResponse,
)

router = APIRouter()


# ----- Health Check -----
@router.get("/health", response_model=HealthResponse)
async def health_check(db: AsyncSession = Depends(get_db)):
    """Health check endpoint."""
    try:
        await db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception:
        db_status = "unhealthy"

    return HealthResponse(
        status="ok" if db_status == "healthy" else "degraded",
        database=db_status,
        tier="10K",
    )


# ----- Feed Endpoints -----
@router.get("/v1/feed", response_model=FeedResponse)
async def get_feed(
    user_id: int = Query(..., description="User ID requesting the feed"),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: Optional[str] = Query(default=None),
    feed_service: FeedService = Depends(get_feed_service),
):
    """
    Get personalized feed for a user.
    Uses pull-based feed generation (fan-out-on-read).
    """
    posts, next_cursor, has_more = await feed_service.get_feed(
        user_id=user_id,
        limit=limit,
        cursor=cursor,
    )

    return FeedResponse(
        posts=[PostResponse(**p) for p in posts],
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ----- Post Endpoints -----
@router.post("/v1/posts", response_model=PostResponse, status_code=201)
async def create_post(
    request: PostCreate,
    post_service: PostService = Depends(get_post_service),
):
    """Create a new post."""
    post = await post_service.create_post(
        user_id=request.user_id,
        caption=request.caption,
        media_url=request.media_url,
        media_type=request.media_type,
    )
    return PostResponse(**post)


@router.get("/v1/posts/{post_id}", response_model=PostResponse)
async def get_post(
    post_id: int,
    viewer_user_id: Optional[int] = Query(default=None),
    post_service: PostService = Depends(get_post_service),
):
    """Get a single post by ID."""
    post = await post_service.get_post(post_id, viewer_user_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return PostResponse(**post)


@router.delete("/v1/posts/{post_id}", status_code=204)
async def delete_post(
    post_id: int,
    user_id: int = Query(..., description="User ID (must be post owner)"),
    post_service: PostService = Depends(get_post_service),
):
    """Delete a post (soft delete, owner only)."""
    success = await post_service.delete_post(post_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Post not found or not authorized")


# ----- Like Endpoints -----
@router.post("/v1/posts/{post_id}/like", response_model=LikeResponse)
async def like_post(
    post_id: int,
    request: LikeRequest,
    interaction_service: InteractionService = Depends(get_interaction_service),
):
    """Like a post."""
    status, count = await interaction_service.like_post(request.user_id, post_id)
    return LikeResponse(status=status, like_count=count)


@router.delete("/v1/posts/{post_id}/like", response_model=LikeResponse)
async def unlike_post(
    post_id: int,
    user_id: int = Query(..., description="User ID"),
    interaction_service: InteractionService = Depends(get_interaction_service),
):
    """Unlike a post."""
    status, count = await interaction_service.unlike_post(user_id, post_id)
    return LikeResponse(status=status, like_count=count)


# ----- Comment Endpoints -----
@router.post("/v1/posts/{post_id}/comments", response_model=CommentResponse, status_code=201)
async def add_comment(
    post_id: int,
    request: CommentCreate,
    interaction_service: InteractionService = Depends(get_interaction_service),
):
    """Add a comment to a post."""
    comment = await interaction_service.add_comment(
        user_id=request.user_id,
        post_id=post_id,
        content=request.content,
    )
    return CommentResponse(**comment)


@router.get("/v1/posts/{post_id}/comments", response_model=CommentListResponse)
async def get_comments(
    post_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    interaction_service: InteractionService = Depends(get_interaction_service),
):
    """Get comments for a post."""
    comments = await interaction_service.get_comments(post_id, limit, offset)
    return CommentListResponse(
        comments=[CommentResponse(**c) for c in comments],
        has_more=len(comments) == limit,
    )


@router.delete("/v1/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id: int,
    user_id: int = Query(..., description="User ID (must be comment owner)"),
    interaction_service: InteractionService = Depends(get_interaction_service),
):
    """Delete a comment (soft delete, owner only)."""
    success = await interaction_service.delete_comment(comment_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Comment not found or not authorized")


# ----- Follow Endpoints -----
@router.post("/v1/follow", response_model=FollowResponse)
async def follow_user(
    request: FollowRequest,
    social_service: SocialService = Depends(get_social_service),
):
    """Follow a user."""
    status, count = await social_service.follow(request.follower_id, request.followee_id)
    return FollowResponse(status=status, follower_count=count)


@router.delete("/v1/follow", response_model=FollowResponse)
async def unfollow_user(
    follower_id: int = Query(..., description="User who is unfollowing"),
    followee_id: int = Query(..., description="User being unfollowed"),
    social_service: SocialService = Depends(get_social_service),
):
    """Unfollow a user."""
    status, count = await social_service.unfollow(follower_id, followee_id)
    return FollowResponse(status=status, follower_count=count)


# ----- User Endpoints -----
@router.post("/v1/users", response_model=UserResponse, status_code=201)
async def create_user(
    request: UserCreate,
    user_service: UserService = Depends(get_user_service),
):
    """Create a new user."""
    user = await user_service.create_user(
        username=request.username,
        email=request.email,
        display_name=request.display_name,
        avatar_url=request.avatar_url,
        bio=request.bio,
    )
    if not user:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    return UserResponse(**user)


@router.get("/v1/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: int,
    user_service: UserService = Depends(get_user_service),
):
    """Get user profile by ID."""
    user = await user_service.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(**user)


@router.get("/v1/users/{user_id}/posts", response_model=PostListResponse)
async def get_user_posts(
    user_id: int,
    viewer_user_id: Optional[int] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    cursor: Optional[str] = Query(default=None),
    post_service: PostService = Depends(get_post_service),
):
    """Get posts by a specific user."""
    posts, next_cursor, has_more = await post_service.get_user_posts(
        user_id=user_id,
        viewer_user_id=viewer_user_id,
        limit=limit,
        cursor=cursor,
    )

    return PostListResponse(
        posts=[PostResponse(**p) for p in posts],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/v1/users/{user_id}/followers")
async def get_followers(
    user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    social_service: SocialService = Depends(get_social_service),
):
    """Get followers of a user."""
    followers = await social_service.get_followers(user_id, limit, offset)
    return {"followers": followers, "has_more": len(followers) == limit}


@router.get("/v1/users/{user_id}/following")
async def get_following(
    user_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    social_service: SocialService = Depends(get_social_service),
):
    """Get users that this user follows."""
    following = await social_service.get_following(user_id, limit, offset)
    return {"following": following, "has_more": len(following) == limit}


@router.get("/v1/users/search")
async def search_users(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(default=20, ge=1, le=50),
    user_service: UserService = Depends(get_user_service),
):
    """Search users by username or display name."""
    users = await user_service.search_users(q, limit)
    return {"users": users}
