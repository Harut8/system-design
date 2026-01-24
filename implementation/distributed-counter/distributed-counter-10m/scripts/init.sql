-- Distributed Counter 10M Tier - Database Schema

CREATE TABLE IF NOT EXISTS user_likes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, item_id, item_type)
);

CREATE INDEX IF NOT EXISTS idx_user_likes_item ON user_likes(item_id, item_type);

CREATE TABLE IF NOT EXISTS counters (
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    like_count BIGINT DEFAULT 0,
    is_sharded BOOLEAN DEFAULT FALSE,
    shard_count SMALLINT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (item_id, item_type)
);

-- Hot items tracking
CREATE TABLE IF NOT EXISTS hot_items (
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    write_rate FLOAT DEFAULT 0,
    is_hot BOOLEAN DEFAULT FALSE,
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (item_id, item_type)
);
