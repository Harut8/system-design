# Primitive Sheet: Twitter-Scale Real-Time Search

Extracted from [`solutions/twitter-search-design.md`](../solutions/twitter-search-design.md).
Method and template: [`README.md`](README.md).

**What this one uniquely teaches:** inverted indexes, scatter-gather reads,
stream windowing, and — the primitive the other three sheets never touch —
**knowing when not to build the distributed system.**

---

## 0. The meta-primitive: scale-appropriate architecture

This solution's most distinctive move is that it designs *two* systems and
gives you the threshold between them.

| | Simple monolith (Postgres + Redis) | Distributed (Kafka + Elasticsearch) |
|---|---|---|
| Best for | < 1M tweets/day | > 10M tweets/day |
| Team | 1–3 engineers | 5+ |
| Latency | < 50 ms | < 200 ms |
| Cost/month | $200–500 | $5,000+ |
| Time to build | 1–2 weeks | 2–3 months |

**Choice:** Start with the monolith. Migrate on a measured trigger.
**Forced by:** At 100k tweets/day the entire 7-day index is **630 MB** — it
fits in RAM on one machine. Postgres GIN full-text search answers it in under
50 ms.
**In one breath:** The distributed version is slower, costs 10× more, and takes
10× longer to build; it only wins when the data stops fitting.
**The number:** The distributed architecture is **4× worse on latency** (200 ms
vs 50 ms). Distribution is a cost you pay to get capacity, not a performance
upgrade.
**Cost accepted:** A migration later, on a known trigger, rather than never.
**Flips when:** > 10M tweets/day, or the index stops fitting in one machine's
memory, or a single-node failure becomes unacceptable.

**Say this in an interview and it lands.** Reaching immediately for Kafka and
Elasticsearch on a 100k/day problem is the most common failure mode in system
design interviews — and "here's the threshold at which I'd switch, and here's
what it costs me" demonstrates more judgment than the distributed design does
on its own.

---

## 1. Indexing — the inverted index

**Choice:** Inverted index (Elasticsearch), not a scan over a row store.
**Forced by:** "Find tweets containing keyword1 AND keyword2" over 7 days of
data, at P99 200 ms.
**In one breath:** Instead of storing documents and scanning them for words,
store words and the list of documents each appears in — so a query is a lookup
plus a set intersection.
**The number:**

```
Raw 7-day data at 500M tweets/day:   ~1 TB
With index overhead (~3×):           ~3 TB
Shards: 30–50 (50–100 GB per shard) × 3 replicas
```

**Cost accepted:** ~3× storage amplification, and every write does index-time
work (tokenise, analyse, post to N term lists).
**Flips when:** Lookups are all by primary key → no index. Or queries need
*semantic* similarity rather than term matching → vector index, an entirely
different structure.

**The index overhead multiple is the number people miss.** "1 TB of tweets"
becomes 3 TB of index, then 9 TB with replicas. Always state the multiple.

**The analyzer is where search behaviour actually lives:** `standard`
tokenizer → `lowercase` → synonyms → `edge_ngram`. Every one of those is a
product decision disguised as configuration — `edge_ngram` is what makes
prefix matching work at all.

---

## 2. Partitioning — sharding a time-series index

**Choice:** Sharding strategy **changes with scale**, ending at `hash(tweet_id)
+ time`.
**Forced by:** Two conflicting needs — cheap retention (drop old data) and even
write distribution.

| Scale | Strategy | Why |
|---|---|---|
| 100k/day | Single shard | Fits in memory |
| 1M+/day | **Time-based** (daily) | Retention = drop an index. Hot/cold tiering falls out |
| 100M+/day | **Hybrid** hash + time | Time alone concentrates *all* writes on today's shard |

**In one breath:** Slice by day so old data can be dropped wholesale, then
slice each day by hash so today's writes spread across machines.
**The number:** 30–50 shards at 50–100 GB each.
**Cost accepted:** A query for "last 7 days" fans out to 7 × N shards instead
of one.
**Flips when:** No retention policy → pure hash, and the time dimension buys
nothing.

**Time-based sharding is the primitive to carry.** For any append-mostly
time-series with a retention window, partition by time: deletion becomes
`DROP` of an entire partition instead of millions of row deletes, and it
composes with storage tiering:

```
Day 0–1   HOT    SSD, 3 replicas, serves most queries
Day 2–4   WARM   SSD, 2 replicas, force-merged into fewer segments
Day 5–7   COLD   HDD, 1 replica, read-only
Day 8+    DELETE (or archive to S3)
```

