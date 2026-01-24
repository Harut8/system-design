from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


# Item type mapping
ItemType = Literal["post", "reel", "comment"]


def item_type_to_int(item_type: ItemType) -> int:
    """Convert item type string to integer."""
    mapping = {"post": 1, "reel": 2, "comment": 3}
    return mapping.get(item_type, 1)


def int_to_item_type(item_type_int: int) -> ItemType:
    """Convert integer to item type string."""
    mapping = {1: "post", 2: "reel", 3: "comment"}
    return mapping.get(item_type_int, "post")


# Request schemas
class LikeRequest(BaseModel):
    user_id: int = Field(..., description="User ID")
    item_id: int = Field(..., description="Item ID")
    item_type: ItemType = Field(default="post", description="Item type")


class UnlikeRequest(BaseModel):
    user_id: int = Field(..., description="User ID")
    item_id: int = Field(..., description="Item ID")
    item_type: ItemType = Field(default="post", description="Item type")


class CountRequest(BaseModel):
    item_id: int = Field(..., description="Item ID")
    item_type: ItemType = Field(default="post", description="Item type")


class BatchCountRequest(BaseModel):
    items: list[CountRequest] = Field(..., description="List of items to get counts for")


class HasLikedRequest(BaseModel):
    user_id: int = Field(..., description="User ID")
    item_ids: list[int] = Field(..., description="List of item IDs")
    item_type: ItemType = Field(default="post", description="Item type")


# Response schemas
class LikeResponse(BaseModel):
    status: Literal["liked", "already_liked", "unliked", "not_liked"]
    count: int = Field(..., description="Current like count")
    user_liked: bool = Field(..., description="Whether user has liked the item")


class CountResponse(BaseModel):
    item_id: int
    item_type: ItemType
    count: int
    cached: bool = False
    as_of: Optional[datetime] = None


class BatchCountResponse(BaseModel):
    counts: dict[str, int] = Field(..., description="Map of item_type:item_id to count")
    took_ms: float = Field(..., description="Time taken in milliseconds")


class HasLikedResponse(BaseModel):
    liked: dict[str, bool] = Field(..., description="Map of item_id to liked status")


class HealthResponse(BaseModel):
    status: str
    database: str
