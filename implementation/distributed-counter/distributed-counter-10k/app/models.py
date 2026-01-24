from sqlalchemy import Column, BigInteger, SmallInteger, DateTime, UniqueConstraint, Index
from sqlalchemy.sql import func
from app.database import Base


class UserLike(Base):
    """User-item like relationship for deduplication."""

    __tablename__ = "user_likes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    item_id = Column(BigInteger, nullable=False)
    item_type = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "item_id", "item_type", name="uq_user_item_like"),
        Index("idx_user_likes_item", "item_id", "item_type"),
        Index("idx_user_likes_user", "user_id", "item_type", "created_at"),
    )


class Counter(Base):
    """Aggregated counter for items."""

    __tablename__ = "counters"

    item_id = Column(BigInteger, primary_key=True)
    item_type = Column(SmallInteger, primary_key=True, default=1)
    like_count = Column(BigInteger, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_counters_updated", "updated_at"),
    )