**Replica count and storage class track access probability.** Old data is
queried rarely, so it doesn't deserve three SSD copies. This is a
cost-optimisation primitive that generalises to logs, metrics, events, and
backups — anywhere access probability decays with age.

---

## 3. Scatter-gather

**Choice:** Query every shard in parallel, merge and rank centrally.
**Forced by:** A term's postings are spread across all shards, so no single
shard can answer "top 20 for this query."
**In one breath:** Ask everyone, wait for everyone, merge the answers.
**The number:** Each shard returns its local top-K; the coordinator merges
30–50 result sets and re-ranks.
**Cost accepted:** **Latency is the slowest shard, not the average.** With 50
shards, a P99 that occurs on 1 shard in 100 shows up in ~40% of queries. This
is the tail-amplification problem, and it is why scatter-gather systems obsess
over per-shard tail latency.
**Flips when:** Queries can be routed to one shard (search *within a user's*
tweets → shard by `user_id` and it becomes a single-shard lookup).

**The general lesson: fan-out multiplies tail latency.** N parallel calls means
your P99 becomes roughly the P(99^(1/N)) of each. Mitigations worth naming —
hedged requests, per-shard timeouts with partial results, and reducing N by
routing.

---

## 4. Stream processing & windowing

**Choice:** Flink, **1-hour tumbling window with a 30-second continuous
trigger**.
**Forced by:** "Trending must be < 1 minute fresh" over a 1-hour signal window.
Batch cannot do it; a pure 1-hour window emits only once an hour.
**In one breath:** Count hashtags over the last hour, but publish partial
results every 30 seconds instead of waiting for the hour to close.
**The number:**

```
.key_by((term, region))
.window(Tumbling 1 hour)
.trigger(ContinuousProcessingTime 30s)   ← freshness comes from here
.aggregate(Count)
.process(TopN per region, n=50)
→ Redis ZSET  (ZREVRANGE trending:global 0 9)
```

**Cost accepted:** Tumbling windows have a discontinuity at the boundary —
counts reset — where a sliding window would be smooth.
**Flips when:** Smoothness matters more than state cost → sliding window, at
substantially higher state overhead (every event belongs to many windows).

**Separating the window from the trigger is the transferable idea.** The window
defines *what is counted*; the trigger defines *how often you publish*. People
conflate them and end up choosing between a stale trend and a noisy one.

**Trending needs spam filtering before counting, not after** — rate-limit per
user (10 tweets/min count), drop exact-duplicate text, minimum account age 7
days. Any "most popular X" system is an attack surface; filtering after
aggregation is too late because the count is already poisoned.

---

## 5. Volatile fields do not belong in the index

The sharpest insight in this design, and the one most worth stealing.

**Choice:** Engagement counts (likes, retweets, views) live in a **Redis feature
store**, not in Elasticsearch — and are joined in at query time.
**Forced by:** Reindexing a document on every like is unaffordable. A viral
tweet would be rewritten thousands of times per second, and every rewrite is a
new segment for the merger to handle.
**In one breath:** The index holds the things that never change; the fast-moving
numbers live somewhere cheap to update and are attached at read time.
**The number:**

```
Feature store:  HASH engagement:{tweet_id} { likes, retweets, views }
                TTL 7 days (matches search lookback)

View batching:  HINCRBY engagement:123 views 1     ← per view: too many writes
                HINCRBY engagement:123 views 847   ← per 10 s: 847× fewer

ES sync:        every 5 min, ONLY tweets with recent activity
```

**Cost accepted:** Ranking needs an extra Redis round trip per query, and ES's
own copy of the engagement data is up to 5 minutes stale (used only for coarse
pre-filtering).
**Flips when:** The field is both volatile *and* needed for filtering (not just
ranking) — then it must be in the index and you pay the reindex cost.

**Carry the rule: split fields by mutation rate.** Immutable → index it.
Volatile → store it outside and join at query time. The same reasoning appears
in the feed design (post metadata in Cassandra, counters in Redis) and it's
what keeps both indexes from being rewritten constantly.

**Write batching for views is the second half:** collapsing 847 increments into
one is a ~99.9% write reduction, and it works because nobody can tell the
difference between a view count that's exact and one that's 10 seconds behind.
Always ask what precision the consumer actually needs.

---

## 6. Ranking — and a cross-design confirmation

**Choice:** A hand-written formula, not an ML model, for v1.
**Forced by:** Nothing yet — this is a deliberate deferral.
**In one breath:** Recency times engagement, both normalised, with weights you
can explain.
**The number:**

