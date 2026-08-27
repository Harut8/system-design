# Design Primitives

A compounding layer over [`tasks/`](../tasks/) and [`solutions/`](../solutions/).

A worked solution teaches you *one design*. A primitive sheet extracts the
reusable part, so the next design costs less than the last one. Memorising
designs is linear — every new one costs full price. Extracting primitives is
not.

---

## The idea

System design interviews do not test *N designs*. They test **~23 reusable
decision primitives** — partitioning, replication, quorums, caching, hot keys,
write-path/read-path asymmetry, capacity arithmetic, failure handling,
coordination, backpressure — plus a toolbox of mechanisms that serve them. A
design is just a vehicle for a subset.

But studying primitives *abstractly* fails as badly as memorising designs, and
arguably worse:

> "Partitioning: hash spreads load evenly, range supports ordered scans."

Three sentences, and it is trivia. It does not stick and it does not transfer,
because you never felt the moment where a requirement made the choice
non-optional. In the key-value store, range partitioning was not a preference —
the `Scan` requirement made hash partitioning arithmetically impossible at the
latency target. *That* is the transferable thing, and you can only get it by
working the case.

**So: designs are the vehicle, primitives are the cargo.** You still do the
designs. What changes is what you write down at the end, and what you check
yourself against.

---

## The template

Copy this per primitive the design touched. Six fields, each earning its place.

```markdown
### <Primitive name>

**Choice:**  <what this design picked>
**Forced by:**  <the specific requirement that made it non-optional>
**In one breath:**  <plain-language mechanism, no jargon>
**The number:**  <the arithmetic that justifies it>
**Cost accepted:**  <what you gave up, stated as a real cost>
**Flips when:**  <the requirement change that reverses the choice>

**Follow-ups you must survive:**
- <question an interviewer will ask> → <your two-sentence answer>
```

### Why each field

**Choice** — one line. If it takes three, you are describing an
architecture, not extracting a decision.

**Forced by** — the most important field. A design decision that isn't
traceable to a requirement is a preference, and preferences don't survive
follow-up questions. If you cannot name the forcing requirement, you have
found a genuine gap in your understanding, not a formatting problem.

**In one breath** — plain words, no vocabulary. This is the defensibility
test. Every named mechanism you say in an interview is an invitation: say
"closed timestamps" and a good interviewer asks what problem they solve.
If you can only produce the name, don't say the name. Writing the plain
version here is what makes it safe to use the term out loud.

**The number** — the arithmetic. Interviews reward "3 AZs, RF=3, quorum 2,
so an AZ loss leaves a majority" over "it's replicated for availability."
Numbers are also what let you *notice* when a design is wrong: the key-value
store's node count was set by write amplification, and only the arithmetic
revealed it.

**Cost accepted** — every real decision has one. An entry with no cost means
you have written marketing copy. This field is also, in practice, the answer
to half of all interview follow-ups.

**Flips when** — the transfer test, and the reason the sheet exists. If you
can state the requirement change that reverses the choice, you own the
primitive and you can re-derive it in a design you have never seen. If you
cannot fill this in, you memorised an answer.

---

## Bad entry vs good entry

The difference is the whole method, so it is worth seeing directly.

**Bad — a summary. Recognisable, not reusable:**

> ### Partitioning
> We use range partitioning with 512 MiB ranges that split automatically when
> they get too big or too hot. Ranges are spread across nodes by the allocator
> with AZ diversity constraints.

Everything there is true and none of it transfers. It describes *what this
system does*. Asked "would you range-partition a URL shortener?", it gives you
nothing — you would have to guess.

**Good — a decision. Reusable:**

> ### Partitioning
>
> **Choice:** Range, not hash.
> **Forced by:** Ordered `Scan` at P99 ≤ 50 ms. On a hash ring, a 1000-key
> prefix scan must query every partition, merge, and discard — the latency
> target is unreachable, not merely worse.
> **In one breath:** Keys are kept in sorted order and the keyspace is cut
> into contiguous chunks, so a scan touches a couple of chunks instead of
> all of them.
> **The number:** 50 TB / 512 MiB ≈ 100k ranges. A 1000-key scan touches 1–2.
> **Cost accepted:** Sequential keys (`/events/<timestamp>`) all land in the
> last range and splitting doesn't help, so hot spots need explicit
> mitigation. Plus split/merge/rebalance machinery a hash ring never needs.
> **Flips when:** Drop the ordered-scan requirement → hash wins immediately,
> and takes the entire hot-spot problem and the allocator with it.

