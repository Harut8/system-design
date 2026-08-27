# Primitive Sheet: Instagram-Scale Distributed Like Counter

Extracted from [`solutions/distributed-counter-design.md`](../solutions/distributed-counter-design.md).
Method and template: [`README.md`](README.md).

**Read this one fourth.** It overlaps [`key-value-store.md`](key-value-store.md)
heavily on hot keys, idempotency, and sharding — which makes it the best sheet
for *confirming* primitives rather than learning them. Where it does teach
something new: the exact/approximate split, bloom-filter discipline, and
reconciliation.

---

## 0. The meta-primitive: split the exact part from the approximate part

One API, two completely different guarantees, and recognising that is the whole
design.

```
"A user can like an item at most once."   → must be EXACT.   Never approximate.
"This post has 1,247,893 likes."          → ±0.1% is fine.   Nobody can tell.
```

**Choice:** Two subsystems. Dedup is exact and durable (Cassandra, one row per
user-item). Counts are approximate, derived, and reconcilable (Redis + async
aggregation).
**Forced by:** The scale gap. 500B like records at exact-once semantics is a
30 TB storage problem; 500M counters at exact-consistency semantics would be a
contention problem with no solution at 500k writes/sec.
**In one breath:** Be precise about who liked what, and relaxed about the
total — because users can detect the first and cannot detect the second.
**The number:**

```
Dedup store (exact):   500B records × 25 B     = 12.5 TB → 30 TB with indexes
Counter store (approx): 50B items × 32 B       = 1.6 TB
Hot counters in RAM:    100M items × 50 B      = 5 GB
```

**Cost accepted:** Displayed counts drift from truth by seconds and by up to
~1%, permanently reconciled but never exactly right at any instant.
**Flips when:** The count *is* the product — money, inventory, votes, rate-limit
quotas. Then the counter needs the same exactness as the dedup record, and you
are designing [`key-value-store.md`](key-value-store.md) instead.

**This is the highest-leverage question to ask in any design: which parts of
this actually need to be exact?** Applying one consistency level uniformly is
how systems become either too slow or too wrong. Most products contain both
kinds of data, and separating them is usually the biggest available win.

---

## 1. Idempotency & deduplication — layered by cost

**Choice:** Three layers, each cheaper and less certain than the next.
**Forced by:** 500k like-writes/sec, most of which are *new* likes. Hitting
Cassandra for every one is 500k reads/sec of pure verification.
**In one breath:** Ask the fast-but-fuzzy check first; only consult the slow
source of truth when the fast check says "maybe."

| Layer | Store | Latency | Certainty | Purpose |
|---|---|---|---|---|
| 1 | Bloom filter (in-process) | ns | **Definite NO** / maybe yes | Reject the common case free |
| 2 | Redis SET `recent_likes:{user}` | < 1 ms | Definite yes | Catch recent repeats |
| 3 | Cassandra `user_likes` | 5–15 ms | **Truth** | Final verification |

**The number:** Bloom + Redis absorb ~90% of the dedup load before it reaches
Cassandra.
**Cost accepted:** Three stores to keep roughly in sync, and one of them
(bloom) that can never be corrected downward — see §2.
**Flips when:** Write volume is low. At 10k likes/sec (§7), a single Postgres
`UNIQUE (user_id, item_id, item_type)` constraint does the entire job — the
database's own index *is* the dedup layer, and all three tiers vanish.

**The pattern generalises: order your checks by cost, and let cheap checks
short-circuit expensive ones.** It works whenever the cheap check has a
*one-sided* error — it may say "maybe" wrongly but never "no" wrongly.

**Compare to [`key-value-store.md`](key-value-store.md) §10.** Both make writes
idempotent, by opposite means. There, the client supplies a token and the
server remembers it — general, works for any operation. Here, the *operation
itself* is naturally idempotent because "user X liked item Y" is a set
membership, and set insertion is idempotent for free. **Ask whether your
operation can be modelled as a set insert before you build a token system.**

