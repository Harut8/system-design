import datetime
import math
import re
from collections import defaultdict
from typing import TypedDict

SCORES: dict[int, int] = {}


class Tweet(TypedDict):
    id: int
    text: str
    hashtags: list[str]
    author_id: int
    likes: int
    retweets: int
    views: int
    created_at: datetime.datetime


class InvertedIndex:
    def __init__(self):
        self.index = defaultdict(set)

    def add(self, word: str, tweet_id: int):
        self.index[word].add(tweet_id)

    def search(self, query: str):
        words = set(word.lower().strip() for word in query.split())
        return set.intersection(*[self.index[word] for word in words])


class SimpleSearchEngine:
    def __init__(self):
        self.tweets: list[Tweet] = []
        self.inverted_index = InvertedIndex()

    def add_tweet(
        self,
        tweet_id: int,
        text: str,
        hashtags: list[str],
        author_id: int,
        likes: int = 0,
        retweets: int = 0,
        views: int = 0,
    ):
        self.tweets.append(
            {
                "id": tweet_id,
                "text": text,
                "hashtags": hashtags,
                "author_id": author_id,
                "likes": likes,
                "retweets": retweets,
                "views": views,
                "created_at": datetime.datetime.now(),
            }
        )
        SCORES[tweet_id] = self._build_score(self.tweets[-1])
        words = set(re.findall(r"\w+", text.lower()))
        indexable = set(hashtags) | words
        for word in indexable:
            self.inverted_index.add(word, tweet_id)

    def _build_static_score(self, tweet):
        return len(tweet["text"]) + len(tweet["hashtags"])

    def _build_dynamic_score(self, tweet):
        return (
            math.log1p(tweet["likes"])
            + math.log1p(tweet["retweets"])
            + math.log1p(tweet["views"])
        )

    def _build_score(self, tweet):
        return round(
            (
                self._build_static_score(tweet) * 0.3
                + self._build_dynamic_score(tweet) * 0.3
                + self._build_recency_score(tweet) * 0.3
            ),
            2,
        )

    def _build_recency_score(self, tweet):
        return math.exp(
            -(datetime.datetime.now() - tweet["created_at"]).total_seconds() / 3600
        )

    def search(self, query: str):
        words = set(word.lower().strip() for word in query.split())
        if not words:
            return []

        # Collect all tweet IDs that match any word (OR search)
        result_ids = set()
        for word in words:
            result_ids |= self.inverted_index.search(word)

        # Sort by precomputed score
        sorted_ids = sorted(result_ids, key=lambda x: SCORES[x], reverse=True)

        # Map IDs to tweets safely
        id_to_tweet = {tweet["id"]: tweet for tweet in self.tweets}
        return [id_to_tweet[tid] for tid in sorted_ids]


index = SimpleSearchEngine()
index.add_tweet(1, "Happy New Year! #NewYear2026", ["NewYear2026"], author_id=101)
index.add_tweet(
    2,
    "Celebrating the New Year with friends #NewYear2026",
    ["NewYear2026"],
    author_id=102,
)
index.add_tweet(3, "2026 is here! #NewYear", ["NewYear"], author_id=103)


print(index.search("New Year"))