Now ask "would you range-partition a URL shortener?" and the answer falls out
in seconds: lookups are by random hash key, nobody scans, so **hash** — and you
just bought yourself immunity to hot spots for free.

That is what "the primitive transfers" means concretely.

---

## The checklist, in four tiers

### Why tiered, and not one flat list

The usual "50 patterns of system design" table is good *coverage* and a bad
*study structure*, because it conflates four kinds of thing that need four
different treatments:

| Kind | Example | What it needs from you |
|---|---|---|
| **Decision** | Range vs hash partitioning | The full six-field sheet. There's a trade-off axis and a forcing requirement |
| **Mechanism** | Bloom filter, fencing token | One line of recall. You don't "decide" it in the abstract — you reach for it when a decision's cost bites |
| **Practice** | Observability, SLOs, canary rollout | A *position*, one or two sentences. No trade-off axis, but their absence is a visible gap |
| **Sub-design** | Workflow orchestration, search | It's a design, not a primitive. Don't compress it into a row |

The tell that a list is flat when it shouldn't be: every row explains **why it
matters** and no row states **what you trade**. That is exactly the bad-entry
failure from the section above, at list scale — recognisable, not reusable.

A flat 50-row list is also undrillable. You cannot cover a column and
re-derive it, because there's nothing to derive.

### Tier 1 — Decision primitives

**These get sheets.** Walk this list after each design: *did this design touch
it?* If yes, extract it with the full template. If no, note which design will
teach it.

| # | Primitive | The question it answers | Trade-off axis |
|---|---|---|---|
| 1 | **Partitioning** | How is the keyspace cut? | Scan-ability vs. even load |
| 2 | **Replication & consensus** | How many copies, and who decides what's true? | Fault tolerance vs. write tail latency |
| 3 | **Quorums** | What does intersection actually guarantee — and not? | Availability vs. a real commit point |
| 4 | **Consistency levels** | What is promised, to whom, at what price? | Freshness vs. latency vs. partition availability |
| 5 | **Write path vs read path** | Which is hot, and what asymmetry does that justify? | Work at write time vs. at read time |
| 6 | **Storage engine** | LSM or B-tree? | Write amplification vs. read amplification |
| 7 | **Caching** | What's cached, where, how invalidated? | Hit rate vs. staleness vs. invalidation complexity |
| 8 | **Fan-out** | On write or on read? | Write cost vs. read latency; breaks at the celebrity tail |
| 9 | **Hot key vs hot partition** | Two different problems — which is this? | System-fixable vs. only client-routable |
| 10 | **Indexing & retrieval** | What lookup shape does the query force? | Query flexibility vs. write cost and index size |
| 11 | **Capacity arithmetic** | Which resource actually binds? | — (a method, not a choice) |
| 12 | **Failure detection & recovery** | Dead or slow, and what's guessing wrong worth? | Fast reaction vs. cost of a false positive |
| 13 | **Idempotency & dedup** | What happens when a client retries a write it can't see? | API friction vs. double-apply bugs |
| 14 | **Backpressure & overload** | What stops the feedback loop eating the cluster? | Rejecting work vs. collapsing under it |
| 15 | **Failure domains & placement** | What does "survives an AZ loss" cost to guarantee? | Diversity constraints vs. rebuild speed |
| 16 | **Geo-distribution** | What does the speed of light charge? | Write latency vs. region survivability |
| 17 | **Backup vs replication** | Which threats does replication *not* cover? | Cost vs. surviving your own bugs and operators |
| 18 | **Coordination** | Do you need it *at all*, and who arbitrates? | Correctness under contention vs. a hard availability floor |
| 19 | **Sync vs async & delivery semantics** | Inline or decoupled, and what's the delivery guarantee? | Latency and simplicity vs. burst absorption |
| 20 | **Read-model derivation** | Precompute the answer or compute it on read? | Write amplification vs. read latency; staleness either way |
| 21 | **Time & ordering** | Do you trust wall clocks for correctness? | Cheap ordering vs. a clock-skew correctness dependency |
| 22 | **Evolution & migration** | How does this change shape without downtime? | Ship-speed now vs. being able to change it later |
| 23 | **Work distribution & scheduling** | How does work map to workers, in what order? | Fairness and locality vs. scheduler complexity |

