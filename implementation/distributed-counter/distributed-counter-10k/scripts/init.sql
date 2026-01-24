-- Distributed Counter 10K Tier - Database Schema

-- Item types enum (1=post, 2=reel, 3=comment)
-- Using SMALLINT for efficiency

-- User likes table (source of truth + deduplication)
CREATE TABLE IF NOT EXISTS user_likes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, item_id, item_type)
);

-- Index for reverse lookup (who liked this item)
CREATE INDEX IF NOT EXISTS idx_user_likes_item
ON user_likes(item_id, item_type);

-- Index for user's likes
CREATE INDEX IF NOT EXISTS idx_user_likes_user
ON user_likes(user_id, item_type, created_at DESC);

-- Counter table (aggregated counts)
CREATE TABLE IF NOT EXISTS counters (
    item_id BIGINT NOT NULL,
    item_type SMALLINT NOT NULL DEFAULT 1,
    like_count BIGINT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (item_id, item_type)
);

-- Index for efficient counter lookups
CREATE INDEX IF NOT EXISTS idx_counters_updated
ON counters(updated_at DESC);

-- Function to generate sample data for testing
CREATE OR REPLACE FUNCTION generate_sample_likes(num_likes INTEGER DEFAULT 1000)
RETURNS void AS $$
DECLARE
    i INTEGER;
    random_user BIGINT;
    random_item BIGINT;
BEGIN
    FOR i IN 1..num_likes LOOP
        random_user := floor(random() * 10000)::BIGINT + 1;
        random_item := floor(random() * 1000)::BIGINT + 1;

        -- Insert like (ignore duplicates)
        INSERT INTO user_likes (user_id, item_id, item_type)
        VALUES (random_user, random_item, 1)
        ON CONFLICT DO NOTHING;

        -- Update counter
        INSERT INTO counters (item_id, item_type, like_count)
        VALUES (random_item, 1, 1)
        ON CONFLICT (item_id, item_type)
        DO UPDATE SET like_count = counters.like_count + 1,
                      updated_at = NOW();
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Function to get counter with default
CREATE OR REPLACE FUNCTION get_counter(p_item_id BIGINT, p_item_type SMALLINT DEFAULT 1)
RETURNS BIGINT AS $$
DECLARE
    result BIGINT;
BEGIN
    SELECT like_count INTO result
    FROM counters
    WHERE item_id = p_item_id AND item_type = p_item_type;

    RETURN COALESCE(result, 0);
END;
$$ LANGUAGE plpgsql;
