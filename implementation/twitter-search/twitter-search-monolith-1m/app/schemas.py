"""Pydantic schemas for request/response validation."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator
import re


# --- Tweet Schemas ---


class TweetCreate(BaseModel):
    """Schema for creating a new tweet."""

    user_id: str = Field(..., min_length=1, max_length=32)
    text: str = Field(..., min_length=1, max_length=280)
    language: Optional[str] = Field(None, max_length=5)
    geo_lat: Optional[float] = Field(None, ge=-90, le=90)
    geo_lon: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator("text")
    @classmethod
    def extract_hashtags_from_text(cls, v: str) -> str:
        """Validate text is not empty after stripping."""
        if not v.strip():
            raise ValueError("Text cannot be empty")
        return v


class TweetResponse(BaseModel):
    """Schema for tweet response."""

    tweet_id: str
    user_id: str
    text: str
    hashtags: list[str]
    language: Optional[str]
    geo_lat: Optional[float]
    geo_lon: Optional[float]
    likes: int
    retweets: int
    views: int
    created_at: datetime

    class Config:
        from_attributes = True


class TweetSearchResult(TweetResponse):
    """Schema for tweet search result with relevance score."""

    relevance: Optional[float] = None
    score: Optional[float] = None


# --- Search Schemas ---


class SearchRequest(BaseModel):
    """Schema for search request parameters."""

    q: str = Field(..., min_length=1, max_length=500, description="Search query")
    user_id: Optional[str] = Field(None, max_length=32)
    language: Optional[str] = Field(None, max_length=5)
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)


class SearchResponse(BaseModel):
    """Schema for search response."""

    results: list[TweetSearchResult]
    total: int
    took_ms: int
    query: str
    next_offset: Optional[int] = None


# --- Trending Schemas ---


class TrendingItem(BaseModel):
    """Schema for a single trending item."""

    term: str
    tweet_count: int
    rank: int


class TrendingResponse(BaseModel):
    """Schema for trending response."""

    trends: list[TrendingItem]
    region: str
    as_of: datetime


# --- Autocomplete Schemas ---


class AutocompleteSuggestion(BaseModel):
    """Schema for autocomplete suggestion."""

    term: str
    score: float


class AutocompleteResponse(BaseModel):
    """Schema for autocomplete response."""

    suggestions: list[AutocompleteSuggestion]
    query: str


# --- Engagement Schemas ---


class EngagementUpdate(BaseModel):
    """Schema for engagement update."""

    tweet_id: str
    action: str = Field(..., pattern="^(like|unlike|retweet|unretweet|view)$")


class EngagementResponse(BaseModel):
    """Schema for engagement response."""

    tweet_id: str
    likes: int
    retweets: int
    views: int


# --- Health Schemas ---


class HealthResponse(BaseModel):
    """Schema for health check response."""

    status: str
    database: str
    redis: str
    version: str = "1.0.0"


# --- Error Schemas ---


class ErrorResponse(BaseModel):
    """Schema for error response."""

    error: str
    detail: Optional[str] = None
    code: str


# --- Utility Functions ---


def extract_hashtags(text: str) -> list[str]:
    """Extract hashtags from tweet text."""
    pattern = r"#(\w+)"
    hashtags = re.findall(pattern, text)
    return [f"#{tag.lower()}" for tag in hashtags]