**Where interviews actually go.** 9, 12, 13, 14, 17, 18, and 22 are the ones
candidates skip, and they are disproportionately where senior interviews land.
Replication is table stakes; *"how do you tell a dead node from a slow one, and
what does guessing wrong cost"* separates people. So does **18**, in an
underrated way: the strongest answer is often *"we don't need coordination
here, and here's how I avoided it."*

**11 is where you find real errors.** The obvious binding resource is usually
wrong — the key-value store's node count turned out to be set by SSD write
endurance, not disk capacity, and only the arithmetic revealed it.

### Tier 2 — Mechanisms (the toolbox)

**These do not get sheets.** They are recall items: you meet a primitive's
cost, and you reach for the tool that pays it down. One line each is correct.
Know what problem each solves and roughly what it costs — that's enough.

| Mechanism | Serves primitive | What it buys |
|---|---|---|
| Consistent hashing + virtual nodes | 1 | Rebalance without remapping everything |
| Range splits / merges | 1, 9 | Load redistribution without moving data |
| Leader leases · ReadIndex | 2, 5 | Local reads without a consensus round per read |
| Learners + joint consensus | 2 | Membership change without dropping below quorum |
| Read repair · hinted handoff | 3 | Convergence in a leaderless store |
| Merkle-tree anti-entropy | 3, 17 | Find divergence between replicas cheaply |
| Vector clocks · CRDTs | 3, 21 | Concurrent writes without a leader or data loss |
| Hybrid logical clocks (HLC) | 21 | Causal ordering without trusting wall clocks |
| Closed / resolved timestamps | 4, 5 | Exact reads of a slightly older consistent snapshot |
| Write-ahead log (WAL) | 2, 6 | Durability before the data structure is updated |
| LSM compaction | 6 | Bounded read amplification; sets write amplification |
| Bloom filters | 6, 7 | Reject definitely-absent keys without touching disk |
| Tombstones + GC barriers | 6, 22 | Deletes that don't resurrect after a partition |
| TTL (read-time + compaction-time) | 6, 7 | Exact expiry semantics, zero-cost reclamation |
| Cache stampede protection (single-flight, jittered TTL) | 7, 14 | Stop N synchronised misses becoming N origin hits |
| Materialized views · CQRS | 20 | Precomputed read shape, decoupled from the write shape |
| Event sourcing | 20, 22 | State as a replayable fact log |
| Cursors / keyset pagination | 10, 20 | Traverse huge results without offset scans |
| Inverted index | 10 | Term → document lookup |
| Token bucket / leaky bucket | 14 | Rate limiting with burst tolerance |
| Retry budget + full jitter | 14 | Cap the retry feedback loop's gain |
| Circuit breakers · bulkheads | 14 | Stop one sick dependency taking the system with it |
| Queue-latency (CoDel-style) shedding | 14 | Shed on real congestion, not a stale configured rate |
| Queues · pub/sub · event logs | 19 | Decouple producers from consumers; absorb bursts |
| Dead-letter queues | 19 | Isolate poison messages instead of blocking the stream |
| Outbox / inbox pattern | 19, 13 | Make a DB write and its event atomic |
| Change data capture (CDC) | 19, 20 | Turn committed DB changes into an event stream |
| Leases + heartbeats | 18, 12 | Ownership that expires without a central reaper |
| Leader election | 18 | One arbiter, elected not configured |
| Distributed locks | 18 | Mutual exclusion across processes |
| **Fencing tokens** | 18 | Stop a paused lock holder corrupting state after expiry |
| Two-phase commit · Saga | 18, 19 | Multi-party atomicity / compensating workflows |
| Compare-and-swap | 18 | Safe concurrent update without a lock |
| Idempotency keys | 13 | Make a retry safe when you can't see the first attempt |
| Schema registry · fwd/back-compatible encodings | 22 | Change the wire format without a flag day |
| Dual-write + shadow-read migration | 22 | Cut over storage with rollback at every step |
| Service discovery · health-checked LB | 23 | Route only to instances that can serve |

**The one to internalise if you internalise nothing else here: fencing
tokens.** Distributed locks are the primitive teams reach for most and
misunderstand most — a lease expiring does not stop a GC-paused holder from
acting. Every lock story needs a fencing answer, and most candidates don't
have one.

### Tier 3 — Production concerns

**These need a position, not a sheet.** No trade-off axis in the design sense,
but their absence reads as inexperience, and one earned sentence each is worth
real signal. Bring them up unprompted when the design is otherwise done.

