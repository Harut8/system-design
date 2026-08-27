# Primitive Sheet: Instagram-Scale Feed

Extracted from [`solutions/instagram-feed-design.md`](../solutions/instagram-feed-design.md).
Method and template: [`README.md`](README.md).

**Pairs with [`key-value-store.md`](key-value-store.md).** The two are
near-disjoint: that one is storage, consensus, and correctness; this one is
fan-out, caching, ranking, and graceful degradation. Together they cover most
of Tier 1.

---

## 0. The meta-primitive: the shape of the work, not where it happens

Every feed design reduces to one question — **do you pay at write time or at
read time?** Every other decision follows from it.

```
Fan-out on WRITE (push):  1 post → 200 writes.  Read = 1 lookup.
Fan-out on READ  (pull):  1 post → 1 write.     Read = 200 queries + merge.
```

Neither wins outright, because **the follower distribution is a power law**.
The median user has 50 followers (push is trivially cheap); a celebrity has
100M (push is 100M writes for one post). One strategy cannot serve both ends
of that distribution.

**The whole design is: pick a threshold, use push below it and pull above it,
and merge at read time.** Threshold = 10,000 followers.

**Flips when:** the distribution isn't power-law. A B2B tool where every
account has 5–50 collaborators is pure push, no threshold, no merge, no
celebrity machinery. Half this design evaporates.

---

## 1. Fan-out

**Choice:** Hybrid — push below 10k followers, pull above it, merged at read.
**Forced by:** The power law. 0.1% of users have >1M followers, and pushing to
them would dominate the entire write budget of the system.
**In one breath:** Most people's posts are copied into their followers' inboxes
immediately; famous people's posts are left in one place and fetched when you
open the app.
**The number:**

```
100M posts/day × avg 200 followers = 20B feed writes/day  (push, everyone)
One 100M-follower celebrity post   = 100M writes for ONE post
Feed composition (20 posts):
    14–16  from pre-computed cache (pushed)
     2–3   from celebrities (pulled at read)
     2–3   recommendations (injected at read)
```

**Cost accepted:** Read path is now a merge of three sources instead of one
lookup, and ranking has to operate across all three. Plus a classification
system (who is a celebrity?) that must stay current.
**Flips when:** No power law → pure push. Or: reads become far rarer than
writes (an archive, an audit log) → pure pull, and the whole cache disappears.

**The threshold is the design.** 10k is not arbitrary — it's roughly where
`followers × post_rate` stops being cheaper than `readers × read_rate`. Be
ready to derive it rather than quote it.

**Middle tier matters too:** 10k–100k followers get *rate-limited* push —
fan-out capped at 100k updates/sec, completing within 10 minutes. Not every
user needs to be on one side of a binary.

**Follow-ups you must survive:**
- *"What if someone crosses the threshold?"* → Their existing pushed posts stay
  in caches; new posts go to the celebrity path. No backfill, no migration —
  the merge at read time makes the transition invisible.
- *"How does the reader know which celebrities to pull?"* → A precomputed
  `following_celebrities:{user_id}` set — the celebrity subset of the follow
  graph, cached with 24h TTL. Capped at 50 per read.

---

## 2. Read-model derivation

**Choice:** Precompute the feed as a Redis list of 500 post IDs per user.
**Forced by:** 200k QPS peak reads against a P99 of 200 ms. Assembling a feed
from scratch on every request cannot hit that.
**In one breath:** Store the *answer* (an ordered list of post IDs), not the
inputs, and rebuild it in the background as posts arrive.
**The number:**

```
500 post IDs × 8 bytes = 4 KB per user
500M active users × 4 KB = 2 TB of feed cache
Redis: 100 nodes (50 primary + 50 replica) × 64 GB = 3.2 TB
```

**Cost accepted:** 2 TB of RAM that is *derived* data — losable and rebuildable,
but expensive. Plus the cache is stale the instant a post is deleted (see
primitive 4).
**Flips when:** Read:write ratio inverts, or the feed is cheap to compute (few
followees, no ranking). Then compute on read and delete 2 TB of infrastructure.