```python
recency    = exp(-age_hours / 6)                       # 6-hour half-life
engagement = log1p(likes)*0.4 + log1p(retweets)*0.4 + log1p(views/1000)*0.2
score      = recency*0.6 + engagement*0.4
```

**Cost accepted:** No personalization, no semantic matching, worse relevance
than a learned model.
**Flips when:** You have engagement data to train on, an inference budget, and
evidence the formula is losing. Phase 3, not Phase 1.

**Compare to [`instagram-feed.md`](instagram-feed.md) §7 — the formula is
essentially identical.** Both use `exp(-age/6h)` decay and `log1p` on counts,
independently, for different products. That is not coincidence, and noticing it
is exactly the cross-design transfer the method is for:

- **Exponential decay with a half-life** is how you express "newer is better"
  without a cliff, and 6 hours is a good default for social content.
- **`log1p` before combining counts** stops a 1M-like post from scoring 10,000×
  a 100-like post. Engagement is perceived logarithmically.
- **Weighted sum of normalised components** is legible, tunable, and
  debuggable — a real advantage over a model when something ranks wrong.

Where they diverge is instructive too: the feed adds a **second ML stage**
because it has a per-user affinity signal worth modelling. Search has no user
graph, so the formula is the whole thing. **Personalisation is what justifies
ML ranking; without it, a formula is usually enough.**

---

## 7. Soft delete

**Choice:** Soft delete — flag the row, filter on read, hard-delete nightly.
**Forced by:** A hard delete must land in Postgres, Elasticsearch, the query
cache, *and* the trending counts — atomically. That's a distributed transaction
across four systems for an operation that isn't worth one.
**In one breath:** Mark it deleted in one place, propagate asynchronously, and
have every read filter it out — then really delete it later, in bulk.
**The number:**

```
DELETE /tweets/{id}
  1. Postgres: is_deleted = true, deleted_at = now()
  2. Publish to Kafka tweets-deleted
  3. Return 202 Accepted            ← the API admits it is async
  ↓ async consumer
  ES is_deleted = true · invalidate cache · decrement trending
  ↓ nightly
  Hard DELETE where deleted_at < now() - 24h
```

**Cost accepted:** Index bloat until cleanup, and **an `is_deleted: false`
filter that must be on every single query** — forget it once and deleted
content is served.
**Flips when:** Legal deletion deadlines (GDPR, court orders) require provable
removal within a window. Then the 24-hour cleanup becomes a compliance job with
verification, not a best-effort cron.

**`202 Accepted` is a design decision, not a status code.** It tells the caller
the delete is *accepted but not complete*. Returning `200` would be a lie, and
clients would build on a guarantee that doesn't exist.

**Compare to [`instagram-feed.md`](instagram-feed.md) §4:** both defer delete
work to read time, for the same reason — the cost of eager propagation scales
with fan-out. Instagram filters deleted posts during feed hydration; search
filters them in the query. **Same primitive, two surfaces.**

---

## 8. Materialized prefix index (autocomplete)

**Choice:** Precompute a Redis ZSET per prefix. No trie traversal at runtime.
**Forced by:** Autocomplete fires on every keystroke, so the budget is a few
milliseconds and there is no time to walk a structure.
**In one breath:** For every term, write it into a bucket for every one of its
prefixes, so looking up "elec" is one sorted-set read instead of a search.
**The number:**

```
autocomplete:e     → ZSET { election: 1.0, elon: 0.9, economy: 0.7 }
autocomplete:el    → ZSET { election, elon }
autocomplete:elec  → ZSET { election, electric, electricity }

ZREVRANGE autocomplete:elec 0 2   →  top 3, O(log N)
Rebuilt every 5 min · TTL 1 hour
Storage: one entry per prefix per term (a 10-char term → 10 entries)
```

**Cost accepted:** ~10× storage versus storing terms once, and suggestions are
up to 5 minutes stale.
**Flips when:** The term set is huge (all queries ever) rather than curated
(top ~600 terms) — then the prefix explosion stops being affordable and you
need an actual trie or FST.