---

## 2. Bloom filters — and the discipline they require

**Choice:** 10 bits/key, ~1% false positive rate, partitioned by
`user_id % 1000`.
**Forced by:** Needing a dedup pre-check that costs nanoseconds and no network.
**In one breath:** A compact bit array that can tell you "definitely not
present" with certainty, and "probably present" with a known error rate.
**The number:** 1B entries × 10 bits ≈ **1.25 GB per shard**. Compare to
storing 1B keys exactly: ~25 GB. **20× compression for a 1% error rate.**

**Cost accepted:** 1% of new likes take a needless Cassandra round trip.

**The rule that makes it safe — and the detail this design gets exactly
right:**

> **A false positive must only ever cause extra work, never a wrong answer.**

Here, a bloom hit doesn't mean "already liked" — it means "go check Cassandra."
The wrong answer is impossible because the fuzzy layer never gets the final
word. A design that returned `already_liked` on a bloom hit would silently drop
1% of genuine likes.

**The asymmetry that bites: you cannot remove from a bloom filter.** From the
unlike path:

```python
# 3. Note: Cannot remove from Bloom filter (false positives acceptable)
```

Unlike leaves the bit set forever. Over time, the filter's false-positive rate
drifts upward as unlikes accumulate, and the only fix is periodic rebuild.
**Bloom filters are for append-mostly membership; every delete degrades them.**
If your workload deletes as often as it inserts, a bloom filter is the wrong
structure (counting bloom filters or cuckoo filters support deletion, at higher
cost).

---

## 3. Hot keys — sharded counters

**Choice:** Split a hot counter into 8 keys; write to a random one; sum on read.
**Forced by:** A viral post takes millions of increments per second against one
Redis key on one node.
**In one breath:** One counter becomes eight counters that add up, so eight
machines can take the writes.
**The number:**

```
counter:1:viral:1          = 5,000,000        ← one key, one node, one bottleneck
counter:1:viral:1:shard_0  =   625,000
...                                            ← 8 keys, 8 nodes, 8× throughput
counter:1:viral:1:shard_7  =   625,000

Write:  INCR shard_{random(8)}      — 1 op
Read:   MGET all 8, sum             — 1 round trip, 8 keys

Shard when:  > 1,000 writes/s sustained for 10 s
Merge back:  < 100 writes/s for 1 hour
```

**Cost accepted:** Reads cost 8 lookups instead of 1, and — the real cost —
**a sharded counter cannot support compare-and-swap or any conditional
update.** You can add to it; you cannot reason about its value atomically.
**Flips when:** The counter needs conditional logic ("decrement only if > 0",
inventory, quota). Then sharding is unavailable and you need a single
linearizable key, capped at that key's throughput.

**Automatic promotion and demotion is what makes it usable.** A design where an
engineer must decide which keys are sharded doesn't survive contact with a
viral post at 3am. Detect, shard, cool down, merge back — and note the
hysteresis (1,000/s for 10 s to shard; 100/s for 1 hour to merge) that prevents
thrashing.

**This is the same conclusion as [`key-value-store.md`](key-value-store.md)
§8**, reached from a different direction. There, batching-and-coalescing was
tried first and sharding was the fallback beyond ~50k writes/sec; here sharding
is the primary tool. Both end at: *you cannot split a key, so you must split
the key space and fan in on read, and you lose atomicity when you do.*

---

## 4. Write path — optimistic now, correct later

**Choice:** Return an optimistic count immediately; compute the real one
asynchronously.
**Forced by:** P99 < 50 ms for a like, while the durable path (Cassandra write
+ Kafka + Flink aggregation + counter DB) takes seconds.
**In one breath:** Increment a cached number and show it to the user right
away; let a background pipeline work out the true total and correct the cache.
**The number:**

