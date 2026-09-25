# Chapter 06: Transactions Across Services — 2PC, Sagas, Transactional Outbox, and Idempotency

The practitioner's chapter on one question every backend runs into: how do you write to your
database **and** publish an event or call another service, without losing work or doing it twice?
It covers the dual-write problem, two-phase commit and why services rarely use it, sagas
(orchestration, choreography, compensation, isolation countermeasures, TCC), the transactional
outbox and inbox, and idempotency in depth. This chapter is the repo's reference page for
idempotency; other chapters link here instead of re-explaining it.

Prerequisites: system and failure models from [`00-primitives-and-system-models.md`](00-primitives-and-system-models.md); single-node ACID
and isolation levels from [`../databases/05-transactions-and-concurrency.md`](../databases/05-transactions-and-concurrency.md).

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
1. [The Dual-Write Problem — Every Failure Interleaving](#1-the-dual-write-problem--every-failure-interleaving)
2. [Two-Phase Commit, Practically](#2-two-phase-commit-practically)
3. [Sagas — Orchestration, Choreography, Compensation](#3-sagas--orchestration-choreography-compensation)
   - [3.9 Isolation anomalies and countermeasures](#39-isolation-anomalies-and-countermeasures)
   - [3.10 TCC — Try, Confirm, Cancel](#310-tcc--try-confirm-cancel)
4. [The Transactional Outbox and the Inbox](#4-the-transactional-outbox-and-the-inbox)
5. [Idempotency in Depth](#5-idempotency-in-depth)
   - [5.3 Idempotency keys for HTTP APIs](#53-idempotency-keys-for-http-apis)
   - [5.5 Consumer-side deduplication](#55-consumer-side-deduplication)
6. [Python Implementations](#6-python-implementations)
7. [Worked Example — SMB E-Commerce Checkout](#7-worked-example--smb-e-commerce-checkout)
8. [Decision Guide — Which Pattern for Which Situation](#8-decision-guide--which-pattern-for-which-situation)
9. [Production Pitfalls / War Stories](#9-production-pitfalls--war-stories)
10. [Interview Questions](#10-interview-questions)
11. [Real-World Cases](#11-real-world-cases)
12. [Sandbox Experiments — Run These Yourself](#12-sandbox-experiments--run-these-yourself)
13. [Key Takeaways](#key-takeaways)
14. [Cross-References](#cross-references)

---

## Start here — the whole chapter in plain words

**The problem.** A backend rarely does just one thing. Placing an order means writing the order to
a database, charging a card through a payment provider, telling the warehouse, and sending an
email. Inside one database, a transaction makes several writes all-or-nothing. Between a database
and a message broker, or between two services, there is no shared transaction: each write succeeds
or fails on its own, and the network can lose the reply to a write that actually succeeded. This
chapter is about making multi-system work end up either fully done or cleanly undone, with nothing
lost and nothing done twice.

**A real-world example.** A small online shop takes about 3,000 orders a day, peaking at 5
checkouts per second. (All numbers in this example are illustrative.) Checkout touches four
systems: the order database (Postgres), a card payment provider, an inventory service, and a
shipping service. The first version calls them one after another from the HTTP handler.

- **Double charges.** The payment provider times out on about 0.2% of calls, roughly 6 a day. The
  code retries. Some of those timeouts happened *after* the charge went through, so the retry
  charges the customer again.
- **Paid orders nobody ships.** The shop deploys 4 times a day. A rolling restart kills requests in
  flight. A handler killed after the order commit but before "tell the warehouse" leaves a paid
  order that no warehouse worker ever sees.
- **Duplicate orders.** Phones on a train retry `POST /checkout` when the reply is slow. Two orders,
  two parcels.
- **Refunds that silently fail.** Stock runs out after the card was charged. The refund call fails,
  the error is logged, and nobody looks.

The patterns in this chapter fix each of these:

- **Idempotency key** (§5.3). The client sends `Idempotency-Key: <uuid>` with `POST /checkout`. A
  retry with the same key gets the first response back instead of creating a second order.
- **Transactional outbox** (§4). The order row and an `OrderPlaced` event row are written in *one*
  Postgres transaction. A separate relay publishes the event afterwards. A crash anywhere means the
  event is published late, never lost.
- **Orchestrated saga** (§3, §7). A checkout orchestrator runs: authorize payment → reserve stock →
  capture payment → create shipment, saving its progress after every step. If a step fails before
  the money is captured, it runs *compensations*: release the stock, void the authorization.
- **Idempotent consumers** (§5.5). Every service that receives a message records the message id in
  the same transaction as its effect. Duplicates, which at-least-once delivery guarantees you will
  get, become no-ops.
- **Two-phase commit** (§2) is considered and rejected here: the payment provider and the broker
  cannot take part in it. It is the right tool *inside* a distributed database, not between
  services.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Dual write | one operation writes to two systems with no shared commit | paying at the counter and hoping the kitchen got the ticket |
| Atomic | all of it happens or none of it does | a bank transfer: never "debited but not credited" |
| Unknown outcome | the call may or may not have worked; the reply was lost | you mailed a cheque; did it arrive? |
| Two-phase commit (2PC) | a coordinator asks everyone "ready?", then tells everyone "commit" | a wedding: both say "I do" before the officiant pronounces |
| Coordinator / participant | the one who decides / the ones who vote and obey | the officiant / the couple |
| In-doubt (blocked) | voted yes, has not heard the decision, must wait | the couple waiting while the officiant has fainted |
| XA | the standard API that lets a transaction manager run 2PC across databases | a universal plug for 2PC |
| Saga | a sequence of local transactions, each undone by a compensation if a later one fails | booking a trip: flight, then hotel; cancel the flight if no hotel |
| Compensation | a new action that semantically undoes an earlier one | a refund, not a time machine |
| Pivot step | the point of no return; after it the saga only moves forward | the moment the plane doors close |
| Orchestration | one coordinator tells each service what to do next | a conductor with a score |
| Choreography | each service reacts to others' events; no coordinator | dancers who follow each other's moves |
| TCC | Try (reserve), Confirm (use), Cancel (release) | a hotel hold on your card before checkout |
| Semantic lock | a "pending" status that warns others the record is mid-saga | a "reserved" sign on a restaurant table |
| Transactional outbox | save the outgoing message in the same DB transaction as the data | writing the letter into the ledger; a clerk mails it later |
| Relay | the process that reads the outbox and publishes it | the clerk who mails the letters |
| CDC | change data capture: reading the database's own change log | reading the shop's till roll instead of asking the cashier |
| Inbox / dedup table | the receiver records which message ids it has already applied | crossing invoice numbers off a list before paying |
| Idempotent | doing it twice has the same effect as doing it once | pressing a lit elevator button again |
| Idempotency key | a client-chosen id that marks retries of the same request | a reference number on a bank transfer form |
| At-least-once | may be delivered more than once, never lost | a courier who re-delivers if unsure |
| Effectively-once | at-least-once delivery + idempotent processing | the courier re-delivers, you refuse the second box |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `N` | participants in a 2PC transaction, or steps in a saga | 2 – 10 | checkout saga: 5 steps |
| `a` | availability of one participant | 0.99 – 0.9999 | 0.999 |
| `a^N` | chance all `N` participants are up (what 2PC needs) | — | `0.999^3 ≈ 0.997` |
| `p_unknown` | share of remote calls whose outcome is unknown (timeout, reset) | 0.01% – 1% | 0.2% of payment calls |
| `T_step` | timeout for one saga step before it counts as "unknown" | 1 – 30 s | 10 s for authorize |
| `T_saga` | deadline for the whole saga before compensating | minutes – days | 15 min for checkout |
| `TTL_res` | safety-net expiry on a reservation (semantic lock) | ≫ `T_saga` | 24 h |
| `T_poll` | outbox relay polling interval | 50 ms – 1 s | 200 ms |
| `B` | relay batch size | 100 – 1,000 | 500 rows |
| `L_outbox` | outbox lag: age of the oldest unpublished row | < 1 s healthy | alert at 60 s |
| `T_lease` | how long an in-progress idempotency key blocks others before takeover | 3 – 10 × handler p99.9 | 60 s |
| `TTL_key` | how long a completed idempotency key is kept | ≥ `H_retry` + margin | 24 h – 7 d |
| `H_retry` | retry horizon: the longest a client might retry one request | seconds – days | mobile offline queue: 72 h |
| `W_dedup` | how long a consumer remembers processed message ids | ≥ broker retention + replay window | 7 d |
| `fp` | request fingerprint: hash of method, path and canonical body | 32 bytes | SHA-256 |
| `v` | per-aggregate version / sequence number | grows by 1 | order 42 at version 7 |
| `K` | number of relay partitions when running several relays | 1 – 16 | 1 for an SMB |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. The Dual-Write Problem — Every Failure Interleaving

> **In plain words.** When one piece of code writes to two systems, there are moments in between
> where a crash, a timeout, or a lost reply leaves the two systems disagreeing. You cannot close
> those moments by writing the code more carefully; you have to change the design so there is only
> one write that matters.
>
> **Real-world example.** A signup handler inserts the user row and then publishes `UserCreated`
> so the email service sends a welcome message. A deploy kills the process between the two lines.
> The user exists and never gets the email, and nothing anywhere records that it was missed.

### 1.1 The shape of the bug

```python
async def place_order(cart):
    async with db.transaction():
        order_id = await db.insert_order(cart)          # write 1: Postgres
    await broker.publish("order.events", {"type": "OrderPlaced", "order_id": order_id})  # write 2
    return order_id
```

There are two systems and no commit that covers both. Every remote operation has **three**
outcomes, not two: success, failure, and **unknown** (the request left, the reply never came). The
interleavings below are all the ways this code can go wrong.

### 1.2 Timeline A — commit to the database, then publish

```
A1  app: COMMIT ──ok──► app: publish ──ok──► broker                          correct

A2  app: COMMIT ──ok──► app ✗ killed (deploy, OOM, node loss)
    order exists; no event, ever                                             LOST EVENT

A3  app: COMMIT ──ok──► app: publish ──► broker stores it
                        app ◄──✗── ack lost / timeout
                        app: retry publish ──► broker stores it again         DUPLICATE EVENT

A4  app: COMMIT ──ok──► app: publish ✗ broker down; in-memory retries exhausted
    (a durable retry store would fix this; that store is the outbox, §4)       LOST EVENT

A5  app: COMMIT ──► DB commits ──✗── reply lost (connection reset)
    app sees an error, returns 500; client retries; second order created     DUPLICATE ORDER
```

A5 surprises people: a commit can succeed while the client sees an error. Only idempotency (§5)
fixes it, because the application cannot find out on its own whether that commit happened without a
key to look up.

### 1.3 Timeline B — publish, then commit (or publish inside the open transaction)

```
B1  app: publish ──ok──► app: COMMIT ──ok──                                   correct

B2  app: publish ──ok──► app: COMMIT ✗ (unique violation, deadlock victim,
                                        serialization failure, crash)
    consumers ship an order that does not exist                              GHOST EVENT

B3  app: publish ──ok──► consumer receives it within 2 ms
                         consumer: SELECT order 42 → not found (not committed yet)
                         app: COMMIT ──ok──                                  RACE (spurious failure)
```

Publishing from inside `async with db.transaction():` is timeline B. So is an ORM "before commit"
hook.

### 1.4 Timeline C — ordering, even when nothing crashes

```
T1: UPDATE order 42 SET status='PAID';       COMMIT at t=10 ms   publish "PAID"      at t=40 ms (GC pause)
T2: UPDATE order 42 SET status='CANCELLED';  COMMIT at t=20 ms   publish "CANCELLED" at t=25 ms

database final state : CANCELLED
broker order         : CANCELLED, PAID
consumer final view  : PAID                                                   WRONG FINAL STATE
```

The database serialized T1 before T2, but the two publishes raced. Downstream now believes the
order is paid and ships it.

### 1.5 The same problem with a synchronous call

```
charge card ──ok──► provider has charged $80
INSERT payment row ✗ crash                     → customer charged, no record of it

INSERT payment row 'pending'; COMMIT
charge card ──►  ✗ timeout after 10 s          → charged or not? UNKNOWN
```

Recording intent before the call (the second variant) is better: at least you know you must find
out. What to do with "unknown" is covered in §3.8 and §7.5.

### 1.6 Fixes that do not work

| "Fix" | Why it fails |
|---|---|
| Retry the publish in a loop | The retries live in process memory. A crash loses them (A2). |
| Publish from an after-commit hook | Still timeline A, in the same process. |
| Publish first, then commit | Ghost events (B2). |
| On publish error, delete the DB row | The delete can fail or crash too, and the publish may have succeeded despite the error (A3). |
| A background job that "reconciles" by scanning `updated_at` | Misses deletes, depends on clocks, and runs minutes late. A poor, homemade outbox. |
| XA across the DB and the broker | Most brokers and every third-party HTTP API cannot take part (§2.3). |
| Check-then-insert for dedup (`SELECT`; if absent, `INSERT`) | Two concurrent duplicates both see "absent". Only a unique constraint arbitrates. |

### 1.7 The fixes that do work — map of this chapter

| Approach | Idea | Section |
|---|---|---|
| **One write is the truth; derive the other** | Outbox: the DB commit is the truth, the event is derived and retried from it. Listen-to-yourself: the log is the truth, the DB is derived. CDC: derive events from the DB log. | §4 |
| **Atomic commit across participants** | 2PC. Works only when every participant speaks the protocol. | §2 |
| **Accept non-atomicity; converge** | Saga: forward steps, and compensations if a step fails. | §3 |
| **Make every repeat harmless** | Idempotency: keys, dedup tables, conditional writes. | §5 |

The core idea: two independent systems cannot commit atomically unless both speak a commit protocol.
So you (a) reduce the operation to **one** commit (outbox), and (b) make it safe to repeat
everything downstream of that commit (idempotency). Sagas compose those two into multi-step
business processes.

---

## 2. Two-Phase Commit, Practically

> **In plain words.** A coordinator asks every participant "can you commit?". Each one that answers
> yes has made a durable promise: it will commit if told to, and it keeps its locks until told. If
> the coordinator dies after collecting the votes but before announcing the decision, the
> participants are stuck holding locks until it comes back.
>
> **Real-world example.** An app server moves money between two databases using XA. It crashes
> right after both databases vote yes. Both account rows stay locked until the app server restarts
> and replays its transaction log. Meanwhile, every query touching those two accounts hangs.

The protocol is derived in full in [`../databases/05-transactions-and-concurrency.md` §6.1](../databases/05-transactions-and-concurrency.md#61-two-phase-commit-2pc), and
the distributed-database variants (Percolator, Spanner, CockroachDB parallel commits, Calvin) are in
[`../databases/19-distributed-databases-deep-dive.md` §4](../databases/19-distributed-databases-deep-dive.md#4-distributed-transaction-protocols-deep-dive). This section covers only what a service
developer needs to decide whether to use it.

### 2.1 The protocol in one picture

```
Coordinator C                    P1 (orders DB)                  P2 (inventory DB)
─────────────                    ──────────────                  ─────────────────
log BEGIN(tx)
PREPARE ───────────────────────► do work; force-log PREPARED;
                                 keep all locks
PREPARE ─────────────────────────────────────────────────────► same
        ◄──────────── YES ───────
        ◄──────────── YES ───────────────────────────────────────
force-log COMMIT(tx)     ◄── the commit point: the decision is durable here
COMMIT ────────────────────────► commit; release locks; ACK
COMMIT ──────────────────────────────────────────────────────► commit; release locks; ACK
log END(tx)  (may now forget tx)
```

Cost on the critical path: at least two round trips and `N + 1` forced log writes (each
participant's PREPARED record, then the coordinator's decision). Every participant holds its locks
from its first write until phase 2 reaches it.

### 2.2 The blocking window

```
t0  C ── PREPARE ──► P1, P2
t1  P1 votes YES (locks held)          P2 votes YES (locks held)
t2  C crashes ✗ — maybe before logging COMMIT, maybe after
t3  P1 asks P2: "did you hear a decision?"   P2: "no; I voted YES too"
    Abort is unsafe:  C may have logged COMMIT and told the client "done".
    Commit is unsafe: C may have decided ABORT (for example, it timed out waiting for a vote).
    → both wait. Locks stay held until C recovers.         IN-DOUBT / BLOCKED
```

A participant that has not voted, or voted NO, may abort on its own. Once it votes YES it gives up
that right. This is the defining weakness of 2PC: **one crashed coordinator can freeze data on
every participant**. In Postgres, an orphaned prepared transaction shows in `pg_prepared_xacts`,
keeps its row locks, and holds back the xmin horizon, so VACUUM cannot clean anything newer than
it. Table bloat grows until someone runs `COMMIT PREPARED` or `ROLLBACK PREPARED`.

XA systems let an operator force a decision on an in-doubt branch (a *heuristic* commit or
rollback). If the operator guesses differently from the coordinator's logged decision, the outcome
is *heuristic mixed*: some participants committed, some rolled back. JTA reports this as
`HeuristicMixedException`, and repairing the data is manual work.

### 2.3 XA in practice

XA (X/Open, 1991) is the interface between a transaction manager (TM) and resource managers (RMs):
`xa_start`, `xa_end`, `xa_prepare`, `xa_commit`, `xa_rollback`, `xa_recover`.

| Component | XA support |
|---|---|
| Postgres | `PREPARE TRANSACTION` / `COMMIT PREPARED`; off by default (`max_prepared_transactions = 0`) |
| MySQL / InnoDB | `XA START … XA PREPARE … XA COMMIT` |
| Java | JTA with a TM such as Narayana or Atomikos |
| Some JMS brokers (IBM MQ, ActiveMQ) | yes |
| Kafka | no; Kafka transactions are internal to Kafka |
| Third-party HTTP APIs (payments, email, SaaS) | no, never |

The operational requirements are easy to underestimate:

- The TM needs a durable log and a recovery process that runs `xa_recover` on restart. Embedded TMs
  inside auto-scaled app containers often lose that log along with the container.
- Lock hold time is set by the slowest participant, plus the coordinator's fsync, plus two round
  trips.
- Availability multiplies. A transaction needs every participant: with three participants at
  99.9%, `0.999^3 ≈ 99.7%` (see [`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md)).
- One TM must reach every service's database directly, which breaks the rule that each service owns
  its data behind its API.

### 2.4 Why service architectures avoid 2PC

| Concern | 2PC / XA across services | Saga + outbox + idempotency |
|---|---|---|
| Atomicity | yes, at commit | "all or compensated", eventually |
| Isolation | yes (locks held) | no; countermeasures needed (§3.9) |
| Availability | product of all participants | each step needs only its own service |
| Latency / lock time | locks across network round trips | local transactions only |
| Who can take part | only XA-capable resource managers | anything with an idempotent API |
| Coupling | shared TM, direct DB access | messages and APIs |
| Worst failure | in-doubt locks after a coordinator crash | a stuck saga, visible and repairable |

### 2.5 Where 2PC is the right answer

Inside **one distributed database**: Spanner, CockroachDB, TiDB, YugabyteDB and FoundationDB run a
commit protocol across shards for every multi-shard transaction. It works there because:

1. The coordinator's and participants' state is itself replicated with Paxos or Raft. A crashed
   node's role is taken over by another replica that has the log, so the blocking window needs a
   *majority* of a group to fail, not one process. (Gray and Lamport's "Paxos Commit" is the
   general form of this idea.)
2. One vendor controls every participant, the failure detector, and the timeouts.
3. The protocol is heavily optimized: single-shard transactions commit in one phase; CockroachDB's
   parallel commits overlap the phases ([`../databases/19-distributed-databases-deep-dive.md` §4.5](../databases/19-distributed-databases-deep-dive.md#45-cockroachdb-parallel-commit)).

Kafka's own transactions are also a 2PC-style protocol run by a transaction coordinator inside
Kafka (§5.6). And the cheapest distributed transaction is the one you avoid: if the order row and
the outgoing event live in the **same** Postgres database, a plain local transaction covers both.
That is the outbox (§4).

### 2.6 Three-phase commit, in one paragraph

3PC (Skeen, early 1980s) adds a PRE-COMMIT phase between voting and committing, so that before
anyone commits, everyone knows that everyone voted yes. With a perfect failure detector and bounded
message delays, a recovery coordinator can then finish the transaction without waiting for the
crashed one. Real networks have partitions and unbounded delays. Under a partition, the two sides
can reach different decisions (one side commits, the other aborts), which violates atomicity, the
one property the protocol exists to provide. It also costs an extra round trip. In practice nobody
uses 3PC; the production answer to blocking is to replicate the coordinator with consensus (§2.5).
Details: [`../databases/05-transactions-and-concurrency.md` §6.2](../databases/05-transactions-and-concurrency.md#62-three-phase-commit-3pc).

---

## 3. Sagas — Orchestration, Choreography, Compensation

> **In plain words.** A saga breaks one big job into a sequence of small steps, each a normal local
> transaction in one service, committed on its own. If a later step fails, the saga runs
> *compensating* steps that undo the earlier ones in business terms: a refund, a released
> reservation, an apology email. Nothing is locked across services. The price is that other users
> can see the half-finished state while the saga runs.
>
> **Real-world example.** A travel site books a flight, then a hotel, then charges the card. The
> hotel is full. The saga cancels the flight booking; the airline may charge a cancellation fee,
> and for a few seconds the seat was visible to others as taken. That is the trade: no global locks,
> visible intermediate states.

### 3.1 The model

A saga (Garcia-Molina and Salem, 1987) is a sequence of local transactions `T1 … Tn`, each with a
compensation `C1 … Cn-1`. The guarantee is that one of these two sequences runs to completion:

```
T1, T2, …, Tn                          (success)
T1, T2, …, Tj, Cj, …, C2, C1           (step j+1 failed; compensate in reverse)
```

That makes a saga **ACD**, not ACID. It is atomic in the "all or compensated" sense, each step is
consistent and durable, and there is **no isolation**: other transactions see `T1`'s effects before
`T2` runs, and see them undone later if the saga compensates.

### 3.2 Step types: compensatable, pivot, retriable

```
      compensatable steps                pivot                  retriable steps
      (can be undone)                (point of no return)      (must eventually succeed)
 ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
 │ T1 create     │→│ T2 authorize  │→│ T3 reserve    │→│ T4 capture    │→│ T5 create     │→ T6 notify
 │ order PENDING │ │ payment       │ │ stock         │ │ payment       │ │ shipment      │
 │ C1 reject     │ │ C2 void auth  │ │ C3 release    │ │ (no undo)     │ │ (retry until  │
 └───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘ │  it succeeds) │
                                                                          └───────────────┘
 failure in T1–T4  → compensate backwards (C3, C2, C1)
 failure in T5–T6  → never compensate; retry forward, then escalate to a human
```

- **Compensatable**: has a compensation, and may be undone.
- **Pivot**: the go/no-go step. If it succeeds, the saga will complete. If it fails, everything
  before it is compensated. It is neither compensatable nor retriable in the business sense.
- **Retriable**: comes after the pivot, so it must be idempotent and must eventually succeed (with
  retries, fixes, or human help).

Design rules that follow:

1. Put the steps most likely to fail (validation, stock check, card decline) **early**, before the
   pivot.
2. Put actions that are irreversible or expensive to undo **at or after** the pivot: capturing
   money, sending email, handing a parcel to a courier.
3. Split an irreversible action into *reserve* + *confirm* so the reserve half is compensatable.
   Card payments already work this way: *authorize* places a hold (voidable), *capture* moves money.
   This split is the core of TCC (§3.10).

### 3.3 Compensation is semantic undo, not rollback

| Forward action | Compensation | Note |
|---|---|---|
| Create order `PENDING` | Mark `REJECTED` (do not delete) | keep the audit trail |
| Authorize card | Void the authorization | cheap, before capture |
| Capture payment | Refund | a new money movement, visible on the statement; hence capture as the pivot |
| Reserve stock | Release the reservation | reservation should also expire on its own (§3.8) |
| Add 100 loyalty points | Subtract 100 points | inverse delta, never "restore the old balance" |
| Send email | Send a correction email | you cannot unsend |
| Ship parcel | Start a return | a business process, often manual |

A compensation must be:

1. **Idempotent.** It will be retried and redelivered.
2. **Retriable until it succeeds.** A compensation that can fail for business reasons ("refund
   rejected") is not a compensation; it needs a human escalation path.
3. **An inverse delta, not a restore.** Other sagas may have changed the value since. Writing back
   the old value would erase their changes (a lost update, §3.9).
4. **Safe if it arrives before the action it undoes.** A step times out, the orchestrator
   compensates, and the original request, still in flight, lands afterwards. The participant must
   record the cancellation (a *tombstone*) so the late forward action becomes a no-op. The TCC
   literature calls these cases *empty compensation* (cancel for an action that never happened) and
   *suspension* or *hanging* (the action arriving after its cancel).

### 3.4 Choreography

Each service listens for events and decides what to do. There is no central coordinator.

```
 Order svc              Payment svc             Inventory svc           Shipping svc
    │ OrderPlaced ────────►│                        │                        │
    │                      │ PaymentAuthorized ────►│                        │
    │                      │                        │ StockReserved ────────►│ (waits for PaymentCaptured?)
    │                      │◄─── StockReserved ─────│                        │
    │                      │ PaymentCaptured ───────────────────────────────►│ ShipmentCreated
    │◄───────────────────────────────────────────────────────────────────────│
    │
  failure path:            │                        │ StockUnavailable       │
    │◄─────────────────────────────────────────────── (order → REJECTED)     │
    │                      │◄──────────────────────── (payment voids auth)   │
```

The question mark in the diagram is the typical problem: the order of steps is spread across four
codebases, and adding one step means changing several services and checking that no event cycle was
created.

### 3.5 Orchestration

One orchestrator (often inside the service that owns the business process, here the order service)
sends commands and waits for replies. It holds the saga's state.

```
                         ┌─────────────────────────────┐
                         │  Checkout orchestrator      │
                         │  (state in saga_instance)   │
                         └──────────────┬──────────────┘
     AuthorizePayment ┌─────────────────┼─────────────────┐ CreateShipment
        CapturePayment│      ReserveStock / ReleaseStock  │
                      ▼                 ▼                 ▼
                ┌──────────┐      ┌───────────┐     ┌──────────┐
                │ Payment  │      │ Inventory │     │ Shipping │
                └────┬─────┘      └─────┬─────┘     └────┬─────┘
                     └────── replies (PaymentAuthorized, StockReserved, …) ─────► orchestrator
```

Participants know nothing about the saga. They expose idempotent commands and reply with events.

### 3.6 Orchestration vs choreography

| Dimension | Choreography | Orchestration |
|---|---|---|
| Where the flow lives | spread across services' event handlers | one place: the orchestrator's state machine |
| Coupling | services depend on each other's events | participants depend on nobody; the orchestrator depends on their APIs |
| Adding / reordering a step | change several services | change the orchestrator |
| Visibility ("where is order 42?") | reconstruct from logs and traces | one row in `saga_instance` |
| Compensation logic | each service must know when to undo | the orchestrator decides and commands |
| Timeouts / deadlines | awkward; who owns the timer? | natural: the orchestrator owns them |
| Risk | cyclic event dependencies, hard to test end to end | the orchestrator becomes a "god service" if it absorbs business logic |
| Extra infrastructure | none | the orchestrator and its state store (or a workflow engine) |
| Good fit | 2–3 steps, no compensation, "fan-out on a fact" (OrderPlaced → email, analytics, search) | compensations, branches, timeouts, 4+ participants, money |

A practical split: **orchestrate the transaction, choreograph the side effects.** The checkout
saga is orchestrated. Once it emits `OrderConfirmed`, email, analytics, and search indexing
subscribe to that event independently, because none of them can make the order fail.

### 3.7 Persisting the saga state machine

An orchestrator whose state lives in memory loses every in-flight saga on a deploy. Persist it:

```sql
CREATE TABLE saga_instance (
    saga_id         uuid        PRIMARY KEY,
    saga_type       text        NOT NULL,                 -- 'checkout'
    business_key    text        NOT NULL UNIQUE,          -- 'order:42' → at most one saga per order
    state           text        NOT NULL,                 -- 'AUTHORIZING', 'RESERVING', …, 'DONE', 'REJECTED'
    step            int         NOT NULL DEFAULT 0,       -- increments on every transition
    data            jsonb       NOT NULL DEFAULT '{}',    -- ids returned by steps: auth id, reservation id
    deadline_at     timestamptz NOT NULL,                 -- saga deadline T_saga
    next_attempt_at timestamptz NOT NULL DEFAULT now(),   -- when the sweeper should re-send the current command
    attempts        int         NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX saga_due ON saga_instance (next_attempt_at)
    WHERE state NOT IN ('DONE', 'REJECTED');
```

The rules that make it crash-safe:

1. **State change and outgoing command in one transaction.** The orchestrator writes the new state
   and inserts the next command into its *outbox* (§4) in the same local transaction. No dual write
   inside the orchestrator itself.
2. **Replies are consumed idempotently** (§5.5) and matched on `(saga_id, step)`. A reply for an
   old step is a duplicate or a straggler: ignore it.
3. **Serialize transitions per saga.** Lock the row (`SELECT … FOR UPDATE`) or use the `step`
   column as an optimistic version, so two orchestrator instances handling two replies cannot both
   advance the same saga.
4. **A sweeper resends due commands.** Every few seconds it finds sagas with `next_attempt_at <
   now()` and re-sends the current command with the **same** command id and idempotency key.
   Participants deduplicate, so resending is always safe. This one loop covers lost messages,
   crashed consumers, and orchestrator restarts.
5. **Terminal states are final.** `DONE` and `REJECTED` never transition again; late replies are
   logged and dropped.

### 3.8 Timeouts and deadlines

- **A step timeout is not a step failure.** When `AuthorizePayment` times out, the authorization may
  exist. Compensating immediately ("void") without an authorization id, or marking the order failed
  while the money is held, both go wrong. The correct moves, in order: retry with the same
  idempotency key; or **ask** the participant ("what is the status of the authorization for order
  42?"); compensate only when the participant confirms failure, or after you have made the forward
  action impossible (tombstone, §3.3).
- **A saga has a deadline** (`T_saga`), driven by business limits: card authorizations expire after
  a provider-specific window (commonly around a week), and customers will not wait an hour for
  "processing". Past the deadline, the orchestrator compensates, relying on tombstones to neutralize
  late forward actions.
- **Semantic locks need a safety-net expiry** (`TTL_res`). If a saga is lost entirely (a bug, a
  deleted row), its stock reservation must not block inventory forever. Make `TTL_res` much larger
  than `T_saga` (for example 24 h vs 15 min) so the expiry never races a healthy saga.
- **Alert on age, not only on errors.** A saga that stays in one state for 10× its normal duration
  is a bug, even if nothing logged an error. Debugging approaches for stuck work are collected in
  [`37-distributed-systems-debugging.md`](37-distributed-systems-debugging.md).

### 3.9 Isolation anomalies and countermeasures

Across services, a saga behaves like a transaction running at READ UNCOMMITTED: every step's
effect is visible the moment it commits locally. The anomalies from
[`../databases/05-transactions-and-concurrency.md` §2](../databases/05-transactions-and-concurrency.md#2-concurrency-anomalies-all-of-them) come back at the service level:

| Anomaly | Saga example |
|---|---|
| **Lost update** | Saga A reserves 2 units: stock 10 → 8. Saga B reserves 1: 8 → 7. A compensates by writing back "10". B's reservation is gone and stock is oversold. |
| **Lost update (state)** | The customer's *cancel* saga sets the order to `CANCELLED`; the checkout saga, one step behind, sets it to `SHIPPING`. |
| **Dirty read** | Saga A marks a customer "premium" (to be compensated). Saga B applies the premium discount to another order. A compensates; B's discount rested on data that never officially existed. |
| **Non-repeatable read** | The checkout saga reads the price in step 1 and again in step 4; a price change in between makes the totals disagree. |

Countermeasures (from Frank and Zahle, 1998, as popularized in Chris Richardson's *Microservices
Patterns*):

| Countermeasure | Idea | Example | Cost |
|---|---|---|---|
| **Semantic lock** | A compensatable step sets a `*_PENDING` status; others check it and wait, fail, or queue | order `APPROVAL_PENDING`; the cancel saga refuses or waits until it clears | every reader must handle "pending"; locks need expiry |
| **Commutative updates** | Design updates so their order does not matter | stock as `available -= n` / `+= n` deltas, reservations as rows | not every operation commutes |
| **Pessimistic view** | Reorder steps to reduce the business risk of dirty reads | apply the loyalty upgrade in a retriable step after the pivot | constrains step order |
| **Reread value** | Before overwriting, reread and verify nothing changed (optimistic lock); restart the saga if it did | capture step checks `orders.version` | wasted work on conflict |
| **Version file** | Record operations as they arrive so out-of-order ones can be reordered or netted out | `CancelReservation` arrives first → recorded; a late `Reserve` sees it and does nothing | bookkeeping per entity |
| **By value** | Choose the mechanism by business risk | orders above a threshold go through a stricter path (manual review, or a single-DB transaction) | two code paths |

In practice an SMB system needs three of these: **semantic locks** (pending statuses), **commutative
updates** (deltas, not restores), and **version checks / tombstones**. They show up again in §7.

### 3.10 TCC — Try, Confirm, Cancel

TCC is a saga in which every participant exposes three operations, and the forward step is split
into a reservation and a confirmation:

```
            Try (reserve)                 Confirm (use)             Cancel (release)
account:    available −= 80, frozen += 80  frozen −= 80              available += 80, frozen −= 80
stock:      available −= 1,  held += 1     held −= 1, sold += 1      available += 1,  held −= 1
card:       authorize $80                  capture                   void

Coordinator: Try all ──► all OK? ──yes──► Confirm all (retry until done)
                               └──no───► Cancel the ones that tried (retry until done)
```

Compared with a plain saga:

- **Better isolation.** The reserved amount is invisible to other transactions (it sits in
  `frozen`), which is a semantic lock built into the model. Nobody reads a dirty balance.
- **Structurally like 2PC**, but "prepare" is a business-level reservation, not a database lock. A
  frozen $80 blocks nobody from using the rest of the balance.
- **Every participant must implement three idempotent operations**, and must handle the classic
  TCC edge cases (documented, for example, by Alibaba's Seata): an *empty cancel* (Cancel arrives
  for a Try that never ran: succeed and record it), *suspension* (Try arrives after Cancel: reject
  it), and repeated Confirm or Cancel (no-ops). A small `tcc_branch(tx_id, branch, state)` table
  checked in the same local transaction handles all three.
- **Confirm and Cancel must not fail for business reasons.** Everything that can fail belongs in Try.

Card authorization and capture is the TCC most engineers already use without calling it that.

### 3.11 When to use a workflow engine

A hand-rolled orchestrator (the `saga_instance` table, the outbox, a sweeper, and a transition
table) is a few hundred lines of code and is fine for one to three saga types. Move to a durable
workflow engine such as Temporal (or its predecessor, Uber's Cadence, or a managed service such as
AWS Step Functions) when you have:

- many saga types, or saga types that change often (versioning long-running instances becomes a
  real problem);
- long waits: hours or days ("wait until the parcel is scanned", "wait for a human approval");
- signals and human steps, or complex branching and loops;
- a need for per-instance history, search, and replay in an operations UI.

In Temporal, workflow code is *durable*: its event history is persisted, and a crashed worker's
workflow is rebuilt by replaying that history. Workflow code must therefore be deterministic.
**Activities** (the calls that touch the outside world) are retried under a retry policy, so they
are at-least-once and **must be idempotent**. The engine removes the plumbing (state table,
sweeper, timers); it does not remove the need for idempotent participants and correct
compensations. Engine-level compensation semantics, and what to do when a compensation itself
fails, are covered in [`../solutions/workflow-orchestration-design.md` §12](../solutions/workflow-orchestration-design.md#12-saga-compensation-and-failure-semantics).

---

## 4. The Transactional Outbox and the Inbox

> **In plain words.** Do not publish the message from your code. Write it as a row into an
> `outbox` table in the same database transaction as the business change. Either both commit or
> neither does. A separate relay reads committed outbox rows and publishes them, retrying until the
> broker acknowledges. On the receiving side, the consumer records each message id in the same
> transaction as its own change, so repeats are ignored.
>
> **Real-world example.** The order service commits the order and an `OrderPlaced` row together.
> The relay is down for 10 minutes during a deploy. When it comes back, it publishes the 10
> minutes of backlog. The warehouse is 10 minutes late, but no order is missing.

### 4.1 The idea

```
               one local transaction (atomic)
   ┌──────────────────────────────────────────────┐
   │ UPDATE orders SET status = 'PLACED' …        │
   │ INSERT INTO outbox (… 'OrderPlaced' …)       │──── COMMIT
   └──────────────────────────────────────────────┘
                        │
                        ▼  later, asynchronously, at-least-once
             ┌────────────────────┐   publish   ┌──────────┐        ┌───────────────────────┐
             │ relay              │ ──────────► │ broker   │ ─────► │ consumers             │
             │ (polling or CDC)   │  wait ack   │ (Kafka…) │        │ (idempotent, §5.5)    │
             └────────────────────┘             └──────────┘        └───────────────────────┘
```

This fixes the dual write by reducing two writes to one: the database commit. The publish becomes a
derived action that can be retried from durable state. The price:

- **At-least-once publishing.** A relay crash after publishing but before marking the rows sent
  republishes them. Consumers must deduplicate.
- **Latency.** Up to one poll interval `T_poll`, or CDC lag.
- **Operations.** One more process, one more table, one more lag metric to watch.

### 4.2 Schema

```sql
CREATE TABLE outbox (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id       uuid        NOT NULL DEFAULT gen_random_uuid() UNIQUE,  -- consumers dedup on this
    aggregate_type text        NOT NULL,          -- 'order' → topic 'order.events'
    aggregate_id   text        NOT NULL,          -- '42'   → message key (per-aggregate order)
    event_type     text        NOT NULL,          -- 'OrderPlaced'
    payload        jsonb       NOT NULL,          -- self-contained; includes aggregate version
    created_at     timestamptz NOT NULL DEFAULT now(),
    published_at   timestamptz                    -- NULL = not yet published
);

-- The relay's only query path: small, because published rows drop out of it.
CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- High churn table: vacuum it more often than the default.
ALTER TABLE outbox SET (autovacuum_vacuum_scale_factor = 0.01,
                        autovacuum_vacuum_insert_scale_factor = 0.01);
```

`gen_random_uuid()` is built in from Postgres 13. The column names follow the conventions of
Debezium's outbox event router (`aggregatetype`, `aggregateid`, `type`, `payload` by default; all
configurable), so moving from a polling relay to CDC later does not require a schema change.

Payload guidelines: include `event_id`, the aggregate's version `v`, `occurred_at`, and a schema
version. Make it self-contained, so consumers do not have to call back to ask for details (that
call couples them to your uptime and reintroduces race B3). Keep secrets and card data out of it:
events are copied to many places.

### 4.3 Writing to the outbox

```sql
BEGIN;
-- Lock the aggregate row first. This is what makes outbox ids follow commit order per aggregate (§4.6).
UPDATE orders
   SET status = 'PLACED', version = version + 1
 WHERE id = $1 AND status = 'CART'
RETURNING version;                                  -- → 3

INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
VALUES ('order', $1, 'OrderPlaced',
        jsonb_build_object('order_id', $1, 'version', 3, 'total_cents', $2));
COMMIT;
```

That is all the application code does. There is no broker client in the request path.

### 4.4 Relay option 1: polling with `FOR UPDATE SKIP LOCKED`

```sql
-- inside one relay transaction
SELECT id, event_id, aggregate_type, aggregate_id, event_type, payload
  FROM outbox
 WHERE published_at IS NULL
 ORDER BY id
 LIMIT 500
   FOR UPDATE SKIP LOCKED;

-- publish all rows in id order; wait for broker acks

UPDATE outbox SET published_at = now() WHERE id = ANY($ids);
COMMIT;
```

`SKIP LOCKED` (Postgres 9.5+) lets a second relay instance skip rows that the first has locked
instead of waiting on them. What each crash point does:

| Crash point | Effect |
|---|---|
| Before the `SELECT` | nothing happened |
| After the `SELECT`, before publishing | transaction aborts, locks drop, rows are picked up next time |
| After publishing, before `COMMIT` | rows are still unpublished and get **republished**: duplicates, removed by consumer dedup |
| After `COMMIT` | done |

Notes:

- **Stop at the first failed publish** in a batch; do not skip it and publish later rows, or you
  reorder events (§4.6). The Python relay in §6.3 rolls back the whole batch and retries.
- Keep the batch small and the broker timeout short (a few seconds): the transaction stays open
  while publishing, and long transactions hold back VACUUM. A variant avoids the open transaction:
  claim rows with `UPDATE … SET claimed_until = now() + interval '30 s' … RETURNING`, commit,
  publish, then mark them published. It has more states to reason about; start with the simple
  version.
- Polling cost is small: one index scan on a tiny partial index every `T_poll`. To cut latency
  without polling faster, `NOTIFY outbox` in the writing transaction (Postgres delivers
  notifications only on commit) and `LISTEN` in the relay. Keep polling as the backstop, because
  notifications are not durable: a relay that is disconnected when one is sent never sees it.

### 4.5 Relay option 2: change data capture

A CDC connector, typically Debezium's Postgres connector running on Kafka Connect, reads the
write-ahead log through a **logical replication slot** (the `pgoutput` plugin). Every committed
`INSERT` into `outbox` becomes a change event. Debezium's outbox event router reshapes it into a
clean event, routes it to a topic per aggregate type, and keys it by aggregate id. Background on
CDC mechanisms: [`../databases/19-distributed-databases-deep-dive.md` §11](../databases/19-distributed-databases-deep-dive.md#11-change-data-capture-and-streaming).

Properties that polling does not have:

- Events come out in **WAL commit order**.
- No polling queries and no `UPDATE … published_at` churn.
- You can `DELETE` the outbox row in the same transaction right after inserting it. The WAL still
  carries the `INSERT`, CDC still sees it, and the table stays empty: no cleanup job, no bloat.
- Or skip the table entirely: `pg_logical_emit_message(true, 'outbox', payload)` writes a
  transactional message straight into the WAL, and the Debezium connector can capture these
  logical decoding messages too.

Risks that polling does not have:

- **A logical slot retains WAL until the consumer confirms it.** If the connector is down for a
  day, the primary keeps a day of WAL and can run out of disk. Set `max_slot_wal_keep_size`
  (Postgres 13+) and alert on slot lag in `pg_replication_slots`.
- **Failover.** Before Postgres 17, logical slots were not synchronized to standbys, so a primary
  failover lost the slot unless your HA tooling recreated it; Postgres 17 added failover support
  for logical slots. Test the failover path either way.
- **The connector is at-least-once by default.** After a restart it may re-emit events since its
  last committed offset. Consumer dedup is still required.

| | Polling relay | CDC relay |
|---|---|---|
| New infrastructure | none (a loop in your service) | Kafka Connect + Debezium; logical replication enabled |
| Latency | ≈ `T_poll` (100 ms – 1 s) | typically sub-second |
| Load on the DB | small queries and updates; table churn | WAL decoding; no table churn |
| Ordering | id order; needs care with several relays (§4.6) | commit order |
| Most likely failure | relay stopped → lag grows (visible in one query) | slot retains WAL → primary disk fills |
| Duplicates | on relay crash | on connector restart |
| Fits | one DB, up to a few thousand events/s, a small team | high volume, many tables, a team that runs Kafka Connect |

For an SMB: start with polling. Move to CDC when relay throughput or table churn becomes a
measurable problem, not before.

### 4.6 Ordering per aggregate

Consumers usually need events for the **same** aggregate in order (`OrderPlaced` before
`OrderCancelled`). Order across aggregates rarely matters. Four traps:

**Trap 1: id order is not commit order.** Identity values are assigned at `INSERT`, not at
`COMMIT`:

```
T1: INSERT outbox → id 100 ...................... (slow) ........ COMMIT at t=50
T2: INSERT outbox → id 101 ...... COMMIT at t=20
relay at t=30 sees 101 only; records "high-water mark = 101"
relay at t=60 queries id > 101 → id 100 is never published                   LOST EVENT
```

Never poll with a high-water mark on `id`. Poll on `published_at IS NULL`, as above, so rows that
commit late are still found.

**Trap 2: per-aggregate commit order.** For a single aggregate, id order *does* match commit order
if every transaction that emits an event for it first locks the aggregate row (the `UPDATE orders
… WHERE id = $1` in §4.3). The second transaction blocks on that lock until the first commits, and
only then inserts its outbox row and receives a larger id. Emitting events without touching the
aggregate row breaks this guarantee.

**Trap 3: several relays reorder.** With `SKIP LOCKED`, relay A locks ids 1–500; relay B skips them
and takes 501–1000; B finishes first. Order 42's event at id 480 (A) is published after its event
at id 620 (B). Fixes:

- **One active relay.** Use a Postgres advisory lock as a leader lease (§6.3). A single relay with
  batching publishes thousands of events per second, which is plenty for most services.
- **Partition relays by key.** Relay `k` of `K` handles only rows with
  `abs(hashtext(aggregate_id)) % K = k` (`hashtext` can return negative values).
- **CDC**, which is ordered by construction.

**Trap 4: the broker.** Use `aggregate_id` as the message key, so all events of one aggregate land
in one Kafka partition. Enable the idempotent producer so internal retries do not reorder or
duplicate ([`07-kafka-and-event-streaming.md` §3.6](07-kafka-and-event-streaming.md#36-idempotent-producers)). Publish a batch in id order and wait for the
acks before marking rows.

Even with all four handled, give consumers the final word: each event carries the aggregate
version `v`, and the consumer ignores anything at or below the version it has already applied
(§5.5). That makes duplicates, replays, and rare reorderings harmless.

### 4.7 Cleanup and partitioning

- **Delete in batches**, keeping a few days of published rows for debugging and replay:

  ```sql
  DELETE FROM outbox
   WHERE id IN (SELECT id FROM outbox
                 WHERE published_at < now() - interval '3 days'
                 LIMIT 5000);
  ```

- **At high volume, partition by time** and drop whole partitions: no delete cost, no vacuum debt.
  On a partitioned table the primary key must include the partition key, for example
  `PRIMARY KEY (id, created_at)` with `PARTITION BY RANGE (created_at)` and one partition per day.
- **With CDC**, delete in the same transaction as the insert (§4.5) and there is nothing to clean.
- **Alert on `L_outbox`**, the age of the oldest unpublished row. It is the single best health
  signal for the whole pattern:

  ```sql
  SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) AS outbox_lag_seconds
    FROM outbox WHERE published_at IS NULL;
  ```

### 4.8 The inbox pattern (the consumer side)

The outbox makes publishing reliable; the inbox makes receiving reliable. There are two forms.

**Form 1: dedup record in the same transaction as the effect** (the common one, detailed in §5.5):

```
receive msg ─► BEGIN
               INSERT INTO processed_messages (consumer, message_id) ON CONFLICT DO NOTHING
               ├─ 0 rows → duplicate: COMMIT, ack
               └─ 1 row  → apply the effect (+ write own outbox rows); COMMIT; ack
```

**Form 2: a full inbox table.** The consumer first stores the raw message
(`INSERT INTO inbox … ON CONFLICT (message_id) DO NOTHING`), acks the broker at once, and a local
worker processes inbox rows later with `SKIP LOCKED`, one aggregate at a time.

```
broker ──► consumer: INSERT inbox row (dedup by message_id); ack ──► worker: process rows
                                                                    (retries, ordering, DLQ
                                                                     under your control)
```

Form 2 helps when processing is slow or flaky: the broker partition does not stall behind one bad
message, and retries, backoff, and dead-lettering happen in your database, where you can query
them. It costs another table and another worker. Start with Form 1.

The full chain across one service boundary is then: **inbox dedup + business change + outbox row,
in one local transaction.** Every hop in the saga of §7 looks like this.

### 4.9 Listen to yourself

An alternative to the outbox when the log is your system of record. The service does **not** write
its database in the request path. It appends the event to the durable log (Kafka), then consumes
its own event to update its database, just like any other consumer.

```
POST /orders ──► publish OrderRequested (key = order id) ──► 202 Accepted
                                   │
                 order service consumes its own topic ──► validate, update its DB, emit follow-ups
```

There is only one write in the request path, so there is no dual write. The costs:

- **No read-your-writes.** The API answers before its own database reflects the change. Clients
  get `202 Accepted` and poll, or the UI shows "processing". (Session guarantees are covered in
  [`04-replication-and-consistency.md`](04-replication-and-consistency.md).)
- **Validation moves to the consumer.** Two requests validated in the handler against the same old
  state can both pass. Validation must happen in the consumer, which processes one aggregate at a
  time because all its events share a partition key.
- It is close to event sourcing, with that approach's schema-evolution and replay concerns.

Use it when the system is already log-centric. For a CRUD service on Postgres, the outbox is
simpler. If the queue itself lives in the same Postgres (a jobs table), you do not need either:
enqueueing is just another insert in the same transaction
([`../solutions/job-scheduler-postgres-deep-dive.md` §20](../solutions/job-scheduler-postgres-deep-dive.md#20-producer-side-transactional-enqueue--outbox)).

### 4.10 What the outbox does and does not guarantee

| Guarantees | Does not guarantee |
|---|---|
| Every committed business change produces its event, eventually | exactly-once publishing (it is at-least-once) |
| No event for a change that rolled back | low latency (bounded by `T_poll` or CDC lag) |
| Per-aggregate order, if §4.6 is followed | order across aggregates |
| Survives relay, broker, and app crashes | anything about what consumers do with the event |

"At-least-once" is the reason the next section exists.

---

## 5. Idempotency in Depth

> **In plain words.** Networks and crashes make repeats unavoidable: clients retry, relays
> republish, brokers redeliver, providers resend webhooks. You cannot stop the repeats, so you make
> them harmless. Each operation carries an id that stays the same across repeats, and the receiver
> records that id in the same transaction as the effect. The second time, it sees the id and does
> nothing (or returns the saved answer).
>
> **Real-world example.** A customer taps "Pay" and the app shows a spinner for 15 seconds, then
> retries. The server sees the same `Idempotency-Key` it saved the first time and returns "order 42
> created" again. One order, one charge.

### 5.1 Definitions and delivery semantics

An operation is **idempotent** if applying it once or many times leaves the same state:
`f(f(x)) = f(x)`. HTTP defines idempotent methods the same way, in terms of the *intended effect
on the server* (RFC 9110 §9.2.2): `PUT`, `DELETE`, and the safe methods (`GET`, `HEAD`, `OPTIONS`,
`TRACE`). The *response* may differ: a second `DELETE` may return `404`. Idempotency keys go one step
further and make the response the same too.

| Semantics | How you get it | What goes wrong | Use it for |
|---|---|---|---|
| **At-most-once** | send once, never retry; or ack / commit the offset *before* processing | loss | metrics samples, "typing…" indicators |
| **At-least-once** | retry until acknowledged; ack *after* processing | duplicates | the default for anything that matters |
| **Exactly-once delivery** | not achievable end to end: a sender cannot tell a lost request from a lost acknowledgement, so it must either risk loss or risk a repeat | — | — |
| **Effectively-once** (exactly-once *processing*) | at-least-once delivery + an idempotent effect, or an atomic commit of effect + "done" marker | needs a dedup key and a place to store it atomically with the effect | payments, stock, ledgers, anything counted |

The formula for the rest of this chapter:

```
effectively-once  =  at-least-once delivery  +  idempotent processing
                                                 └─ the dedup record commits atomically with the effect
```

The last clause is the part that goes wrong in practice. A dedup record stored somewhere else (a
Redis `SETNX` next to a Postgres write) is a dual write, with all the timelines of §1.

### 5.2 Designing naturally idempotent operations

The cheapest idempotency is an operation that is idempotent by construction. The general move is
to turn "do X" (a relative change) into "record the fact that X happened, keyed by a unique id" (an
absolute fact).

| Not idempotent | Idempotent rewrite |
|---|---|
| `UPDATE accounts SET balance = balance - 80` per message | `INSERT INTO ledger (payment_id, amount) … ON CONFLICT (payment_id) DO NOTHING`; adjust the balance only if the insert happened, in the same transaction |
| `POST /orders` with a server-generated id | `PUT /orders/{client-generated uuid}`, or `POST` with an `Idempotency-Key` |
| `stock = stock - 1` on each `ReserveStock` | `INSERT INTO reservations (order_id, sku, qty) ON CONFLICT (order_id, sku) DO NOTHING`, then decrement only if inserted |
| `UPDATE orders SET status = 'SHIPPED'` | `… SET status = 'SHIPPED' WHERE id = $1 AND status = 'PAID'`: a state-machine guard; the repeat updates 0 rows |
| Send a welcome email per `UserCreated` | `INSERT INTO sent_emails (user_id, template) ON CONFLICT DO NOTHING`; send only if inserted (see §5.5 on external side effects) |
| `SET price = 12` (absolute, idempotent) | still needs ordering: `… WHERE version < $v`, or an old message arriving late overwrites a newer value |

Absolute writes (`SET x = 5`) are idempotent but not order-safe. Relative writes (`x = x + 5`)
commute but are not idempotent. Pair absolute writes with a version check, and relative writes with
a unique operation id.

### 5.3 Idempotency keys for HTTP APIs

For operations that create things or move money, the client attaches a key. This design follows
Stripe's public API behavior and Brandur Leach's write-ups on implementing it in Postgres (§11).

```
client                                        API server                               Postgres
  │ POST /checkout                               │                                         │
  │ Idempotency-Key: 5f0c…e1                     │                                         │
  ├─────────────────────────────────────────────►│ INSERT key (IN_PROGRESS, fp) ──────────►│
  │                                              │ … do the work …                         │
  │                                              │ UPDATE key → COMPLETED, 201, body ─────►│ same txn as
  │◄───────────── 201 {"order_id": 42} ──────────┤                                         │ the effects
  │                                              │                                         │
  │  (reply lost; client retries, same key)      │                                         │
  ├─────────────────────────────────────────────►│ key found: COMPLETED, same fp           │
  │◄───────────── 201 {"order_id": 42} ──────────┤ replay stored response, no re-execution │
```

**What the server does on arrival:**

| Stored key state | Fingerprint vs stored | Response |
|---|---|---|
| absent | — | insert `IN_PROGRESS`, execute |
| `COMPLETED` | same | replay the stored status and body; optionally add a header such as `Idempotent-Replayed: true` |
| `COMPLETED` | different | **422** Unprocessable Content: key reused for a different request |
| `IN_PROGRESS`, lease valid | same | **409** Conflict with `Retry-After: 1`: the original is still running |
| `IN_PROGRESS`, lease valid | different | **422** |
| `IN_PROGRESS`, lease expired | same | take over and resume; safe only if the work is resumable (§5.4) |
| header missing on an endpoint that requires it | — | **400** Bad Request |

These status codes match the IETF HTTPAPI working group's draft
*The Idempotency-Key HTTP Header Field* (`draft-ietf-httpapi-idempotency-key-header`): 400 when a
required key is missing, 409 when a request with the same key is still being processed, 422 when a
key is reused with a different payload. The draft specifies the header value as a structured-field
string (quoted: `Idempotency-Key: "8e03978e-…"`), while many existing APIs accept an unquoted
token. Accept both. At the time of writing it was still an Internet-Draft; check its current status
before quoting it as an RFC.

**The design decisions, one by one:**

1. **Scope.** The key is unique per `(tenant or API credential, key)`, never globally. Two
   customers can generate the same key, and a global scope would replay tenant A's response to
   tenant B: a data leak.
2. **Fingerprint.** Store `fp = SHA-256(method, path, canonical body)`, where canonical means
   sorted keys and no insignificant whitespace. A reused key with a different fingerprint is a
   client bug and gets 422. Stripe, for example, compares incoming parameters with those of the
   original request and returns an error if they differ. Clients must not put timestamps or nonces
   in the body of a retry.
3. **What to store.** The status code, the body, and headers that matter (`Location`). Store final
   outcomes: 2xx, and deterministic 4xx such as validation errors or a declined card. For 5xx there
   are two schools. Stripe saves the result of the first request whose execution began, including
   500s, so a client needs a new key after a 500. The alternative is to roll back the key along with
   the failed work, so the client can retry with the same key. Pick one and document it; the second
   is friendlier when the handler is a single local transaction (§5.4 variant A).
4. **In-progress lease.** `T_lease` must exceed the handler's worst case, including downstream
   timeouts: a handler with a 10 s deadline gets a 30–60 s lease. Too short, and a slow original
   and a takeover run at the same time.
5. **TTL.** `TTL_key ≥ H_retry` plus margin. Stripe documents that keys may be removed once they are
   at least 24 hours old. If a mobile app queues offline retries for up to 72 hours, a 24-hour TTL
   lets day-3 retries create duplicates. Delete expired keys in batches using an index on
   `created_at`.
6. **Concurrent duplicates.** The primary key arbitrates. Exactly one `INSERT` wins; the loser reads
   the row and returns 409 (or waits a moment and re-checks). Never check-then-insert without the
   constraint.
7. **Same database as the effects.** The key's completion and the business effects must commit
   together. A Redis-only key store is a dual write.
8. **Where to enforce.** After authentication, because you need the tenant, at the service that owns
   the effect. A per-route dependency or decorator is usually simpler than a generic ASGI middleware
   that has to buffer request and response bodies.
9. **The client contract.** Generate the key once per *user intent* (UUIDv4 or UUIDv7), store it
   with the pending action before the first send, reuse it on every retry of that intent, and create
   a new key only for a new intent (the user edited the cart). Retry only what is retryable (§5.7).

### 5.4 Two server implementations

**Variant A — one local transaction.** Use it when the handler touches only its own database and
is short.

```
BEGIN
  INSERT INTO idempotency_keys (…) ON CONFLICT DO NOTHING
  ├─ conflict → read row → replay / 409 / 422
  └─ inserted → do the work (orders, outbox, saga row)
                UPDATE idempotency_keys SET status = 'completed', response = …
COMMIT
```

A concurrent duplicate's `INSERT … ON CONFLICT DO NOTHING` **blocks** on the unique index until the
first transaction finishes. If the first commits, the duplicate sees the conflict, reads the
completed row (READ COMMITTED takes a fresh snapshot per statement), and replays it. If the first
rolls back, the duplicate's insert proceeds and it does the work itself. A crash rolls back
everything, key included, so the client's retry starts fresh. No leases, no recovery code. Run it
at READ COMMITTED (the Postgres default); at REPEATABLE READ the duplicate gets a serialization
error instead.

**Variant B — lease and recovery points.** Use it when the handler must call something external
(a payment provider) before responding.

```
phase 1  (txn)     insert key IN_PROGRESS with lease; create order PENDING;
                   recovery_point = 'started'                                   COMMIT
phase 2  (no txn)  provider.authorize(…, idempotency_key = f"{key}:authorize")  ← external mutation
phase 3  (txn)     store auth id; recovery_point = 'authorized'                  COMMIT
phase 4  (txn)     insert outbox row; key → COMPLETED with response               COMMIT
```

A retry that arrives after the lease expires resumes from the stored recovery point. Phase 2 is
re-run with the **same derived key**, so the provider deduplicates it. This is Brandur Leach's
"atomic phases" design: every piece of foreign state mutation sits between two local commits and
carries its own idempotency key.

**The better design is often to avoid Variant B.** Move external calls out of the request path
into the saga. `POST /checkout` then only writes local rows (order, saga instance, outbox) and
fits Variant A. That is what the worked example in §7 does.

### 5.5 Consumer-side deduplication

**(a) Processed-message table, same transaction as the effect:**

```sql
CREATE TABLE processed_messages (
    consumer     text        NOT NULL,      -- several consumers can share one DB
    message_id   uuid        NOT NULL,      -- the producer's event_id, not the broker offset
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, message_id)
);
CREATE INDEX ON processed_messages (processed_at);   -- for retention deletes
```

| Crash point | What happens on redelivery |
|---|---|
| before the DB `COMMIT` | nothing was applied; the message is applied once |
| after the DB `COMMIT`, before the offset commit / ack | the insert conflicts → skip → ack |
| after the ack | nothing is redelivered |

Deduplicate on a **producer-assigned id** (the outbox `event_id`), not on the broker offset. A
relay that republishes after a crash produces new offsets for the same event.

Retention: `W_dedup` must cover the longest window in which a duplicate can arrive: broker
retention plus any manual replay or offset reset you might do. With 7-day Kafka retention, keep 7
days, or rely on a version check (below) for anything older.

**(b) Natural idempotency with upserts:**

```sql
INSERT INTO order_view (order_id, status, version)
VALUES ($1, $2, $3)
ON CONFLICT (order_id) DO UPDATE
   SET status = EXCLUDED.status, version = EXCLUDED.version
 WHERE order_view.version < EXCLUDED.version;      -- duplicates and stale events change nothing
```

**(c) Conditional writes and version checks.** Each event carries the aggregate version `v`.
Either apply only if `v = current + 1` (strict: detects gaps, which then need a re-fetch), or only
if `v > current` (last version wins). DynamoDB's `ConditionExpression`, Postgres
`WHERE version = $n`, and etcd's compare-and-swap are all the same tool. It also handles *fencing*:
a stale writer's update is rejected ([`03-consensus-raft-and-distributed-locking.md` §9](03-consensus-raft-and-distributed-locking.md#9-distributed-locking-fundamentals)).

**(d) Side effects outside your database** (email, SMS, a third-party API) cannot commit
atomically with your dedup row. Options, best first:

1. Pass a deterministic idempotency key derived from the message id to the provider, if it
   supports one.
2. Record intent (`sent_emails` row, status `SENDING`), call, record the result. After a crash in
   the middle, you know which sends are in doubt and can check with the provider.
3. Accept a rare duplicate where it is harmless (a second "order shipped" email).

This boundary is where "exactly-once" ends. Say so explicitly in design reviews.

**(e) Poison messages.** Dedup does not help with a message that always fails. Bound the retries,
then move the message to a dead-letter queue with the error attached, and alert. Never ack and drop
silently.

### 5.6 Kafka's idempotent producer and transactions, briefly

- **Idempotent producer** (`enable.idempotence=true`): the broker drops duplicates caused by the
  producer's *own internal retries*, using a producer id plus a per-partition sequence number. It
  does **not** deduplicate across producer sessions: an outbox relay that restarts gets a new
  producer id and republishes rows it had already sent; the broker cannot tell. Consumer dedup
  still matters. Details: [`07-kafka-and-event-streaming.md` §3.6](07-kafka-and-event-streaming.md#36-idempotent-producers).
- **Transactions** (`transactional.id`, epoch-based fencing of zombie producers, `read_committed`
  consumers) make *consume from Kafka → produce to Kafka → commit offsets* atomic. They do not
  cover writes to Postgres or calls to HTTP APIs. For those, use the inbox (§4.8, §5.5). Details:
  [`07-kafka-and-event-streaming.md` §6](07-kafka-and-event-streaming.md#6-exactly-once-semantics) and, for end-to-end exactly-once in stream processors with
  transactional and idempotent sinks, [`22-stream-processing-flink-watermarks-eos.md` §6.3](22-stream-processing-flink-watermarks-eos.md#63-exactly-once-end-to-end).
- Could the outbox relay use Kafka transactions? They would make each batch atomic within Kafka,
  but marking rows published in Postgres is still a separate commit, so a crash still republishes.
  It adds complexity without removing the need for consumer dedup.

### 5.7 Retries and idempotency together

Retries and idempotency are two halves of one design; [`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md)
§2 covers the retry half (budgets, backoff, jitter, classification).

- **Idempotency first, retries second.** Adding retries to a non-idempotent call turns every
  ambiguous timeout into a potential duplicate.
- **The same key on every retry of one intent.** A new key per attempt gives no protection at all.
- **Classify by outcome certainty:**

  | Error | Did the server act? | Retry? |
  |---|---|---|
  | connection refused / DNS failure / TLS handshake failure | no | yes |
  | read timeout, connection reset after sending | **unknown** | only with an idempotency key |
  | `409` (same key in progress) | still running | yes, after `Retry-After` |
  | `422` (key reused with a different body) | no | no: a client bug |
  | `429`, `503` | no (usually) | yes, with backoff, honoring `Retry-After` |
  | `400`, `402` (card declined), `404` | no / business "no" | no |
  | `500` | maybe | only with a key; depends on the 5xx policy chosen in §5.3 |

- **Idempotency makes retries safe, not cheap.** Three layers retrying three times still hit the
  bottom service up to 27 times. Budgets and deadlines still apply
  ([`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md)).
- **The key TTL must outlive every retry path**, including queued retries: a saga's sweeper may
  re-send a command for hours during a provider outage.
- **An open circuit breaker is a "not yet", not a failure.** A saga step blocked by an open breaker
  stays pending and is retried later with the same key.

### 5.8 Idempotency anti-patterns

| Anti-pattern | Why it breaks |
|---|---|
| Check-then-act without a unique constraint | concurrent duplicates both pass the check |
| Dedup record in Redis, effect in Postgres | dual write: "marked done, never applied" or the reverse |
| Deduplicating on broker offset or delivery tag | republished events get new offsets |
| Key TTL shorter than the client's retry horizon | late retries become new operations |
| Fingerprint over non-canonical JSON, or a body containing a timestamp | honest retries get 422 |
| Idempotency key generated server-side | the client cannot mark its retries as retries |
| Globally scoped keys | cross-tenant response replay |
| Compensation implemented as "restore the old value" | lost updates from concurrent sagas |
| Retrying a timed-out call with a fresh key "because the first one probably failed" | the classic double charge |

---

## 6. Python Implementations

> **In plain words.** Four short pieces of code that implement the chapter: an idempotent
> `POST /checkout`, the order-plus-outbox write, the relay, and an idempotent consumer. They use
> FastAPI, psycopg 3 (async), and confluent-kafka. They are deliberately small; production
> additions (metrics, structured logs, graceful shutdown) are listed after each one.
>
> **Real-world example.** These four files, plus the saga transition table in §7, are the whole
> reliability layer of a small shop's checkout. There is no framework to learn and no new
> infrastructure beyond Postgres and a broker.

The `outbox` table is in §4.2, `processed_messages` in §5.5, and `saga_instance` in §3.7. The two
remaining tables:

```sql
CREATE TABLE orders (
    id                uuid        PRIMARY KEY,
    tenant_id         bigint      NOT NULL,
    customer_id       bigint      NOT NULL,
    total_cents       bigint      NOT NULL CHECK (total_cents > 0),
    currency          char(3)     NOT NULL,
    payment_method_id text        NOT NULL,   -- provider token (e.g. 'pm_…'), never a card number
    status            text        NOT NULL,
    version           int         NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
    tenant_id     bigint      NOT NULL,
    key           text        NOT NULL CHECK (length(key) BETWEEN 1 AND 255),
    fingerprint   bytea       NOT NULL,
    status        text        NOT NULL CHECK (status IN ('in_progress', 'completed')),
    locked_until  timestamptz,                -- lease; used only by variant B (§5.4)
    response_code int,
    response_body jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key)
);
CREATE INDEX idempotency_keys_created ON idempotency_keys (created_at);   -- TTL deletes
```

### 6.1 Idempotent `POST /checkout` (variant A)

```python
# app.py — pip install fastapi "psycopg[binary,pool]"
import hashlib
import json
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

pool = AsyncConnectionPool("postgresql://app@db/shop", open=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    yield
    await pool.close()


app = FastAPI(lifespan=lifespan)


class CheckoutIn(BaseModel):
    customer_id: int
    total_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    payment_method_id: str       # token from the provider's browser SDK; card data never reaches us


def fingerprint(method: str, path: str, body: dict) -> bytes:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method}\n{path}\n{canonical}".encode()).digest()


async def current_tenant(request: Request) -> int:
    return request.state.tenant_id          # set by the authentication middleware


@app.post("/checkout")
async def checkout(body: CheckoutIn,
                   tenant_id: int = Depends(current_tenant),
                   idempotency_key: str = Header(min_length=1, max_length=255)):
    key = idempotency_key.strip('"')         # accept the IETF draft's quoted form too
    fp = fingerprint("POST", "/checkout", body.model_dump(mode="json"))

    async with pool.connection() as conn, conn.transaction():
        # A concurrent duplicate blocks on this INSERT until we commit or roll back.
        cur = await conn.execute(
            """INSERT INTO idempotency_keys (tenant_id, key, fingerprint, status)
               VALUES (%s, %s, %s, 'in_progress')
               ON CONFLICT (tenant_id, key) DO NOTHING""",
            (tenant_id, key, fp))
        if cur.rowcount == 0:
            return await replay(conn, tenant_id, key, fp)

        order_id = await create_order(conn, tenant_id, body)          # §6.2, same transaction
        response = {"order_id": str(order_id), "status": "PENDING"}
        await conn.execute(
            """UPDATE idempotency_keys
                  SET status = 'completed', response_code = 201, response_body = %s
                WHERE tenant_id = %s AND key = %s""",
            (Jsonb(response), tenant_id, key))
    return JSONResponse(response, status_code=201)


async def replay(conn, tenant_id: int, key: str, fp: bytes) -> JSONResponse:
    stored_fp, status, code, body = await (await conn.execute(
        """SELECT fingerprint, status, response_code, response_body
             FROM idempotency_keys WHERE tenant_id = %s AND key = %s""",
        (tenant_id, key))).fetchone()
    if stored_fp != fp:
        raise HTTPException(422, "Idempotency-Key was already used with a different request")
    if status != "completed":                # reachable only with leases (variant B)
        raise HTTPException(409, "A request with this Idempotency-Key is in progress",
                            headers={"Retry-After": "1"})
    return JSONResponse(body, status_code=code, headers={"Idempotent-Replayed": "true"})
```

What this gets right, and what is left out:

- Everything, key included, is one transaction. A crash or an exception rolls it all back, so the
  client can retry with the same key (the "roll back on failure" policy from §5.3, item 3).
- A deterministic business rejection (for example, an unknown customer) raised as an exception is
  *not* stored, so a retry re-evaluates it. If you want such 4xx responses replayed, catch the
  domain error, store the 4xx in the key row, and commit.
- Left out: a nightly `DELETE FROM idempotency_keys WHERE created_at < now() - interval '7 days'`
  in batches, metrics on replays and on 409/422 counts (a 422 spike means a client bug), and a
  per-tenant rate limit.

### 6.2 Order + saga + outbox in one transaction

```python
async def create_order(conn, tenant_id: int, body: CheckoutIn) -> uuid.UUID:
    order_id, saga_id = uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        """INSERT INTO orders (id, tenant_id, customer_id, total_cents, currency,
                               payment_method_id, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')""",
        (order_id, tenant_id, body.customer_id, body.total_cents, body.currency,
         body.payment_method_id))
    await conn.execute(
        """INSERT INTO saga_instance (saga_id, saga_type, business_key, state, step, data,
                                      deadline_at)
           VALUES (%s, 'checkout', %s, 'AUTHORIZING', 1, %s, now() + interval '15 minutes')""",
        (saga_id, f"order:{order_id}", Jsonb({"order_id": str(order_id)})))
    # The saga's first command leaves through the outbox: no broker call in the request path.
    # A payment-method token is not card data, and commands have exactly one consumer.
    await conn.execute(
        """INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
           VALUES ('order', %s, 'AuthorizePayment', %s)""",
        (str(order_id), Jsonb({"saga_id": str(saga_id), "step": 1, "order_id": str(order_id),
                               "amount_cents": body.total_cents, "currency": body.currency,
                               "payment_method_id": body.payment_method_id})))
    return order_id
```

Three rows, one commit. If the process dies at any point before `COMMIT`, none of them exist and
the client's retry starts over. After `COMMIT`, the relay guarantees the command goes out, and the
sweeper (§3.7) guarantees it is re-sent if its reply never arrives.

### 6.3 The relay: one active instance, `FOR UPDATE SKIP LOCKED`

```python
# relay.py — pip install "psycopg[binary]" confluent-kafka
import asyncio
import json
import logging

import psycopg
from confluent_kafka import Producer

DSN = "postgresql://relay@db/shop"   # direct connection: session advisory locks do not work
                                     # through a transaction-pooling proxy
RELAY_LOCK_ID = 60_001               # any constant shared by all relay instances
COMMAND_TOPICS = {
    "AuthorizePayment": "payment.commands", "CapturePayment": "payment.commands",
    "VoidAuthorization": "payment.commands", "ReserveStock": "inventory.commands",
    "ReleaseStock": "inventory.commands", "CreateShipment": "shipping.commands",
}
log = logging.getLogger("relay")
producer = Producer({"bootstrap.servers": "kafka:9092",
                     "enable.idempotence": True,        # librdkafka's default is False
                     "acks": "all"})

CLAIM = """
    SELECT id, event_id, aggregate_type, aggregate_id, event_type, payload
      FROM outbox
     WHERE published_at IS NULL
     ORDER BY id
     LIMIT %s
       FOR UPDATE SKIP LOCKED"""


async def relay_batch(conn: psycopg.AsyncConnection, batch_size: int = 500) -> int:
    async with conn.transaction():
        rows = await (await conn.execute(CLAIM, (batch_size,))).fetchall()
        if not rows:
            return 0
        errors = []

        def on_delivery(err, _msg):
            if err is not None:
                errors.append(err)

        for _id, event_id, agg_type, agg_id, ev_type, payload in rows:     # id order
            producer.produce(COMMAND_TOPICS.get(ev_type, f"{agg_type}.events"),
                             key=agg_id, value=json.dumps(payload).encode(),
                             headers={"event_id": str(event_id), "event_type": ev_type},
                             on_delivery=on_delivery)
        unacked = await asyncio.to_thread(producer.flush, 10.0)            # wait for acks
        if errors or unacked:
            # Roll back: the whole batch stays unpublished and is re-sent in order.
            # Rows that did reach the broker become duplicates; consumers drop them (§5.5).
            raise RuntimeError(f"publish failed: {len(errors)} errors, {unacked} unacked")
        await conn.execute("UPDATE outbox SET published_at = now() WHERE id = ANY(%s)",
                           ([r[0] for r in rows],))
        return len(rows)


async def main() -> None:
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as lock_conn:
        while not (await (await lock_conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (RELAY_LOCK_ID,))).fetchone())[0]:
            await asyncio.sleep(5)                  # another instance is the active relay
        log.info("relay lock acquired")
        async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
            while True:
                await lock_conn.execute("SELECT 1")  # raises if the lock session died → exit
                try:
                    sent = await relay_batch(conn)
                except psycopg.OperationalError:
                    raise                            # connection lost: let the supervisor restart us
                except Exception:
                    log.exception("relay batch failed; retrying")
                    sent = 0
                    await asyncio.sleep(1)
                if sent == 0:
                    await asyncio.sleep(0.2)         # T_poll


if __name__ == "__main__":
    asyncio.run(main())
```

Notes:

- The advisory lock is held by the `lock_conn` session. If the process dies, the connection drops
  and another instance takes over within its 5-second retry. There is a narrow window in which a
  network-partitioned old relay and a new one both publish; that produces duplicates and possible
  reordering, which the consumers' dedup and version checks absorb (§4.6).
- With a single active relay, `SKIP LOCKED` is not strictly needed. It keeps a second instance, a
  manual run, or a future partitioned setup from blocking.
- Production additions: export `outbox_lag_seconds` (§4.7) and publish error counts, a batch
  cleanup job, graceful shutdown on SIGTERM (finish the batch, then exit).

### 6.4 An idempotent consumer: inbox dedup + effect + reply in one transaction

The inventory service handles `ReserveStock` and `ReleaseStock`. Tables it owns: `stock(sku,
available)`, `reservations(order_id, sku, qty, expires_at, PRIMARY KEY (order_id, sku))`,
`reservation_tombstones(order_id PRIMARY KEY)`, plus its own `outbox` and `processed_messages`.

```python
# inventory_consumer.py
import asyncio
import json

import psycopg
from confluent_kafka import Consumer
from psycopg.types.json import Jsonb

DSN = "postgresql://inventory@db/inventory"
consumer = Consumer({"bootstrap.servers": "kafka:9092", "group.id": "inventory",
                     "enable.auto.commit": False,       # commit offsets only after the DB commit
                     "auto.offset.reset": "earliest"})
consumer.subscribe(["inventory.commands"])


class OutOfStock(Exception):
    pass


async def try_reserve(conn, order_id: str, items: list[dict]) -> bool:
    if await (await conn.execute("SELECT 1 FROM reservation_tombstones WHERE order_id = %s",
                                 (order_id,))).fetchone():
        return False                                    # released already: a late Reserve is a no-op
    try:
        async with conn.transaction():                  # savepoint: all items or none
            for it in sorted(items, key=lambda i: i["sku"]):    # fixed lock order: no deadlocks
                cur = await conn.execute(
                    """UPDATE stock SET available = available - %s
                        WHERE sku = %s AND available >= %s""",
                    (it["qty"], it["sku"], it["qty"]))
                if cur.rowcount == 0:
                    raise OutOfStock(it["sku"])
                await conn.execute(
                    """INSERT INTO reservations (order_id, sku, qty, expires_at)
                       VALUES (%s, %s, %s, now() + interval '24 hours')""",
                    (order_id, it["sku"], it["qty"]))
        return True
    except OutOfStock:
        return False


async def release(conn, order_id: str) -> None:
    # Idempotent and commutative: only rows actually deleted are added back.
    await conn.execute(
        """WITH released AS (DELETE FROM reservations WHERE order_id = %s RETURNING sku, qty)
           UPDATE stock s SET available = s.available + r.qty
             FROM released r WHERE s.sku = r.sku""", (order_id,))
    await conn.execute("INSERT INTO reservation_tombstones (order_id) VALUES (%s) "
                       "ON CONFLICT DO NOTHING", (order_id,))


async def handle(conn, msg) -> None:
    headers = {k: v.decode() for k, v in (msg.headers() or [])}
    cmd = json.loads(msg.value())
    async with conn.transaction():
        cur = await conn.execute(
            """INSERT INTO processed_messages (consumer, message_id)
               VALUES ('inventory', %s) ON CONFLICT DO NOTHING""", (headers["event_id"],))
        if cur.rowcount == 0:
            return                                      # duplicate: effect and reply already committed
        if headers["event_type"] == "ReserveStock":
            ok = await try_reserve(conn, cmd["order_id"], cmd["items"])
            reply = "StockReserved" if ok else "StockUnavailable"
        elif headers["event_type"] == "ReleaseStock":
            await release(conn, cmd["order_id"])
            reply = "StockReleased"
        else:
            return
        await conn.execute(                             # the reply leaves through our own outbox
            """INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
               VALUES ('inventory', %s, %s, %s)""",
            (cmd["order_id"], reply, Jsonb({"saga_id": cmd["saga_id"], "step": cmd["step"],
                                            "order_id": cmd["order_id"]})))


async def main() -> None:
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as conn:
        while True:
            msg = await asyncio.to_thread(consumer.poll, 1.0)
            if msg is None or msg.error():
                continue
            await handle(conn, msg)   # an exception exits before the offset commit → redelivered
            await asyncio.to_thread(consumer.commit, message=msg, asynchronous=False)


if __name__ == "__main__":
    asyncio.run(main())
```

What makes it correct:

- **Dedup row, stock change, and reply commit together.** A crash before the commit applies
  nothing; a crash after it makes the redelivery a no-op. The reply is not lost either, because it
  sits in the same transaction's outbox.
- **Two layers of idempotency.** Even without `processed_messages`, the `reservations` primary key
  would reject a double reservation, and `release` only adds back what it deleted.
- **Tombstones** make a `ReserveStock` that arrives after its `ReleaseStock` harmless (§3.3, rule 4).
- **A sweeper for `expires_at`** (not shown) releases reservations of sagas that vanished, the
  `TTL_res` safety net from §3.8.
- **Left out on purpose:** a retry counter with a dead-letter topic for poison messages (§5.5 e),
  and committing offsets in batches for throughput. Committing once per message is simple and
  correct; batching the commit is safe too, because a redelivered message is a dedup hit.

---

## 7. Worked Example — SMB E-Commerce Checkout

> **In plain words.** One checkout, end to end: the order service accepts the request idempotently,
> an orchestrated saga drives payment, inventory and shipping through outbox commands, every
> participant deduplicates, and three realistic failures are walked through step by step.
>
> **Real-world example.** The shop from "Start here": about 3,000 orders a day, peak 5 checkouts/s,
> one Postgres per service, Kafka as the broker, a card payment provider with idempotency keys and
> webhooks. (Illustrative numbers.) At 0.2% ambiguous payment calls, about 6 checkouts a day hit
> failure 1 (§7.4); all of them resolve without a human.

### 7.1 Architecture

```
            POST /checkout (Idempotency-Key)
 client ───────────────────────────────────► ┌──────────────────────────────────────────┐
        ◄──── 201 {order_id, PENDING} ────── │ Order service  (Postgres)                │
        GET /orders/{id} (poll or push)      │  orders · idempotency_keys · saga_instance│
                                             │  outbox · processed_messages              │
                                             │  API · orchestrator · relay · sweeper     │
                                             └───────┬──────────────────────▲───────────┘
                          payment.commands            │ inventory.commands   │ payment.events
                          shipping.commands           ▼                      │ inventory.events
                                        ┌──────────── Kafka ─────────────────┴──────────┐
                                        └────┬─────────────────┬──────────────────┬─────┘
                                             ▼                 ▼                  ▼
                               ┌─────────────────────┐ ┌───────────────┐ ┌───────────────┐
 card provider ◄── HTTPS ────  │ Payment service     │ │ Inventory svc │ │ Shipping svc  │
 (idempotency keys) ── webhooks►│ payments · outbox · │ │ stock · resv. │ │ shipments ·   │
                               │ processed_webhooks  │ │ outbox · inbox│ │ outbox · inbox│
                               └─────────────────────┘ └───────────────┘ └───────────────┘
```

Every arrow into a service is consumed with inbox dedup; every arrow out of a service leaves
through that service's outbox. Card data goes from the browser straight to the provider's hosted
fields or SDK; our services only ever see a payment-method token. Never store or log card numbers
(PAN) or CVV; tokenizing through the provider keeps the system in the smallest PCI DSS scope.

### 7.2 The saga definition

| # | State | Participant | Command | Success reply → next | Business failure → | Type | Downstream idempotency |
|---|---|---|---|---|---|---|---|
| 1 | `AUTHORIZING` | Payment → provider | `AuthorizePayment` | `PaymentAuthorized` → `RESERVING` | `PaymentDeclined` → `REJECTED` | compensatable (void) | provider key `order:{id}:authorize` |
| 2 | `RESERVING` | Inventory | `ReserveStock` | `StockReserved` → `CAPTURING` | `StockUnavailable` → `VOIDING` | compensatable (release) | `reservations` PK `(order_id, sku)` |
| 3 | `CAPTURING` | Payment → provider | `CapturePayment` | `PaymentCaptured` → `SHIPPING` | `CaptureFailed` → `RELEASING`, then `VOIDING` | **pivot** | provider key `order:{id}:capture` |
| 4 | `SHIPPING` | Shipping | `CreateShipment` | `ShipmentCreated` → `DONE` | not allowed: retry, then escalate | retriable | `shipments` unique `order_id` |
| C | `RELEASING` | Inventory | `ReleaseStock` | `StockReleased` | must not fail: retry, alert | compensation | delete-returning (§6.4) |
| C | `VOIDING` | Payment → provider | `VoidAuthorization` | `AuthorizationVoided` → `REJECTED` | must not fail: retry, alert | compensation | provider key `order:{id}:void` |

Why authorize before reserving: card declines are the most common failure, and checking them first
avoids locking stock for customers who cannot pay. (Reserving first is also defensible if stock-outs
are more common than declines; either way, both steps come before the pivot.) Why capture is the
pivot: before it, undoing costs a void; after it, undoing costs a refund, which is a new money
movement visible to the customer. The `order` row mirrors the saga: `PENDING` → `CONFIRMED` or
`REJECTED`. `PENDING` is the semantic lock (§3.9): while an order is `PENDING`, a customer cancel
does not edit the row; it asks the orchestrator to compensate, which is possible only before the
pivot.

The orchestrator's reply handler, called inside the inbox-dedup transaction of §6.4:

```python
TRANSITIONS = {   # (state, reply) -> (next state, next command or None)
    ("AUTHORIZING", "PaymentAuthorized"): ("RESERVING", "ReserveStock"),
    ("AUTHORIZING", "PaymentDeclined"):   ("REJECTED", None),
    ("RESERVING", "StockReserved"):       ("CAPTURING", "CapturePayment"),
    ("RESERVING", "StockUnavailable"):    ("VOIDING", "VoidAuthorization"),
    ("CAPTURING", "PaymentCaptured"):     ("SHIPPING", "CreateShipment"),
    ("CAPTURING", "CaptureFailed"):       ("RELEASING", "ReleaseStock"),
    ("RELEASING", "StockReleased"):       ("VOIDING", "VoidAuthorization"),
    ("VOIDING", "AuthorizationVoided"):   ("REJECTED", None),
    ("SHIPPING", "ShipmentCreated"):      ("DONE", None),
}


async def on_reply(conn, reply_type: str, reply: dict) -> None:
    saga = await (await conn.execute(
        "SELECT state, step, data FROM saga_instance WHERE saga_id = %s FOR UPDATE",
        (reply["saga_id"],))).fetchone()
    if saga is None or reply["step"] != saga[1] or (saga[0], reply_type) not in TRANSITIONS:
        return                                    # stale, duplicate, or late: ignore (and log)
    next_state, command = TRANSITIONS[(saga[0], reply_type)]
    data = {**saga[2], **reply.get("data", {})}   # e.g. keep the authorization id
    await conn.execute(
        """UPDATE saga_instance
              SET state = %s, step = step + 1, data = %s, attempts = 0,
                  next_attempt_at = now() + interval '30 seconds', updated_at = now()
            WHERE saga_id = %s""",
        (next_state, Jsonb(data), reply["saga_id"]))
    if command:                                   # same transaction as the state change
        # insert_outbox builds the command payload from `data` and inserts it, as in §6.2
        await insert_outbox(conn, command, saga_id=reply["saga_id"], step=saga[1] + 1, data=data)
```

The sweeper re-sends the current command for any saga whose `next_attempt_at` has passed, with the
same `(saga_id, step)` and therefore the same downstream idempotency key, and compensates any saga
past `deadline_at` that has not reached the pivot.

### 7.3 Happy path

```
Client     Order svc (API + orchestrator)          Payment svc          Provider        Inventory   Shipping
  │ POST /checkout ─►│ txn: key, order PENDING, saga AUTHORIZING,
  │                  │      outbox AuthorizePayment
  │◄─ 201 order 42 ──│
  │                  │ relay ─ AuthorizePayment ───►│ authorize(key=order:42:authorize) ─►│
  │                  │                              │◄──────────── auth_123 ──────────────│
  │                  │◄──── PaymentAuthorized ──────│ (txn: payment AUTHORIZED + outbox)
  │                  │ txn: saga RESERVING + outbox ReserveStock
  │                  │ ──────────────── ReserveStock ────────────────────────────────────►│
  │                  │◄─────────────── StockReserved ─────────────────────────────────────│
  │                  │ txn: saga CAPTURING + outbox CapturePayment
  │                  │ ─ CapturePayment ───────────►│ capture(key=order:42:capture) ─────►│
  │                  │◄──── PaymentCaptured ────────│
  │                  │ txn: saga SHIPPING + outbox CreateShipment
  │                  │ ─────────────────────────── CreateShipment ─────────────────────────────────────►│
  │                  │◄────────────────────────── ShipmentCreated ──────────────────────────────────────│
  │                  │ txn: saga DONE, order CONFIRMED + outbox OrderConfirmed (→ email, analytics)
```

Nine local transactions and nine messages, typically finishing in a second or two. Every
transaction is local to one database, and every message can be duplicated without harm.

### 7.4 Failure 1 — the payment succeeds but the response is lost

```
t=0.0 s   Payment svc → provider: POST /authorize  (Idempotency-Key: order:42:authorize)
t=0.4 s   provider creates auth_123 and replies
t=0.4 s   ✗ connection reset; the reply never arrives
t=10 s    Payment svc: read timeout → outcome UNKNOWN
```

What the payment service must **not** do is reply `PaymentDeclined`. The orchestrator would reject
the order while the customer's card carries an $80 hold: the customer sees "payment failed" and a
pending charge on their banking app.

What it does instead:

1. Its `payments` row for order 42 is `AUTHORIZING` (written before the call). The handler raises;
   the command is not acked and is redelivered, or the handler retries internally with backoff.
2. The retry sends the **same** idempotency key. The provider recognizes it and returns the stored
   result: `auth_123`. The payment service records it and emits `PaymentAuthorized` through its
   outbox. The customer sees nothing unusual; checkout took 12 s instead of 1 s.
3. If the provider did not support idempotency keys, step 2 would be "look up by our reference":
   every authorization carries `order_id` as metadata, and the service searches for it before
   authorizing again.
4. If the provider stays unreachable for longer than `T_saga` (15 min), the orchestrator's sweeper
   moves the saga to `VOIDING`. `VoidAuthorization` for order 42 means "void whatever authorization
   exists for order 42, and record that order 42 is voided". If `auth_123` surfaces later (a late
   reply or a webhook), the payment service sees the `VOIDED` state and voids it immediately: the
   tombstone rule of §3.3.
5. Meanwhile the provider's webhook for `auth_123` may arrive before, after, or instead of the
   retry. Both paths converge on the same guarded state transition (failure 3).

### 7.5 Failure 2 — the inventory compensation fails

```
t=0       CapturePayment → provider says the authorization has expired → CaptureFailed
t=0       orchestrator: saga → RELEASING; outbox ReleaseStock
t=0..40m  inventory service is crash-looping after a bad migration; ReleaseStock is never acked
```

What happens:

- Compensations run in reverse order, so `VoidAuthorization` waits behind the release. Here that
  costs nothing, because an expired authorization holds no funds. In general, independent
  compensations (release and void are independent) can be issued in parallel so one broken
  participant does not delay the other; that needs a join in the state machine (wait for both
  replies), which this minimal version leaves out.
- `ReleaseStock` stays in Kafka, unacked, and the sweeper re-sends it every few minutes with the
  same `(saga_id, step)`. When inventory recovers at t=40 min, it processes the command (and its
  duplicates) exactly once: dedup row plus delete-returning release (§6.4).
- The saga sits in `RELEASING` for 40 minutes. An alert on "any saga in a compensating state for
  more than 15 minutes" fires at t=15 min, pointing at the inventory service, not at checkout.
- If the saga itself were lost (a bug, a deleted row), the reservation's 24 h `expires_at` would
  still release the stock.
- If a compensation can *never* succeed (the SKU was deleted from the catalog), retries will not
  help. After N attempts the saga moves to `NEEDS_ATTENTION`, a human-operated state with a runbook
  and "retry" and "mark resolved" actions in an admin tool.

What would have gone wrong with shortcuts:

| Shortcut | Consequence |
|---|---|
| Mark the saga `FAILED` after 3 attempts and move on | 2 units stay reserved forever: phantom "out of stock" while the shelf is full |
| Release by `SET available = <value read at reservation time>` | overwrites every other order's reservation made in the 40 minutes: oversell |
| Non-idempotent release (`available += qty` per message) | every duplicate adds stock back again: oversell |

### 7.6 Failure 3 — a duplicate webhook from the payment provider

Payment providers deliver webhooks at least once and do not promise order. Stripe's documentation,
for example, says an endpoint may receive the same event more than once and that events are not
guaranteed to arrive in the order they were generated.

```
t=0.4 s  sync reply:  auth_123 → payment row AUTHORIZED, outbox PaymentAuthorized
t=0.9 s  webhook #1:  event evt_A "authorization succeeded", auth_123
t=31 s   webhook #2:  evt_A again (provider did not see our 200 in time)
```

The webhook handler, in the payment service:

1. **Verify the signature** (HMAC over the raw body with the endpoint secret) and the timestamp
   tolerance before parsing anything. Reject unsigned or stale requests.
2. In **one transaction**:
   `INSERT INTO processed_webhooks (provider, event_id) … ON CONFLICT DO NOTHING`. Zero rows means
   a duplicate: commit and return **200**. Returning 4xx or 5xx for a duplicate makes the provider
   retry it for days.
3. Apply a **guarded transition**:

   ```sql
   UPDATE payments SET status = 'AUTHORIZED', auth_id = $2, version = version + 1
    WHERE order_id = $1 AND status IN ('AUTHORIZING', 'UNKNOWN')
   RETURNING version;
   ```

   Here the synchronous path already moved the row to `AUTHORIZED`, so this updates 0 rows. Only
   the path that actually changes the row inserts `PaymentAuthorized` into the outbox, so the
   orchestrator receives exactly one `PaymentAuthorized` per payment, even though the provider
   reported it three times (sync reply plus two webhooks). The orchestrator would ignore a second
   one anyway (wrong step, §7.2): defense in depth.
4. **Out-of-order events** (a "refunded" event arriving before "succeeded"): the state machine
   refuses backward transitions. When the local state and the event disagree, re-read the object
   from the provider's API and apply that (the *reread value* countermeasure, §3.9).
5. **Respond fast.** Do the minimum in the webhook request (verify, dedup, transition, outbox) and
   leave the rest to consumers. Providers time out slow endpoints and retry, which creates more
   duplicates.

### 7.7 The other failures, briefly

| Failure | What handles it |
|---|---|
| Customer double-clicks "Pay" | same `Idempotency-Key` → the second request blocks, then replays 201 (§6.1) |
| API process dies after `COMMIT`, before responding | client retries with the same key → replay |
| Relay dies after publishing, before marking rows | rows republished → consumers' dedup drops them |
| Orchestrator dies between two steps | state and command were committed together; the sweeper re-sends |
| `ReserveStock` delivered twice | `processed_messages` hit; the `reservations` PK as backup |
| Consumer-group rebalance mid-batch | uncommitted offsets redelivered → dedup |
| Provider down for 1 hour | breaker opens (ch. 33); steps stay pending; past `T_saga` the saga compensates |
| Customer cancels while `PENDING` | cancel asks the orchestrator to compensate; it never edits the order row directly |

### 7.8 What to monitor

| Signal | Why | Alert when (illustrative) |
|---|---|---|
| `outbox_lag_seconds` per service | relay stopped or broker down | > 60 s |
| Sagas by state and age | stuck steps, broken participants | any saga > 3 × p99 duration in one state; any compensating saga > 15 min |
| Compensation rate | payment or stock problems, fraud | 3× the weekly baseline |
| Idempotency replays, 409s, 422s | client retry health; 422 = client bug | any sustained 422s |
| Dedup hit rate per consumer | redelivery storms, relay loops | sudden jump |
| DLQ depth | poison messages | > 0 |
| Webhook signature failures | misconfiguration or probing | > 0 sustained |
| Replication slot lag (CDC only) | WAL retention risk | > 10% of `max_slot_wal_keep_size` |

Tracing one order across these services, and finding the cause of lag or a stuck saga, is covered
in [`37-distributed-systems-debugging.md`](37-distributed-systems-debugging.md).

---

## 8. Decision Guide — Which Pattern for Which Situation

| Situation | Use | Avoid |
|---|---|---|
| Write a DB row and publish an event from one service | Transactional outbox, polling relay to start (§4) | publish after commit; after-commit hooks |
| Write a DB row and enqueue a job, queue in the same Postgres | Transactional enqueue: same transaction | an external queue plus a dual write |
| Consume an event and update your own DB | Inbox dedup in the same transaction, or upsert with a version check (§5.5) | dedup in Redis next to a DB write |
| Consume from Kafka and produce to Kafka only | Kafka transactions ([`07-kafka-and-event-streaming.md` §6](07-kafka-and-event-streaming.md#6-exactly-once-semantics)) | hand-rolled dedup |
| Public `POST` that creates resources or moves money | Idempotency keys, Stripe-style (§5.3) | hoping clients do not retry |
| Call a third-party API that mutates state | Provider idempotency key derived from your id; resolve "unknown" by retry or lookup (§7.4) | retries without a key; treating timeout as failure |
| 2–3 services react to a fact; nothing can fail the business process | Choreography | an orchestrator with no decisions to make |
| Multi-service process with compensations, timeouts, 4+ steps | Orchestrated saga: state table + outbox + sweeper (§3.7) | choreography across a long chain |
| Long-running (hours to days), human steps, many workflow types | Workflow engine such as Temporal (§3.11) | a growing pile of cron sweepers |
| Money or stock that must not be double-spent mid-process | TCC, or saga steps with semantic locks and reservations | direct decrements with "restore" compensations |
| Atomic update across rows or shards inside one datastore | that datastore's transaction (Spanner and CockroachDB run 2PC internally) | an application-level saga |
| Atomic across two databases you own, same vendor, short transactions, low volume | XA/2PC with a real transaction manager and recovery (§2.3) | XA involving brokers or HTTP APIs |
| The log is already your system of record | Listen to yourself (§4.9) | an outbox on top of an event store |
| Concurrent writes to the same entity in several regions | conflict resolution, CRDTs: [`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md) | using sagas to resolve write conflicts |

---

## 9. Production Pitfalls / War Stories

> **In plain words.** These patterns rarely fail in their logic. They fail at the edges: a relay
> nobody watches, a key that expires too soon, a dedup record kept in the wrong place, two relays
> where there should be one.
>
> **Real-world example.** An outbox relay stops after a config change. Nothing errors, because
> nothing is trying to publish. The only symptom is a number, the outbox lag, that nobody graphs.

These are **composite scenarios** built from the failure modes in this chapter. The numbers are
illustrative, not measurements from a specific company.

**9.1 The relay that stopped quietly.** A deploy manifest change scaled the relay to zero replicas.
Orders kept committing, events piled up in the outbox, and the warehouse saw nothing for 6 hours,
until customers asked where their parcels were. *Fix:* alert on `outbox_lag_seconds > 60`, and
run the relay as part of the service's own health, not as an unowned sidecar. *Lesson:* the outbox
turns outages into lag, and lag is only visible if you measure it.

**9.2 The replication slot that filled the disk.** A Debezium connector was paused over a weekend
for a Kafka Connect upgrade. Its logical slot kept every WAL segment since Friday; on Sunday the
primary's disk filled and all writes stopped. *Fix:* `max_slot_wal_keep_size`, an alert on slot
lag, and a runbook for dropping and re-snapshotting an abandoned slot. *Lesson:* CDC moves the
failure mode from "events late" to "primary down"; choose it knowingly.

**9.3 The 24-hour key and the offline phone.** The mobile app queued failed checkouts and retried
them when connectivity returned, sometimes 2–3 days later. Keys had a 24-hour TTL, so late retries
created new orders. *Fix:* `TTL_key` ≥ the client's retry horizon, and the client stops
auto-retrying a payment after 24 hours and asks the user instead. *Lesson:* the TTL is a contract
with the client's retry policy.

**9.4 The 422 storm after an SDK upgrade.** A new client SDK added `client_sent_at` to request
bodies. Every retry now had a different fingerprint and got 422, so retries that used to succeed
became hard failures at checkout. *Fix:* fingerprint only business fields, reject volatile fields in
idempotent bodies at review time, and alert on 422 rate. *Lesson:* a 422 is a client bug, and a
spike means a release broke retry safety.

**9.5 Dedup in Redis, effect in Postgres.** A consumer ran `SET message_id NX EX 86400` in Redis,
then wrote to Postgres. When the Postgres write failed (a deadlock), the retry found the id in Redis
and skipped the message, so the update was lost. *Fix:* move the dedup record into the same
Postgres transaction. *Lesson:* a dedup record must commit atomically with the effect it guards.

**9.6 Three relays and the cancelled order that shipped.** To speed up a backlog, the relay was
scaled to three replicas with `SKIP LOCKED`. For a few orders, `OrderCancelled` reached shipping
before `OrderPlaced`; shipping ignored the cancel of an unknown order, then shipped the order.
*Fix:* back to one active relay (advisory lock), per-aggregate version checks in consumers, and a
tombstone when a cancel arrives for an unknown order. *Lesson:* scaling a relay is an ordering
decision, not only a throughput one.

**9.7 Orphaned prepared transactions.** A service used an embedded XA transaction manager whose
recovery log lived on the container's filesystem. A node drain rescheduled the container mid-commit;
the new container had no log, and two `PREPARE TRANSACTION`s stayed in `pg_prepared_xacts` holding
row locks on hot inventory rows. VACUUM fell behind for days. *Fix:* monitor `pg_prepared_xacts`
age, put the TM log on durable storage, and in this case remove XA in favor of an outbox. *Lesson:*
2PC's blocking window is not theoretical.

**9.8 Timeout treated as a decline.** A payment adapter mapped every exception, including read
timeouts, to `PaymentDeclined`. About 0.2% of checkouts were rejected while the card held an
authorization, and each became a support ticket. *Fix:* a separate `UNKNOWN` outcome resolved by
retrying with the same key or looking up by reference (§7.4). *Lesson:* model three outcomes, not
two.

Smaller gotchas worth one line each:

- Session advisory locks through PgBouncer in transaction-pooling mode are meaningless: the lock
  stays on a server connection that other clients reuse. The relay needs a direct connection.
- An outbox write that forgets to lock the aggregate row loses the per-aggregate ordering guarantee
  (§4.6, trap 2).
- `processed_messages` without a retention job grows forever. Delete by `processed_at` in batches.
- Webhook handlers that return 500 on duplicates get the same event retried for days.

---

## 10. Interview Questions

> **In plain words.** Most questions in this area are the same question in different clothes: "what
> happens if it crashes right here, or the reply is lost right here?" Answer with a timeline, name
> the guarantee (at-least-once, effectively-once), and say where the dedup record lives.
>
> **Real-world example.** "How do you publish an event when an order is created?" A strong answer:
> "Outbox row in the same transaction, a relay publishes at-least-once, consumers dedup on the
> event id in the same transaction as their write, and I alert on outbox lag."

**Q1. What is the dual-write problem?** Writing to two systems (DB and broker, or DB and an API)
without a shared commit. Crashes, lost replies, and races produce lost events, ghost events,
duplicates, and reordering (§1). You cannot fix it with careful code, only by reducing it to one
commit plus idempotent downstream processing.

**Q2. Why not 2PC between microservices? When is it fine?** Participants must all speak XA (brokers
and HTTP APIs do not), locks are held across network round trips, availability is the product of
all participants, and a coordinator crash blocks in-doubt participants. It is fine inside a
distributed database (Spanner, CockroachDB), where the coordinator's state is replicated by
consensus, and occasionally between two same-vendor databases with a proper transaction manager.

**Q3. Explain the transactional outbox and its guarantee.** Write the event row in the same local
transaction as the business change; a relay publishes committed rows and marks them sent. The
guarantee is at-least-once publication with per-aggregate order (if done right) and latency of up
to one poll interval. The catch: duplicates on relay crash, so consumers must be idempotent.

**Q4. Polling relay or CDC?** Polling needs no new infrastructure and fails visibly (lag grows);
it is right for most services. CDC gives commit-order events without polling load, but needs Kafka
Connect/Debezium and a logical slot, whose failure mode is WAL retention filling the primary's disk.

**Q5. How do you keep per-aggregate order?** Lock the aggregate row before inserting the outbox row
(so ids follow commit order per aggregate), poll on `published_at IS NULL` rather than an id
high-water mark, run one active relay (or partition relays by key, or use CDC), key messages by
aggregate id, and have consumers ignore versions they have already applied.

**Q6. Is exactly-once delivery possible?** Not end to end: a sender cannot distinguish a lost
request from a lost acknowledgement. What you build is effectively-once processing: at-least-once
delivery plus an idempotent effect, with the dedup record committed atomically with the effect.

**Q7. Design idempotency keys for a payments API.** Client-generated key per intent; unique per
(tenant, key); store a fingerprint of method, path, and canonical body; store the final status and
body; `IN_PROGRESS` with a lease for concurrent duplicates (409), 422 on fingerprint mismatch, 400
when missing; TTL at least the client's retry horizon; key and effects in the same database and,
ideally, the same transaction.

**Q8. Two requests with the same key arrive at the same moment. What happens?** The unique
constraint picks one winner. In the single-transaction design, the loser's insert blocks until the
winner commits, then replays the stored response (or proceeds if the winner rolled back). In the
lease design, the loser gets 409 with `Retry-After`.

**Q9. Orchestration or choreography?** Choreography for short, stable flows where services react
to facts and nothing needs compensating. Orchestration once there are compensations, timeouts,
branches, or more than three or four participants, because the flow lives in one inspectable state
machine. Common split: orchestrate the transaction, choreograph the side effects.

**Q10. What is a pivot transaction? Where does "capture payment" go?** The go/no-go step: before it,
failures are compensated; after it, steps only retry forward. Capture is a good pivot: split payment
into authorize (compensatable by void) and capture, put failure-prone steps (decline, stock check)
before it, and irreversible ones (shipping, email) after.

**Q11. Give a saga isolation anomaly and a countermeasure.** Lost update: saga A compensates a
stock reservation by restoring the old count, erasing saga B's reservation. Countermeasures:
commutative deltas (`available += n`), reservations as rows, semantic locks (`PENDING` states),
and version checks before overwriting.

**Q12. A payment call times out. What do you do?** Treat it as unknown, not failed. Retry with the
same idempotency key, or look the payment up by your reference. Compensate only after confirming
it failed, or after making a late success harmless (a tombstone that voids it on arrival). Never
retry with a new key.

**Q13. How should a consumer deduplicate, and why not in Redis or by offset?** Insert the
producer's event id into a processed-messages table in the same transaction as the effect; skip on
conflict. Redis is a separate commit (a dual write). Offsets change when an event is republished
(relay restart), so they do not identify the event.

**Q14. Kafka has idempotent producers and transactions. Does that give exactly-once into
Postgres?** No. The idempotent producer removes duplicates from its own retries within one
session; transactions make consume-produce-commit atomic within Kafka. A write to Postgres or an
HTTP call is outside both, so it still needs inbox dedup or an idempotent upsert.

**Q15. A compensation keeps failing. Now what?** Compensations must be idempotent and retried until
they succeed, with an alert on saga age. Reservations carry a safety-net expiry. If success is
impossible, the saga moves to a human-handled state with a runbook. Never silently mark it failed.

**Q16. TCC versus saga?** TCC splits every step into try (reserve), confirm, and cancel. Reserved
resources are invisible to others, so it gives better isolation, at the cost of three idempotent
operations per participant plus handling empty cancels and tries that arrive after their cancel.
Card authorize/capture/void is TCC.

**Q17. When would you adopt Temporal instead of a hand-rolled saga?** Many or evolving workflow
types, waits of hours or days, human signals, or a need for per-instance history and replay.
Activities are still at-least-once, so they must still be idempotent.

---

## 11. Real-World Cases

> **In plain words.** Public write-ups from companies and authors who built these patterns. Read the
> originals; the summaries below stick to what they document.
>
> **Real-world example.** Stripe's public API documentation is the de facto reference for how an
> idempotency-key API should behave from the client's side.

- **Stripe, idempotent requests (API documentation) and "Designing robust and predictable APIs
  with idempotency" (Stripe blog, Brandur Leach, 2017).** Client-generated keys of up to 255
  characters, sent in an `Idempotency-Key` header; the first result is saved and replayed for later
  requests with the same key, including server errors; parameters of a reused key are compared with
  the original request and a mismatch is an error; keys may be pruned once at least 24 hours old.
  The blog post pairs idempotency with exponential backoff and jitter on the client.
- **Brandur Leach, "Implementing Stripe-like Idempotency Keys in Postgres" (2017).** The source of
  the atomic-phases design in §5.4: an `idempotency_keys` table with a lock timestamp and a recovery
  point; every foreign state mutation (a call to another service) sits between two local
  transactions; background processes finish abandoned requests and delete old keys.
- **Stripe webhooks documentation.** Endpoints may receive the same event more than once, and
  delivery order is not guaranteed; handlers should record processed event ids and verify
  signatures. The basis of §7.6.
- **IETF HTTPAPI working group, `draft-ietf-httpapi-idempotency-key-header`.** Standardizes the
  `Idempotency-Key` request header and the 400/409/422 error semantics used in §5.3.
- **Amazon Builders' Library, "Making retries safe with idempotent APIs" (Malcolm Featonby).**
  Client request tokens, returning a semantically equivalent response to a repeated request, and
  rejecting a repeated token that comes with different parameters. EC2's `RunInstances`
  `ClientToken` and its `IdempotentParameterMismatch` error are a public example of the same design.
- **Airbnb Engineering, "Avoiding Double Payments in a Distributed Payments System" (2019).**
  Describes Orpheus, Airbnb's idempotency library for payments: requests split into pre-RPC, RPC,
  and post-RPC phases, network calls kept out of database transactions, and explicit classification
  of errors as retryable or not.
- **Debezium, "Reliable Microservices Data Exchange With the Outbox Pattern" (Gunnar Morling,
  2019) and the Outbox Event Router documentation.** The CDC-based outbox of §4.5: the outbox table
  captured from the WAL, routing by aggregate type, keying by aggregate id, and deleting rows right
  after insert.
- **Netflix Technology Blog, "Netflix Conductor: A microservices orchestrator" (2016).** Explains
  the move from processes spread across services (pub/sub plus direct calls) to a central
  orchestrator for visibility and control: the orchestration-versus-choreography trade in §3.6.
- **Uber Cadence and Temporal.** Cadence was built and open-sourced at Uber; Temporal was started
  in 2019 by Cadence's creators. Both implement durable execution (event-history replay) with
  at-least-once activities (§3.11).
- **Foundational papers.** Garcia-Molina and Salem, "Sagas" (SIGMOD 1987). Pat Helland, "Life
  beyond Distributed Transactions: an Apostate's Opinion" (CIDR 2007), which argues that scalable
  systems should rely on entities, at-least-once messaging, and idempotent processing rather than
  distributed transactions. Gray and Lamport, "Consensus on Transaction Commit" (2006), the
  consensus-replicated alternative to 2PC's blocking coordinator. Corbett et al., "Spanner" (OSDI
  2012), 2PC over Paxos groups inside one database. Chris Richardson's *Microservices Patterns*
  (2018) and microservices.io, the common vocabulary for sagas, pivot transactions, and
  countermeasures.

---

## 12. Sandbox Experiments — Run These Yourself

> **In plain words.** Four experiments with two or three `psql` windows against a throwaway
> Postgres. Each takes a few minutes and makes one claim from this chapter visible.
>
> **Real-world example.** Experiment 3 shows in 30 seconds why a relay that remembers "last
> published id" loses events.

Setup (any recent Postgres; `wal_level=logical` is needed only for experiment 4):

```bash
docker run -d --name pg06 -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:17 -c wal_level=logical
psql postgresql://postgres:pw@localhost/postgres     # open two or three of these
```

Create the `outbox` table from §4.2 and the `idempotency_keys` table from §6.

**Experiment 1 — `SKIP LOCKED` and the reordering it allows.**

```sql
INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
SELECT 'order', (g % 3)::text, 'E' || g, '{}' FROM generate_series(1, 10) g;

-- window A
BEGIN; SELECT id, aggregate_id FROM outbox WHERE published_at IS NULL
       ORDER BY id LIMIT 5 FOR UPDATE SKIP LOCKED;
-- window B (same query): returns ids 6–10 at once. Remove SKIP LOCKED and it waits for A.
```

*Predict:* each of the three aggregates has rows in both batches. If B publishes first, every
aggregate's later events overtake its earlier ones: trap 3 of §4.6.

**Experiment 2 — concurrent duplicate idempotency keys.**

```sql
-- window A
BEGIN; INSERT INTO idempotency_keys (tenant_id, key, fingerprint, status)
       VALUES (1, 'k1', '\x01', 'in_progress') ON CONFLICT DO NOTHING;    -- INSERT 0 1
-- window B: the same INSERT (in autocommit) → it hangs
-- window A: COMMIT;   → B prints INSERT 0 0 (duplicate detected)
```

Repeat with `ROLLBACK` in A: B prints `INSERT 0 1` and would do the work itself. Repeat with B
inside `BEGIN ISOLATION LEVEL REPEATABLE READ`: after A commits, expect a serialization error
instead of `INSERT 0 0`. That is why variant A in §5.4 runs at READ COMMITTED.

**Experiment 3 — identity order is not commit order.**

```sql
-- window A
BEGIN; INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
       VALUES ('order', 'x', 'slow', '{}') RETURNING id;               -- say 11; do not commit
-- window B
INSERT INTO outbox (aggregate_type, aggregate_id, event_type, payload)
VALUES ('order', 'y', 'fast', '{}') RETURNING id;                      -- 12, committed
SELECT max(id) FROM outbox;                                            -- 12; 11 is invisible
-- window A
COMMIT;
```

*Predict:* a relay that polled between B's commit and A's commit, and then queries `id > 12`,
never publishes row 11. A relay that polls on `published_at IS NULL` finds it.

**Experiment 4 — a replication slot retains WAL.**

```sql
SELECT pg_create_logical_replication_slot('demo', 'pgoutput');
CREATE TABLE junk AS SELECT g, repeat('x', 1000) AS pad FROM generate_series(1, 200000) g;
SELECT slot_name,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal
  FROM pg_replication_slots;
SELECT pg_drop_replication_slot('demo');                               -- clean up
```

*Predict:* the retained WAL is at least the size of the table you just wrote (about 200 MB), and it
keeps growing with every write until a consumer confirms progress or the slot is dropped. Multiply
by a weekend of production writes for pitfall 9.2.

---

## Key Takeaways

1. **Every remote call has three outcomes: success, failure, unknown.** Most bugs in this area come
   from treating unknown as failure.
2. **You cannot make two independent systems commit atomically with careful code.** Reduce to one
   commit (outbox, transactional enqueue, listen-to-yourself) and make everything downstream safe
   to repeat.
3. **2PC is a database-internal tool.** It works inside Spanner or CockroachDB, where the
   coordinator is replicated. Between services, its blocking coordinator, lock hold times, and
   participant requirements make it the wrong choice.
4. **Sagas trade isolation for availability.** Order steps around a pivot, make compensations
   idempotent inverse deltas, and use semantic locks, commutative updates, and version checks
   against the anomalies.
5. **Persist the orchestrator's state and send its commands through its own outbox.** Add a sweeper
   that re-sends due commands with the same keys, and alert on saga age.
6. **The outbox is at-least-once and ordered only if you make it so.** Lock the aggregate row, poll
   on `published_at IS NULL`, run one relay per ordering domain, key messages by aggregate, and
   watch `outbox_lag_seconds`.
7. **Effectively-once = at-least-once delivery + idempotent processing**, with the dedup record
   committed atomically with the effect, keyed by a producer-assigned id.
8. **Idempotency keys are a contract:** scope per tenant, fingerprint the request, store the final
   response, 409 while in progress, 422 on mismatch, TTL longer than the client's retry horizon.
9. **Idempotency makes retries safe, not free.** Retry budgets, backoff, and deadlines from
   [`33-resilience-patterns-circuit-breakers.md`](33-resilience-patterns-circuit-breakers.md) still apply.

---

## Cross-References

### Within distributed-systems/
- **[`00-primitives-and-system-models.md`](00-primitives-and-system-models.md)**: failure models, the UNKNOWN outcome of a remote call,
  consistency models, CAP/PACELC.
- **[`03-consensus-raft-and-distributed-locking.md` §9](03-consensus-raft-and-distributed-locking.md#9-distributed-locking-fundamentals)**: leases and fencing tokens, the same idea as
  version checks and relay leadership here.
- **[`04-replication-and-consistency.md`](04-replication-and-consistency.md)**: read-your-writes and session guarantees, relevant when a
  listen-to-yourself API returns before its own database is updated (§4.9).
- **[`07-kafka-and-event-streaming.md` §3.6](07-kafka-and-event-streaming.md#36-idempotent-producers), §6**: idempotent producer, transactions, and when Kafka
  exactly-once applies.
- **[`22-stream-processing-flink-watermarks-eos.md` §6.3](22-stream-processing-flink-watermarks-eos.md#63-exactly-once-end-to-end)**: end-to-end exactly-once with
  transactional and idempotent sinks.
- **[`29-failure-detection-phi-accrual.md`](29-failure-detection-phi-accrual.md)**: why a timeout cannot tell "slow" from "dead", the root
  of the unknown outcome.
- **[`33-resilience-patterns-circuit-breakers.md` §2](33-resilience-patterns-circuit-breakers.md#2-retry-patterns----the-deceptively-dangerous-pattern), §9.1**: retry classification, budgets, and the
  payment-gateway case study that relies on idempotency keys.
- **[`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md)**: draining a relay or saga backlog after an
  outage without overloading consumers.
- **[`35-reliability-math-slos-and-error-budgets.md`](35-reliability-math-slos-and-error-budgets.md)**: availability of serial dependencies, the
  `a^N` behind 2PC's availability cost.
- **[`36-multi-region-active-active-and-geo-replication.md`](36-multi-region-active-active-and-geo-replication.md)**: concurrent writes across regions,
  conflict resolution, and CRDTs, which sagas do not solve.
- **[`37-distributed-systems-debugging.md`](37-distributed-systems-debugging.md)**: tracing one order across services and debugging stuck
  sagas and growing outbox lag.

### From databases/
- **[`../databases/05-transactions-and-concurrency.md`](../databases/05-transactions-and-concurrency.md)**: §2 anomalies, §6.1–6.3 2PC, 3PC and a
  saga summary, §8.6 idempotency keys as a SQL function.
- **[`../databases/19-distributed-databases-deep-dive.md`](../databases/19-distributed-databases-deep-dive.md)**: §4 distributed commit protocols
  (Percolator, Spanner, CockroachDB parallel commits, Calvin), §11 change data capture.
- **[`../databases/12-replication-and-distributed-storage.md` §5.3](../databases/12-replication-and-distributed-storage.md#53-saga-pattern)**: saga choreography versus
  orchestration from the storage side.
- **[`../databases/14-write-ahead-log-internals.md`](../databases/14-write-ahead-log-internals.md)**: the WAL that logical decoding and CDC read.

### From solutions/
- **[`../solutions/workflow-orchestration-design.md` §12](../solutions/workflow-orchestration-design.md#12-saga-compensation-and-failure-semantics)**: saga compensation and failure semantics
  inside a workflow engine.
- **[`../solutions/job-scheduler-postgres-deep-dive.md` §20](../solutions/job-scheduler-postgres-deep-dive.md#20-producer-side-transactional-enqueue--outbox)**: transactional enqueue and the outbox
  for a Postgres-backed job queue.
- **[`../solutions/api-message-patterns.md` §4.3](../solutions/api-message-patterns.md#43-outbox-pattern)**: a short outbox sketch in the context of CQRS and
  event-driven APIs.
- **[`../solutions/big-tech-api-standards.md` §4.6](../solutions/big-tech-api-standards.md#46-idempotency)**: Stripe-style idempotency from the API-design
  angle.
- **[`../solutions/distributed-counter-design.md` §7](../solutions/distributed-counter-design.md#7-idempotency--deduplication)**: idempotency and deduplication for counters.
- **[`../solutions/agent-orchestration-design.md` §11.2](../solutions/agent-orchestration-design.md#112-exactly-once-tool-execution-via-idempotency-keys)** and
  **[`../solutions/tool-platform-design.md` §10.6](../solutions/tool-platform-design.md#106-idempotency-key-handling)**: idempotency keys for agent tool execution.

### Elsewhere
- **[`../ai-rag/24-tool-calling-and-enterprise-integration.md` §8](../ai-rag/24-tool-calling-and-enterprise-integration.md#8-idempotency)**: idempotency for LLM tool calls,
  including systems without idempotency keys.
- **[`../sre-observability/25-streaming-and-kafka-observability.md` §9](../sre-observability/25-streaming-and-kafka-observability.md#9-exactly-once-and-idempotency-observability)**: observing exactly-once and
  idempotency in streaming pipelines.