| Concern | The position to have ready |
|---|---|
| **Observability** | Name the 5–6 metrics that would actually page, not "we'd add monitoring." Leading indicators over lagging ones |
| **SLOs & error budgets** | A number, a window, and where the budget actually goes (rarely where you'd guess) |
| **Deployment & rollout** | Canary → one AZ → fleet, with SLO-triggered auto-rollback. Turns a 30-min bad deploy into 4 |
| **Config & feature flags** | Dynamic behaviour without redeploy — and the flag read path is itself a read-hot-key problem |
| **Disaster recovery** | RPO and RTO as numbers, plus when you last *rehearsed* a restore |
| **Secrets & key management** | Rotation without downtime; the credential that can delete prod cannot delete backups |
| **Data migration** | Reversible at every phase, with a stated rollback window |
| **API design & contracts** | Versioning, pagination, and typed errors that tell the caller what to do |

### Tier 4 — Sub-designs

These appear on pattern lists but are **designs in their own right**. Trying to
compress them into a row is how you end up able to say the word and nothing
else. When one is central to a problem, it deserves its own 3-day sprint.

- **Workflow orchestration** — long-running multi-step operations, compensation, durable execution
- **Search & retrieval** — indexing, ranking, relevance ([`twitter-search`](../solutions/twitter-search-design.md))
- **Stream processing** — windowing, watermarks, late data, exactly-once effects
- **Ledger / payments** — double-entry, reconciliation, exactly-once money
- **Collaborative editing** — OT / CRDT convergence
- **Geospatial** — S2 / quadtrees, matching under movement

---

## The collapse: 50 rows → 11 clusters

A long flat pattern list is mostly *one primitive with several mechanisms
under it*, listed as peers. Seeing the collapse is what makes the surface
learnable — and it's the same move as the sheet itself.

| Cluster (learn as one) | Flat-list rows it absorbs |
|---|---|
| **Overload & backpressure** | retries & timeouts · circuit breakers · bulkheads · rate limiting · admission control · cache stampede |
| **Async & decoupling** | queues · streams/event logs · pub-sub · delivery semantics · ordering · DLQ · outbox/inbox · CDC |
| **Coordination** | leases & heartbeats · leader election · distributed locks · fencing tokens |
| **Write atomicity** | transactions · CAS · saga · exactly-once effects · idempotent processing |
| **Read optimisation** | caching · cache invalidation · materialized views · CQRS · pagination & cursors |
| **State lifecycle** | TTL · compaction · GC & tombstones · anti-entropy · reconciliation |
| **Time** | clocks · logical clocks / HLC |
| **Evolution** | schema evolution · serialization compatibility · data migration |
| **Traffic** | load balancing · service discovery · multi-region failover |
| **Work distribution** | work partitioning · scheduling · workflow orchestration |
| **Production practice** | observability · SLOs · config & flags · secrets · rollout · DR |

Eleven clusters, not fifty rows. And "exactly-once vs. idempotent processing"
stops being a row to memorise and becomes what it actually is — **the central
distinction inside one cluster**: you cannot guarantee exactly-once *execution*
across a network, so you make the *effect* idempotent instead. That sentence is
worth more than the two rows it replaces.

---

## Coverage across this repo

Four designs cover nearly the whole surface. This is the compounding, made
visible — by the fourth sheet you are mostly *confirming* primitives, not
learning them.

| Primitive | KV store | Instagram feed | Twitter search | Distributed counter |
|---|:---:|:---:|:---:|:---:|
| Partitioning | ●● | ○ | ●● | ● |
| Replication & consensus | ●● | — | ○ | ○ |
| Quorums | ●● | — | — | ○ |
| Consistency levels | ●● | ● | ○ | ●● |
| Write vs read path | ●● | ●● | ● | ●● |
| Storage engine | ●● | ○ | ● | ○ |
| Caching | ● | ●● | ● | ●● |
| Fan-out | — | ●● | ○ | — |
| Hot key vs hot partition | ●● | ●● | ● | ●● |
| Indexing | — | ○ | ●● | — |
| Capacity arithmetic | ●● | ●● | ●● | ●● |
| Failure detection & recovery | ●● | ○ | ○ | ● |
| Idempotency & dedup | ●● | — | — | ●● |
| Backpressure & overload | ●● | ● | ● | ● |
| Failure domains & placement | ●● | ○ | ● | ○ |
| Geo-distribution | ●● | ● | ● | ● |
| Backup vs replication | ●● | — | — | ○ |
| Coordination | ●● | — | — | ○ |
| Sync vs async & delivery | ○ | ● | ● | ●● |
| Read-model derivation | — | ●● | ●● | ●● |
| Time & ordering | ●● | ○ | ● | ● |
| Evolution & migration | ● | ○ | ○ | ○ |
| Work distribution & scheduling | ● | ● | ● | ● |

`●●` teaches it · `●` exercises it · `○` touches it · `—` absent

[`fastapi-rbac.md`](fastapi-rbac.md) is deliberately absent from this matrix —
it is an application-architecture problem, not a distributed-systems one, and
it scores on a different axis (authorization modelling, invalidation fan-out,
multi-tenancy). Read it for the Tier 3 concerns and for the job, not for Tier 1
coverage.

**What these four do *not* cover**, so you know what you're still missing:
collaborative convergence (OT/CRDT), the log-as-primary-abstraction, geospatial
indexing, CDN distribution, stateful long-lived connections, and ledger
semantics. All Tier 4 — each needs its own design, not a row.

**Read the columns, not the rows, when picking what to study next.** Two
designs with overlapping columns waste a week: the key-value store and the
distributed counter are both write-path/hot-key/sharding problems, so doing
both back-to-back feels like progress and isn't. The key-value store paired
with the Instagram feed is near-disjoint — that pair covers most of the table.

---

## How to actually use this

The sheet is worthless if you read it. It is a **drill target**, not a
document.

**Where it fits in a 3-day design sprint:**

| Day | Work | Sheet's role |
|---|---|---|
| 1 | Read the solution once, close it, rewrite from memory, compare | The gaps you find *are* your study list — not the whole solution |
| 2 | Attack gaps only. One timed 45-min whiteboard, out loud, recorded | — |
| 3 | Two timed runs with requirements changed mid-way. **Then write the sheet** (~1 hour) | This is the output of the sprint |

Writing the sheet *last* is deliberate. Written first, it becomes something to
copy from. Written after you have already produced the design cold, it is a
record of what you now know, and the fields you struggle to fill are precisely
the parts you don't.

**The drills:**

1. **Cover the "Choice" column.** Read only *Forced by* and re-derive the
   choice. This is the interview, exactly.
2. **Read "Flips when" and re-derive the alternative design.** If the
   requirement flips, what else changes downstream? (Drop `Scan` from the KV
   store and you lose the allocator, the split machinery, and the hot-spot
   problem — three things, not one.)
3. **Answer the follow-ups out loud, timed, from a blank page.** Silently
   thinking "yes I know that" is the failure mode this whole method exists to
   prevent.
4. **Cross-design check:** pick one primitive and read that row across all
   sheets. Why did the counter shard hot keys while the KV store batched them?
   Being able to answer that is real transfer.

**One rule that matters more than the sheet:** only use a term in an interview
if you can give the *In one breath* version and say why the mechanism exists.
Otherwise describe it plainly — "followers can serve reads as of a slightly
older timestamp the leader has promised not to write before" is stronger than
"closed timestamps", and it cannot be ambushed.

---

## Sheets

| Sheet | What it uniquely teaches |
|---|---|
| [`key-value-store.md`](key-value-store.md) | Consensus, quorums, consistency levels, storage engines, failure detection. **The densest — start here.** |
| [`instagram-feed.md`](instagram-feed.md) | Fan-out, precomputed read models, cache invalidation, graceful degradation, the celebrity problem |
| [`twitter-search.md`](twitter-search.md) | Inverted indexes, scatter-gather, stream windowing, and **when not to build the distributed system** |
| [`distributed-counter.md`](distributed-counter.md) | Splitting exact from approximate, bloom-filter discipline, reconciliation. Mostly *confirms* the others |
| [`fastapi-rbac.md`](fastapi-rbac.md) | Authorization modelling, invalidation fan-out, multi-tenancy. Application-scale, not distributed-scale |

**Reading order matters.** Key-value store → Instagram feed covers the most
ground fastest (near-disjoint, see the matrix above). Twitter search third for
indexing and the scale-appropriateness lesson. Distributed counter fourth — by
then you should recognise most of it, and *that recognition is the point*: it
is the first sheet where you are confirming rather than learning, which is what
compounding feels like from the inside.