```
SYNCHRONOUS (< 20 ms, user-facing)
  1. Rate limit check (Redis)          — 100 likes/min per user
  2. Dedup: bloom → Redis → Cassandra
  3. Kafka produce, acks=1             — fire and forget
  4. Redis INCR                        — the optimistic count
  5. Return 200 with that count

ASYNCHRONOUS (seconds, Flink)
  1-second micro-batches → dedup within window → verify against
  Cassandra → aggregate per item → batch write counter DB →
  refresh Redis → update bloom filters
```

**Cost accepted:** The number shown to the user is a guess. It is usually right
and occasionally corrected downward, which is visible if you watch closely.
**Flips when:** A wrong-then-corrected number is unacceptable (a bank balance).

**`acks=1` on Kafka is the detail worth understanding.** That's deliberately
weak durability — a leader failure loses the event. It's safe here because
**Kafka is not the source of truth; Cassandra is.** The like record is already
durable before the event is produced, so a lost event costs a temporarily wrong
*count*, which reconciliation (§6) will fix anyway.

**Generalise: an event stream doesn't need strong durability if the value it
derives is reconcilable from a durable source.** Decide what your source of
truth is, then let everything downstream of it be cheap. Getting this backwards
— treating the queue as the system of record — is a common and expensive
mistake.

---

## 5. Aggregation — batching as write amplification control

**Choice:** Flink micro-batches in 1-second windows, aggregating per item
before writing.
**Forced by:** 500k likes/sec against a counter DB that cannot take 500k
individual row updates/sec.
**In one breath:** Collect a second's worth of increments, add them up per
item, and write one update instead of thousands.
**The number:** **~100× write amplification reduction** (the solution's own
trade-off table). A viral post receiving 10,000 likes in a second becomes one
`+10000` write.
**Cost accepted:** Up to ~1 second of aggregation lag on top of everything
else, and Flink state to operate.
**Flips when:** Each event must be individually durable and ordered (a ledger)
— then you cannot collapse them, and the write volume is the write volume.

**Coalescing works because addition is associative and commutative.** This is
the same property that lets CRDTs merge counters across regions, and the same
one that lets [`key-value-store.md`](key-value-store.md) collapse 250 increments
into one Raft entry. **When your operation is commutative, batching is free
correctness-wise — and that's what makes counters so much more tractable than
general writes.**

---

## 6. Reconciliation — the primitive most designs forget

**Choice:** Periodically sample cached counters, compare to the source of
truth, and repair drift.
**Forced by:** The cache is updated by two independent paths (optimistic INCR
and the async aggregator), so drift is not a possibility — it is a certainty.
**In one breath:** Assume the fast copy is wrong, check a sample of it on a
schedule, and fix what has slipped.
**The number:**

```
Normal items:  every 5 min  · sample 10,000 random counters
               · refresh from DB if drift > 1%
Viral items:   every 30 s   · tolerate up to 5% drift
               (checked more often, judged more leniently)
```

**Cost accepted:** A background job forever, and a permanent accepted error
band.
**Flips when:** There is exactly one write path and it is transactional. Then
drift is impossible and reconciliation is dead code.

**Two things worth carrying:**

- **Sampling, not full comparison.** 10,000 of 50B counters is a vanishing
  fraction, and it's enough — you're measuring the *drift rate*, not fixing
  every counter. Full reconciliation at this scale would cost more than the
  system it protects.
- **The tolerance is inverted from intuition.** Viral posts get checked *more
  often* but are judged *more leniently* (5% vs 1%). More traffic means faster
  drift, so check often; but nobody can perceive 5% of 5 million, so the
  absolute accuracy matters less. **Error tolerance should scale with
  magnitude, and checking frequency with change rate** — two separate knobs
  that people usually collapse into one.

**Whenever a value is derived and cached with more than one writer,
reconciliation is not optional — it is the thing that keeps eventual
consistency from becoming permanent inconsistency.**

---

## 7. The scale ladder