**This is the space-for-time trade in its purest form**, and it is a
*materialized view* (Tier 1 #20): precompute the answer for every possible
query because the query space is small and bounded. Note also the **weighted
merge of three sources** — trending (1.0), popular queries (0.7), hashtags
(0.5) — a simple, explainable way to blend signals of different trustworthiness.

---

## 9. Caching & graceful degradation

**Choice:** 30-second TTL on a normalised-query cache; every dependency
degrades rather than fails.
**Forced by:** Search queries are Zipf-distributed — a small number of queries
are a large share of traffic — and the freshness requirement is seconds, not
milliseconds.
**In one breath:** Cache on the normalised query for half a minute, and when
something downstream breaks, return a worse answer rather than an error.
**The number:** cache key = `hash(normalized_query + filters)`, TTL 30 s,
target hit rate > 80%.

| Down | Fallback |
|---|---|
| Elasticsearch | Cached results, up to 5 min stale, **+ warning header** |
| Feature store | Recency-only ranking |
| Trending | Last known trends, up to 1 h stale |
| Autocomplete | Disabled; search still works |

**Cost accepted:** Results up to 30 s stale on a cache hit.
**Flips when:** Personalized results → the cache key must include the user, hit
rate collapses, and query caching stops being worth it. **Personalization and
caching are in direct tension** — worth saying out loud, because it's the
hidden cost of the "add personalization" suggestion.

**Normalising the query before hashing** (lowercase, sort terms, canonicalise
filters) is what makes the hit rate real: `"cat dog"` and `"dog cat"` must be
one cache entry.

---

## 10. Backpressure — bounded queues that drop

**Choice:** Bounded indexing queue that **drops the oldest** when full.
**Forced by:** Indexing is the slowest step; if ingestion outruns it, something
must give.
**In one breath:** Cap the queue, and when it's full throw away the stalest
work rather than growing memory until the process dies.
**The number:**

```
API gateway:   100 req/s per user · circuit opens at 50% errors in 10 s
               429 + Retry-After
Kafka:         alert at 10k lag · auto-pause consumption if downstream is slow
Elasticsearch: bulk index with exponential backoff
               queue depth 1000 documents, then DROP OLDEST
```

**Cost accepted:** Under extreme load, some tweets are never indexed — they
exist but aren't searchable.
**Flips when:** Every item must be indexed (an audit log, a legal archive).
Then you cannot drop; you must apply backpressure all the way to the producer
and refuse writes.

**Two things worth carrying:**

- **Auto-pause consumption is backpressure done right.** Rather than reading
  from Kafka and buffering in memory, stop reading. Kafka is already a durable
  buffer — using it as one is free, and it's the cleanest form of backpressure
  in an event-driven system.
- **Dropping the *oldest*** is deliberate. For a freshness-sensitive system,
  the stalest queued item is the least valuable. For a correctness-sensitive
  one it would be the opposite. **The eviction direction encodes what your
  system is for.**

---

## The index card

```
SCALE     Start: 100k tweets/day (630 MB index — fits in RAM)
          Target: 500M/day → 1 TB raw → 3 TB indexed → 30-50 shards × 3

META      MONOLITH until 1-10M/day. Distributed is 4× SLOWER (200 vs 50 ms),
          10× costlier, 10× longer to build. Distribution buys capacity,
          not speed. Know the threshold; say it out loud.

INDEX     Inverted index. ~3× storage overhead — always state the multiple.
          Analyzer = standard → lowercase → synonyms → edge_ngram
SHARD     100k: one · 1M+: by TIME (retention = drop a partition)
          100M+: hash + time (time alone puts all writes on today)
TIERING   Day 0-1 HOT SSD ×3 · 2-4 WARM ×2 · 5-7 COLD HDD ×1 · 8+ delete
          Replicas and storage class track access probability.
READ      Scatter-gather. LATENCY = SLOWEST SHARD. Fan-out amplifies tails.
STREAM    Flink · 1h TUMBLING window · 30s CONTINUOUS TRIGGER
          Window = what's counted. Trigger = how often you publish. Separate.
          Spam-filter BEFORE counting, never after.
VOLATILE  Engagement lives in Redis, NOT the index. Join at query time.
          Never index a field you'd have to rewrite on every like.
          Batch views: HINCRBY +847 per 10 s, not +1 per view.
RANK      exp(-age/6h)*0.6 + log1p(counts)*0.4
          ← SAME shape as instagram-feed. No user graph → no ML needed.
DELETE    Soft. Flag + Kafka + async propagate + nightly hard delete.
          202 Accepted (not 200). is_deleted filter on EVERY query.
AUTOCOMP  Materialized ZSET per prefix. ~10× storage, O(log N) lookup.
          Weighted blend: trending 1.0 · queries 0.7 · hashtags 0.5
CACHE     30 s TTL on normalized query. >80% hit.
          Personalization KILLS query caching — direct tension.
BACKPRESS Bounded queue, DROP OLDEST. Auto-pause Kafka consumption.
          Eviction direction encodes what the system is for.

BIG IDEA  Split fields by mutation rate: immutable → index it;
          volatile → keep it outside and join at read.
```
