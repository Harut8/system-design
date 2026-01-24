"""SQLAlchemy models with PostgreSQL full-text search support."""

from datetime import datetime
from typing import Optional
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def generate_tweet_id() -> str:
    """Generate a unique tweet ID."""
    return uuid.uuid4().hex[:16]


class Tweet(Base):
    """Tweet model with full-text search support."""

    __tablename__ = "tweets"

    # Primary key
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Tweet identifiers
    tweet_id: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, default=generate_tweet_id
    )
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # Content
    text: Mapped[str] = mapped_column(Text, nullable=False)
    hashtags: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)), default=list, server_default="{}"
    )
    language: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

    # Geolocation
    geo_lat: Mapped[Optional[float]] = mapped_column(Numeric(10, 8), nullable=True)
    geo_lon: Mapped[Optional[float]] = mapped_column(Numeric(11, 8), nullable=True)

    # Engagement metrics (denormalized for performance)
    likes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    retweets: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    views: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    # Soft delete
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # Full-text search vector (will be populated via trigger)
    search_vector: Mapped[Optional[str]] = mapped_column(TSVECTOR, nullable=True)

    # Indexes
    __table_args__ = (
        # Full-text search index (GIN)
        Index("idx_tweets_search", "search_vector", postgresql_using="gin"),
        # Composite index for recent non-deleted tweets
        Index(
            "idx_tweets_created_not_deleted",
            "created_at",
            postgresql_where=(is_deleted == False),
        ),
        # User tweets index
        Index(
            "idx_tweets_user_created",
            "user_id",
            "created_at",
            postgresql_where=(is_deleted == False),
        ),
        # Hashtags GIN index
        Index(
            "idx_tweets_hashtags",
            "hashtags",
            postgresql_using="gin",
            postgresql_where=(is_deleted == False),
        ),
    )

    def __repr__(self) -> str:
        return f"<Tweet(id={self.id}, tweet_id={self.tweet_id}, user_id={self.user_id})>"


class SearchLog(Base):
    """Log of search queries for autocomplete and analytics."""

    __tablename__ = "search_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    query: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    results_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_search_logs_created", "created_at"),
        Index("idx_search_logs_query_created", "query", "created_at"),
    )


# SQL for creating the search vector trigger (run during migration)
SEARCH_VECTOR_TRIGGER_SQL = """
-- Function to update search vector
CREATE OR REPLACE FUNCTION tweets_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.text, '')), 'A') ||
        setweight(to_tsvector('simple', COALESCE(array_to_string(NEW.hashtags, ' '), '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop existing trigger if exists
DROP TRIGGER IF EXISTS tweets_search_vector_trigger ON tweets;

-- Create trigger
CREATE TRIGGER tweets_search_vector_trigger
    BEFORE INSERT OR UPDATE OF text, hashtags ON tweets
    FOR EACH ROW
    EXECUTE FUNCTION tweets_search_vector_update();

-- Update existing rows
UPDATE tweets SET search_vector =
    setweight(to_tsvector('english', COALESCE(text, '')), 'A') ||
    setweight(to_tsvector('simple', COALESCE(array_to_string(hashtags, ' '), '')), 'B');
"""
