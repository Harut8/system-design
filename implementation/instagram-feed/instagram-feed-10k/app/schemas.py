from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# ----- User Schemas -----
class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    email: str = Field(..., max_length=255)
    display_name: Optional[str] = Field(None, max_length=100)
    avatar_url: Optional[str] = None
    bio: Optional[str] = None


class UserCreate(UserBase):
    pass


class UserResponse(BaseModel):
    id: int
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    follower_count: int = 0
    following_count: int = 0
    post_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True


# ----- Post Schemas -----
MediaType = Literal["image", "video"]


class PostCreate(BaseModel):
    user_id: int = Field(..., description="Author user ID")
    caption: Optional[str] = Field(None, max_length=2200)
    media_url: str = Field(..., description="URL to the media")
    media_type: MediaType = Field(default="image")


class PostResponse(BaseModel):
    id: int
    user_id: int
    username: str
    user_avatar_url: Optional[str] = None
    caption: Optional[str] = None
    media_url: str
    media_type: str
    like_count: int = 0
    comment_count: int = 0
    is_liked: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class PostListResponse(BaseModel):
    posts: list[PostResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


# ----- Follow Schemas -----
class FollowRequest(BaseModel):
    follower_id: int = Field(..., description="User who is following")
    followee_id: int = Field(..., description="User being followed")


class FollowResponse(BaseModel):
    status: Literal["followed", "already_following", "unfollowed", "not_following"]
    follower_count: int


# ----- Like Schemas -----
class LikeRequest(BaseModel):
    user_id: int = Field(..., description="User ID")


class LikeResponse(BaseModel):
    status: Literal["liked", "already_liked", "unliked", "not_liked"]
    like_count: int


# ----- Comment Schemas -----
class CommentCreate(BaseModel):
    user_id: int = Field(..., description="User ID")
    content: str = Field(..., min_length=1, max_length=1000)


class CommentResponse(BaseModel):
    id: int
    user_id: int
    username: str
    user_avatar_url: Optional[str] = None
    post_id: int
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class CommentListResponse(BaseModel):
    comments: list[CommentResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


# ----- Feed Schemas -----
class FeedRequest(BaseModel):
    user_id: int = Field(..., description="User requesting the feed")
    limit: int = Field(default=20, ge=1, le=50)
    cursor: Optional[str] = Field(None, description="Pagination cursor")


class FeedResponse(BaseModel):
    posts: list[PostResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


# ----- Health Schemas -----
class HealthResponse(BaseModel):
    status: str
    database: str
    tier: str = "10K"
