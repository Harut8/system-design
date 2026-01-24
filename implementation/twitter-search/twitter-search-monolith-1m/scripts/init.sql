-- PostgreSQL initialization script for Twitter Search
-- This runs automatically when the database container starts

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Note: Tables are created by SQLAlchemy on application startup
-- This file is for any additional initialization

-- Create a function to generate sample data (for testing)
CREATE OR REPLACE FUNCTION generate_sample_tweets(num_tweets INTEGER DEFAULT 100)
RETURNS INTEGER AS $$
DECLARE
    i INTEGER;
    sample_users TEXT[] := ARRAY['user1', 'user2', 'user3', 'user4', 'user5'];
    sample_hashtags TEXT[][] := ARRAY[
        ARRAY['#tech', '#ai'],
        ARRAY['#news', '#breaking'],
        ARRAY['#sports', '#football'],
        ARRAY['#music', '#concert'],
        ARRAY['#food', '#cooking']
    ];
    sample_texts TEXT[] := ARRAY[
        'Just discovered an amazing new AI tool that helps with coding! #tech #ai',
        'Breaking news: Major developments in the tech industry today #news #breaking',
        'What an incredible game last night! The team played amazingly #sports #football',
        'Heading to the concert tonight, so excited! #music #concert',
        'Made the most delicious pasta for dinner #food #cooking',
        'Working on a new project using machine learning #tech #ai',
        'The latest smartphone release looks promising #tech',
        'Great weather for outdoor activities today!',
        'Just finished reading an amazing book, highly recommend!',
        'Coffee and coding, perfect morning combination #tech'
    ];
    random_user TEXT;
    random_text TEXT;
    random_hashtags TEXT[];
    tweet_id TEXT;
BEGIN
    FOR i IN 1..num_tweets LOOP
        random_user := sample_users[1 + floor(random() * array_length(sample_users, 1))::int];
        random_text := sample_texts[1 + floor(random() * array_length(sample_texts, 1))::int];
        random_hashtags := sample_hashtags[1 + floor(random() * array_length(sample_hashtags, 1))::int];
        tweet_id := encode(gen_random_bytes(8), 'hex');

        INSERT INTO tweets (
            tweet_id,
            user_id,
            text,
            hashtags,
            language,
            likes,
            retweets,
            views,
            created_at
        ) VALUES (
            tweet_id,
            random_user,
            random_text,
            random_hashtags,
            'en',
            floor(random() * 1000)::int,
            floor(random() * 500)::int,
            floor(random() * 10000)::int,
            NOW() - (random() * interval '7 days')
        );
    END LOOP;

    RETURN num_tweets;
END;
$$ LANGUAGE plpgsql;

-- Instructions for generating sample data after tables are created:
-- SELECT generate_sample_tweets(1000);