**Choice:** Four tiers, each a complete working system.
**Forced by:** Nothing — this is the design admitting that most readers do not
have Instagram's traffic.
**The number:**

| Tier | Load | Architecture | Dedup mechanism |
|---|---|---|---|
| 1 | 10k likes/s | **Postgres only** | `UNIQUE (user, item, type)` constraint |
| 2 | 100k/s | + Redis cache, async counters | Redis set + Postgres |
| 3 | 500k/s | + Kafka, Flink, Cassandra | Bloom → Redis → Cassandra |
| 4 | millions/s | + sharded counters, multi-region | The above, plus reconciliation |

**Cost accepted:** Migrations between tiers.
**Flips when:** —

**Tier 1 is the one to internalise.** At 10k likes/sec — which is a genuinely
successful product — the entire design is two Postgres tables, a unique
constraint, and a synchronous transaction. No Kafka, no Flink, no Cassandra, no
bloom filters, no reconciliation, **no eventual consistency at all**. The
counter is exact because a transaction makes it exact.

Everything in tiers 2–4 exists to buy throughput, and every bit of it costs
exactness. That is the trade in one sentence, and it's the same lesson as
[`twitter-search.md`](twitter-search.md) §0: **distribution buys capacity, and
it is paid for in guarantees and operational surface.**

---

## 8. Storage tiering & caching

**Choice:** Four tiers, each ~10× slower and ~10× larger than the one above.
**Forced by:** 10M reads/sec against a 5 TB counter store.
**In one breath:** Keep the hottest things closest, and accept that each step
outward is an order of magnitude worse.

| Tier | Store | Latency | Size | TTL |
|---|---|---|---|---|
| 0 | In-process heap | < 0.1 ms | 100 MB/instance | **1 s** |
| 1 | Redis cluster | < 1 ms | 50 GB | 1 h |
| 2 | Counter DB (sharded PG/Scylla) | 5–10 ms | 5 TB | — |
| 3 | Dedup DB (Cassandra) | 5–15 ms | 30 TB | — |

**The number:** ~95% Redis hit rate. Batch reads use `MGET` for 50 items in a
single round trip — the feed use case.
**Cost accepted:** Four places a value can be stale, and a cache-coherence
story for each.
**Flips when:** The working set fits in one tier. Below ~100k items, Redis
alone is the whole cache.

**Tier 0's 1-second TTL is the interesting choice.** An in-process cache with a
1-second lifetime looks pointless until you consider a viral post being read
100,000 times per second by one service instance: it collapses 100,000 Redis
round trips into one, and one second of staleness on a number that changes
constantly is undetectable. **Very short TTLs on very hot keys are a
request-coalescing mechanism, not really a cache** — and they're the cheapest
answer to a read-hot key that exists.

Compare [`instagram-feed.md`](instagram-feed.md) §14 (watch-plus-cache) and
[`key-value-store.md`](key-value-store.md) §8: three designs, three answers to
read-hot keys — short-TTL local cache, watch-invalidated local cache, and
follower reads. **All three move the read off the hot node; they differ only in
how invalidation happens.**

---

## 9. Partitioning

**Choice:** Different partition keys for the two stores, chosen by access
pattern.
**Forced by:** The two stores answer different questions.

```
Counter DB:  hash(item_id) % 256
             → "how many likes does item X have?"
             → hashed because item_ids are sequential (would hot-spot)

Cassandra:   partition = user_id, clustering = (item_type, item_id)
             → "has user X liked item Y?"  → single partition, one lookup
             → "all likes by user X?"      → single-partition range scan
             → no hot partitions: users have broadly similar like counts
```

**Cost accepted:** "Who liked item Y?" requires a second table (`item_likes`)
with the reverse key — the same denormalise-per-query-shape rule as
[`instagram-feed.md`](instagram-feed.md) §13.
**Flips when:** Follower counts become power-law-distributed like Instagram's —
then partitioning by user *would* hot-spot and the "no hot partitions" claim
fails.

