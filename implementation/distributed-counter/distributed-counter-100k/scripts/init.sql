-- Distributed Counter 100K Tier - Database Schema
-- Same as 10K tier - PostgreSQL remains source of truth

CREATE TABLE IF NOT EXISTS user_likes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, item_id, item_type)
);

CREATE INDEX IF NOT EXISTS idx_user_likes_item ON user_likes(item_id, item_type);
CREATE INDEX IF NOT EXISTS idx_user_likes_user ON user_likes(user_id, item_type, created_at DESC);

CREATE TABLE IF NOT EXISTS counters (
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    like_count BIGINT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (item_id, item_type)
);

CREATE INDEX IF NOT EXISTS idx_counters_updated ON counters(updated_at DESC);