**Store IDs, not posts.** The feed cache holds 8-byte post IDs, and post
metadata is hydrated in a batch fetch afterwards. Storing full posts would be
~1 KB × 500 × 500M = 250 TB, and every edit to a post would need to be written
to every copy. **The indirection is what makes the cache affordable and
mutable.** This generalises: cache the *keys* of an answer, hydrate the values.

---

## 3. Caching (the four-tier structure)

**Choice:** Four distinct caches with four different data structures and TTLs.
**Forced by:** Four different access patterns. One cache shape cannot serve all
of them.
**In one breath:** Match the Redis data structure to the query, not to the
data.

| Cache | Structure | Why that structure | TTL |
|---|---|---|---|
| `feed:{user}` | LIST | `LPUSH` + `LTRIM` = insert-and-cap in one op; `LRANGE` = pagination | 7 d |
| `celebrity_posts:{user}` | ZSET, score = timestamp | Need *range by time* (`ZREVRANGEBYSCORE`), not just recent-N | 7 d |
| `post:{id}` | HASH | Field-level reads and counter updates without rewriting the blob | 1 h |
| `affinity:{user}` | ZSET, score = affinity | Need top-N-by-score for ranking | batch |

**Cost accepted:** Four invalidation stories instead of one.
**Flips when:** The access pattern changes. A feed needing arbitrary time-range
queries would force the list → ZSET, and `LTRIM`'s free capping is lost.

**`LPUSH` + `LTRIM` is the pattern worth stealing:** a bounded, self-trimming
recent-items list with no background reaper and no unbounded growth. It shows
up in notifications, activity logs, and recent-searches everywhere.

---

## 4. Cache invalidation

**Choice:** **Lazy.** Deleted posts are filtered at read time, not removed from
caches at delete time.
**Forced by:** Proactive invalidation of a deleted post means N writes — one
per follower's cache. For a celebrity that's 100M writes to undo one post.
**In one breath:** Don't chase the copies; check validity when you're already
looking things up.
**The number:** Deletes are <0.1% of posts. The read path already does a batch
metadata fetch, so the `is_deleted` filter is **free** — no extra round trip.
**Cost accepted:** Deleted post IDs linger in caches, consuming slots until
they age out of the 500-item window. Best-effort async `LREM` cleans up.
**Flips when:** Deletes become common, or a delete is legally required to
propagate immediately (GDPR, takedowns). Then you pay the N writes — and you
size the system for it.

**The transferable rule: invalidation cost scales with fan-out, so put the
check wherever you're already paying for a lookup.** Lazy invalidation is
correct precisely when the read path already touches the source of truth.

---

## 5. Hot key vs hot partition

**Choice:** Two separate mechanisms, because there are two separate problems.
**Forced by:** They appear at opposite ends of the system.
**In one breath:** A celebrity is a hot key on the *write* side (one post, N
copies). A viral feed is a hot key on the *read* side (one Redis key, N
readers). Different fixes.

| | Celebrity (write-hot) | Hot feed key (read-hot) |
|---|---|---|
| Symptom | Fan-out queue explodes | One Redis node saturates |
| Fix | Don't fan out — flip to pull | Detect, then add replicas / route to a dedicated replica set |
| Detection | `follower_count` threshold, known in advance | Sampled `MONITOR` (1% of commands), >1000 ops/s |
| Nature | **Predictable** | **Emergent** |

**Cost accepted:** Two detection systems, one of which (sampling) is
approximate.
**Flips when:** Never — but note the asymmetry: the celebrity case is
*predictable from a counter you already have*, which is why it can be solved
structurally at write time. Emergent hot keys can only be detected and routed
around.

**Carry this distinction.** "Is this hotness knowable in advance?" determines
whether you can design it away or must detect it at runtime.

---

## 6. Write path vs read path