**The reasoning to carry: partition by the key you filter on, and hash it if
it's sequential.** Sequential IDs plus range partitioning equals every write
landing on the last partition — the same hazard as
[`key-value-store.md`](key-value-store.md) §1's `/events/<timestamp>` case.

---

## 10. Geo-distribution & conflict resolution

**Choice:** Active-active regions, last-write-wins by timestamp, plus a global
Flink dedup pass in 5-second windows.
**Forced by:** Users like from everywhere, and a cross-region write would blow
the 50 ms budget.
**In one breath:** Each region accepts likes locally and mirrors them; a global
job merges the streams and removes duplicates that only become visible once the
regions are compared.
**The number:** Local write path < 50 ms; global aggregation lags by seconds.
**Cost accepted:** LWW can drop a genuinely concurrent action, and correctness
now depends on cross-region clock agreement.
**Flips when:** The action isn't idempotent. LWW is safe here **only because
"like" is a set-insert** — dropping a duplicate is the *correct* outcome.

**This is the honest weak point of the design, and worth being able to name.**
LWW-by-timestamp is generally a data-loss mechanism (see
[`key-value-store.md`](key-value-store.md) §25), and it's acceptable here purely
because the operation is idempotent and the duplicate resolution is the desired
behaviour anyway. If the same LWW rule were applied to, say, profile edits, it
would silently discard writes.

**Ask of any LWW design: what happens when it picks wrong?** If the answer is
"nothing, both writes meant the same thing," LWW is fine. If it's "a user's
change vanishes," it isn't.

---

## The index card

```
SCALE     500M DAU · 5B likes/day · 60k/s avg, 500k/s peak
          10M reads/s avg, 50M peak · ±0.1% display accuracy OK

META      SPLIT EXACT FROM APPROXIMATE.
          "liked at most once" = EXACT (Cassandra, 30 TB)
          "1,247,893 likes"    = APPROX (Redis, reconciled)
          Ask of every design: which parts must actually be exact?

DEDUP     3 layers by cost: bloom (ns) → Redis set (<1ms) → Cassandra (5-15ms)
          Absorbs ~90% before the DB. Cheap check must have ONE-SIDED error.
BLOOM     10 bits/key, 1% FP, 1.25 GB per 1B entries (20× vs exact)
          RULE: a false positive may cost work, NEVER a wrong answer.
          CANNOT DELETE from a bloom filter — unlikes degrade it forever.
HOT KEY   Shard into 8, write random, MGET+sum on read.
          Shard at >1000/s for 10s · merge back at <100/s for 1h
          COST: sharded counters cannot do CAS or conditional updates.
WRITE     Sync <20ms: ratelimit → dedup → Kafka(acks=1) → INCR → return
          Async: Flink 1s micro-batch → dedup → aggregate → DB → cache
          acks=1 is safe because KAFKA ISN'T THE SOURCE OF TRUTH.
BATCH     1-second aggregation = ~100× fewer counter writes.
          Works because addition is commutative (same reason CRDTs work).
RECONCILE Sample 10k counters/5min, refresh if drift >1%
          Viral: check every 30s, tolerate 5%.
          Check frequency ∝ change rate. Tolerance ∝ magnitude. Two knobs.
CACHE     T0 heap 1s TTL (request coalescing!) · T1 Redis 1h · T2 PG · T3 C*
          95% hit rate. MGET 50 items for feed loads.
SHARD     Counters: hash(item_id)%256 (hashed — ids are sequential)
          Dedup: partition=user_id, cluster=(type,item) — no hot partitions
GEO       Active-active + LWW + global Flink dedup in 5s windows.
          LWW is only safe because LIKE IS IDEMPOTENT (a set insert).

LADDER    10k/s = POSTGRES + A UNIQUE CONSTRAINT. Exact. Transactional.
          Everything above buys throughput and pays in exactness.
```
