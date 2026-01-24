from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime

ItemType = Literal["post", "reel", "comment"]


def item_type_to_int(item_type: ItemType) -> int:
    return {"post": 1, "reel": 2, "comment": 3}.get(item_type, 1)


def int_to_item_type(item_type_int: int) -> ItemType:
    return {1: "post", 2: "reel", 3: "comment"}.get(item_type_int, "post")


class LikeRequest(BaseModel):
    user_id: int
    item_id: int
    item_type: ItemType = "post"


class UnlikeRequest(BaseModel):
    user_id: int
    item_id: int
    item_type: ItemType = "post"


class CountRequest(BaseModel):
    item_id: int
    item_type: ItemType = "post"


class BatchCountRequest(BaseModel):
    items: list[CountRequest]


class HasLikedRequest(BaseModel):
    user_id: int
    item_ids: list[int]
    item_type: ItemType = "post"


class LikeResponse(BaseModel):
    status: Literal["liked", "already_liked", "unliked", "not_liked"]
    count: int
    user_liked: bool


class CountResponse(BaseModel):
    item_id: int
    item_type: ItemType
    count: int
    cached: bool = False
    as_of: Optional[datetime] = None


class BatchCountResponse(BaseModel):
    counts: dict[str, int]
    took_ms: float
    cache_hits: int = 0
    cache_misses: int = 0


class HasLikedResponse(BaseModel):
    liked: dict[str, bool]


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