**Choice:** Split every operation into sync (user waits) and async (user
doesn't).
**Forced by:** Post creation must feel instant; fan-out to 200 followers and
30-second video transcoding cannot happen inside that request.
**In one breath:** The user waits only for the part that determines whether
their action succeeded; everything else is a background consequence.

| Operation | Mode | Why |
|---|---|---|
| Post metadata write | **Sync** | User needs the confirmation |
| Media processing | Async | 5–30 s |
| Fan-out | Async | Minutes for popular users |
| **Ranking** | **Sync** | The feed response is meaningless without it |
| Counter updates | Async | High volume, eventual is fine |
| Affinity scores | Batch (hourly) | Expensive, slow-moving |

**Cost accepted:** The user's own post is not in their feed for up to 30 s.
**Flips when:** —

**The best trick in the whole document is how they resolve that cost:** they
don't fix read-your-writes in the backend. They **show the user's own post in a
client-side banner at the top of the feed.** The backend stays eventually
consistent and the user perceives instant consistency.

**Generalise it: "the user must see their own write immediately" is a
*perceptual* requirement, and perceptual requirements can sometimes be met in
the client for free.** Always ask whether the guarantee is needed in the data
or only in the experience — it can save an entire consistency tier.

---

## 7. Ranking under a latency budget

**Choice:** Two-stage — cheap scoring on ~1000 candidates, expensive ML on the
surviving 200.
**Forced by:** A 50 ms ranking budget. ML inference on 1000 candidates does not
fit; on 200 it does.
**In one breath:** Use a cheap filter to throw away most candidates, then spend
the real compute on the few that survive.
**The number:**

```
Stage 1  1000 → 200   recency + log-scaled engagement   budget 20 ms
Stage 2   200 →  20   GBDT on ~19 features             budget 30 ms
                                              total    < 50 ms
```

**Cost accepted:** Stage 1 can discard something Stage 2 would have ranked #1.
Recall loss, accepted for latency.
**Flips when:** Candidates shrink (few followees) → single-stage. Or inference
gets cheap enough to run on 1000.

**This is the general funnel pattern** — cheap-and-broad, then
expensive-and-narrow — and it recurs in search retrieval, recommendation,
fraud detection, and log triage. Two details worth carrying:

- **Time decay as `exp(-age_hours / 6)`** — a 6-hour half-life, continuous, no
  cliff. Better than bucketing by age.
- **`log1p` on engagement counts** — a post with 1M likes shouldn't score
  10,000× one with 100. Log-scaling counts before combining is near-universal
  in ranking.

---

## 8. Backpressure — degrading by changing architecture

**Choice:** Three escalating tiers keyed on Kafka consumer lag, ending in a
**runtime switch from push to pull for the entire system**.
**Forced by:** A viral post can back the fan-out queue up faster than workers
can scale.
**In one breath:** As the backlog grows, first add workers, then serve only
users who are actually online, then stop pushing altogether and compute feeds
on demand.
**The number:**

```
lag > 100K    Level 1  autoscale workers 2×, page
lag > 500K    Level 2  fan out only to users active in last 24h; skip the rest
                       (inactive users get fan-out on next login)
lag > 1M      Level 3  STOP fan-out. Switch every feed read to pull mode.
                       Resume when lag < 100K.
```

**Cost accepted:** In pull mode, read latency degrades sharply (~500 ms) and
the cluster does far more read work.
**Flips when:** —

**Level 2 is the underrated one.** Fan-out to a user who hasn't opened the app
in a month is pure waste, and *deferring it to their next login costs nothing*
because the pull path already exists. Under load, the cheapest work to shed is
work for people who aren't watching.

**Level 3 is the real insight: because both a push path and a pull path exist,
the architecture itself becomes a load-shedding lever.** Having built the
hybrid for celebrities, they get a system-wide emergency mode for free. Ask of
any design: *does a fallback path I already built double as an overload mode?*

---

## 9. Graceful degradation

**Choice:** Every dependency has a defined, degraded-but-functional fallback.
Nothing returns 500.
**Forced by:** 99.99% availability across ~8 services. If any one being down
meant the feed being down, the composed availability would be far below target.
**In one breath:** Decide in advance what a worse-but-working answer looks like
for each dependency, so a failure degrades quality instead of availability.

| Down | Fallback | What degrades |
|---|---|---|
| Redis feed cache | Compute feed from followee posts | 8 ms → ~500 ms |
| **Ranking service** | **Sort by recency** | Relevance. Feed still works |
| Feature store | Stale features, up to 1 h | Ranking quality |
| Recommendations | Show followed content only | Diversity |
| Social graph | Cached followee list, 24 h stale | New follows invisible |
| CDN | Serve from S3 origin | Latency, S3 rate-limit risk |

**Cost accepted:** A fallback path per dependency, each of which must be
exercised or it will not work when needed.
**Flips when:** —

**Two details that separate this from a table nobody implements:**

- **The circuit breaker is what triggers it** (5 failures → open for 30 s), so
  degradation is automatic, not an operator decision at 3am.
- **`X-Feed-Quality: degraded` in the response header.** The system tells the
  client it's degraded. That's what makes degradation *measurable* — you can
  alarm on the rate — instead of a silent quality collapse nobody notices.

---

## 10. Capacity arithmetic — the number that reframes the problem

**Choice:** Treat media as a separate system, served by CDN, not by the feed.
**Forced by:** The arithmetic, which is lopsided in a way that is not obvious
until you do it.
**The number:**

```
Post metadata:  100M posts/day × 1 KB              =    100 GB/day
Media:          70M img × 500 KB + 30M vid × 10 MB =    335 TB/day
                                                      ─────────────
                                        Media is 3,350× the metadata.

Feed delivery:  20 posts = 20 KB metadata + 200 KB thumbnails
Peak bandwidth: 200K QPS × 220 KB = 44 GB/s
                → CDN absorbs 95%+, leaving ~2 GB/s to origin
```

**Cost accepted:** CDN spend, and a cache-invalidation story at the edge.
**Flips when:** Text-only content (Twitter) → media stops dominating and the
CDN becomes an optimisation rather than a structural necessity.

**The lesson is the method, not the number: compute each resource separately
and look for the lopsided one.** "Design Instagram's feed" sounds like a
database problem. The arithmetic says it is a *metadata* problem attached to a
much larger *distribution* problem, and the distribution problem is solved by
buying a CDN rather than by designing anything. Knowing which part isn't your
problem is worth as much as designing the part that is.

---

## 11. Consistency & the freshness budget

**Choice:** Eventual consistency for feeds, with a stated freshness target.
**Forced by:** 99.99% availability, chosen over correctness, for data where
being 30 seconds stale is imperceptible.
**In one breath:** A feed is a recommendation, not a ledger — nobody can tell
whether they're seeing the newest possible set of posts.
**The number:** New posts visible within **30 s at P99**. Celebrity posts up to
5 min. Like counts lag by seconds.
**Cost accepted:** Feed ordering can differ across sessions and devices;
mitigated by a consistent feed session ID so pagination doesn't shuffle
underneath the user.
**Flips when:** The content is money, inventory, or permissions. Then eventual
is unacceptable and you're designing the KV store instead.

**The transferable question: what is this data *for*?** A feed's job is to be
engaging, so freshness is a quality metric with a budget, not a correctness
constraint. Naming that explicitly is what licenses every other choice in this
design — and interviewers listen for whether you can tell the two kinds of data
apart.

---

## 12. Geo-distribution

**Choice:** Active-active regions, with replication strategy chosen **per data
store** rather than globally.
**Forced by:** Different data has genuinely different conflict semantics.
**In one breath:** Counters merge safely, so replicate them everywhere; the
follow graph doesn't, so it gets one home region.

| Store | Strategy | Why |
|---|---|---|
| Cassandra posts | Multi-DC async, `LOCAL_QUORUM` | Posts are immutable — no conflicts possible |
| Redis counters | **Active-active CRDT** | Counters merge commutatively |
| Redis feed cache | Active-active CRDT lists | Derived data; divergence self-heals |
| Kafka | MirrorMaker 2, <1 s | Events are append-only |
| **MySQL social graph** | **Single primary (us-west-2)** | Follow/unfollow ordering matters; concurrent conflicting edits are real |
| S3 media | Cross-region replication, <15 min | Immutable blobs |

**Cost accepted:** A follow takes 1–2 s to appear in other regions, and the
social-graph write path pays a cross-region hop for non-US users.
**Flips when:** —

**The pattern: immutable data replicates trivially, commutative data replicates
with CRDTs, and only order-dependent mutable data needs a single writer.**
Classify your stores that way and most of the multi-region design falls out.
Notice that only *one* of six stores actually needed a primary region.

---

## 13. Storage engine selection

**Choice:** Cassandra for posts, MySQL for the social graph, Redis for
everything derived.
**Forced by:** Three different access shapes.
**In one breath:** Append-heavy time-series goes to Cassandra; a
relational-integrity graph goes to MySQL; derived, rebuildable, latency-critical
state goes to RAM.
**The number:** 100M posts/day sustained writes; feed reads at 8 ms from Redis
vs ~25 ms for a Cassandra batch get.
**Cost accepted:** Three databases to operate, and no cross-store transactions.
**Flips when:** Under ~10M posts/day, PostgreSQL does all three jobs and the
operational saving beats the scaling ceiling. (The solution's §2 walks exactly
that ladder from 10k users upward.)

**Two encoding details worth carrying:**

- **`posts` is partitioned by `user_id`, with a second table `posts_by_id`.**
  Two access patterns → two tables, denormalised. Standard Cassandra practice
  and it surprises people coming from relational modelling: *you write the
  data once per query shape you need.*
- **`is_celebrity` is a stored generated column** (`follower_count >= 10000`).
  The classification that drives the entire fan-out strategy is a derived
  column, not application logic — one place to change the threshold.

---

## The index card

```
SCALE     500M DAU · 100M posts/day · 5B feed reads/day · 200K QPS peak
LATENCY   feed P99 200 ms · freshness 30 s P99

FAN-OUT   Hybrid. Push <10k followers, pull ≥10k, merge at read.
          Feed = 14-16 pushed + 2-3 celebrity + 2-3 recommended
          Middle tier 10k-100k: rate-limited push, 100k/s, done in 10 min
FEED      Redis LIST of 500 post IDs (8B each) = 4 KB/user = 2 TB total
          LPUSH + LTRIM = self-capping. Store IDs, hydrate metadata.
CACHES    LIST feed · ZSET celebrity(score=ts) · HASH post · ZSET affinity
INVALID.  LAZY. Deletes <0.1%; filter at read where you already batch-fetch.
HOT       Celebrity = write-hot, PREDICTABLE → design it away
          Viral feed key = read-hot, EMERGENT → detect (1% sample) + replicate
RANKING   Two-stage 1000→200 (20 ms, recency+log engagement) →20 (30 ms, GBDT)
          decay = exp(-age/6h) · log1p all counts
ASYNC     Sync: post metadata, ranking. Async: fan-out, media, counters.
          Read-your-writes solved in the CLIENT ("Your post" banner)
BACKPRESS Kafka lag 100K→scale · 500K→active users only · 1M→ALL PULL MODE
DEGRADE   Every dep has a fallback. Ranking down → recency sort.
          Circuit breaker triggers it. X-Feed-Quality: degraded header.
CAPACITY  Metadata 100 GB/day · MEDIA 335 TB/day (3,350×) → CDN's problem
GEO       Immutable→replicate free · counters→CRDT · social graph→1 primary

BIG IDEA  The hybrid built for celebrities doubles as a system-wide
          load-shedding mode. Fallback paths are overload modes.
```
