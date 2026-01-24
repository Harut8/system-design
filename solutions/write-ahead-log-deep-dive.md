# Write-Ahead Log (WAL): A Deep Dive

## Executive Summary

Write-Ahead Logging (WAL) is the foundational durability mechanism in modern database systems. This document covers how WAL works, its alternatives, modern optimizations, distributed systems integration, and critical trade-offs for system design decisions.

---

## Table of Contents

1. [Fundamentals](#1-fundamentals)
2. [The ARIES Protocol](#2-the-aries-protocol)
3. [Alternatives to WAL](#3-alternatives-to-wal)
4. [Modern Optimizations](#4-modern-optimizations)
5. [WAL in Distributed Systems](#5-wal-in-distributed-systems)
6. [Event Sourcing vs WAL](#6-event-sourcing-vs-wal)
7. [Trade-offs and Decision Framework](#7-trade-offs-and-decision-framework)
8. [Production Recommendations](#8-production-recommendations)
9. [Failure Modes and Recovery Edge Cases](#9-failure-modes-and-recovery-edge-cases)
10. [Performance Internals Deep Dive](#10-performance-internals-deep-dive)
11. [Storage Layer and Filesystem Interactions](#11-storage-layer-and-filesystem-interactions)
12. [Operational Concerns](#12-operational-concerns)
13. [WAL as Replication Protocol](#13-wal-as-replication-protocol)
14. [Beyond Traditional WAL](#14-beyond-traditional-wal)

---

## 1. Fundamentals

### What is WAL?

Write-Ahead Logging is a protocol that guarantees **atomicity** and **durability** (two of the ACID properties) by following one rule:

> **Before any change is applied to the main data store, the change must first be written to an append-only log on durable storage.**

### How It Works

```
┌─────────────────────────────────────────────────────────────────────┐
│                        WAL Write Path                                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  1. Transaction Request                                              │
│         │                                                            │
│         ▼                                                            │
│  2. Write to WAL Buffer (memory)                                     │
│         │                                                            │
│         ▼                                                            │
│  3. fsync WAL to Disk ◄──── DURABILITY GUARANTEE POINT               │
│         │                                                            │
│         ▼                                                            │
│  4. Acknowledge Commit to Client                                     │
│         │                                                            │
│         ▼                                                            │
│  5. Apply Changes to Data Pages (can be async)                       │
│         │                                                            │
│         ▼                                                            │
│  6. Checkpoint: Flush dirty pages, truncate old WAL                  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### Why Sequential Writes Matter

| Write Pattern | HDD Performance | SSD Performance |
|---------------|-----------------|-----------------|
| Random 4KB writes | ~100 IOPS | ~10,000 IOPS |
| Sequential writes | ~100 MB/s | ~500+ MB/s |

WAL converts random writes to data pages into sequential appends—a **100-1000x performance improvement** on traditional storage.

### Key WAL Components

1. **Log Sequence Number (LSN)**: Monotonically increasing identifier for each log record
2. **Log Records**: Contain transaction ID, operation type, before/after images
3. **Checkpoints**: Periodic snapshots that allow WAL truncation
4. **Recovery**: Replay uncommitted transactions from last checkpoint

### Systems Using WAL

| System | WAL Implementation |
|--------|-------------------|
| PostgreSQL | pg_wal directory |
| MySQL/InnoDB | Redo logs |
| SQLite | WAL mode (optional) |
| MongoDB | Oplog + journal |
| Oracle | Redo logs |
| Kafka | Commit log (conceptually similar) |
| etcd | WAL directory |

---

## 2. The ARIES Protocol

ARIES (Algorithms for Recovery and Isolation Exploiting Semantics) is the industry-standard WAL implementation, developed at IBM in the 1990s. Nearly all modern RDBMS use ARIES or derivatives.

### Three Recovery Phases

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ARIES Recovery Process                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  CRASH                                                               │
│    │                                                                 │
│    ▼                                                                 │
│  ┌─────────────┐                                                     │
│  │  ANALYSIS   │  Scan log from last checkpoint                      │
│  │   PHASE     │  Build: Active Transaction Table (ATT)              │
│  │             │         Dirty Page Table (DPT)                      │
│  └──────┬──────┘                                                     │
│         │                                                            │
│         ▼                                                            │
│  ┌─────────────┐                                                     │
│  │    REDO     │  Replay ALL logged actions (committed or not)       │
│  │   PHASE     │  Start from oldest LSN in DPT                       │
│  │             │  "History repeating"                                │
│  └──────┬──────┘                                                     │
│         │                                                            │
│         ▼                                                            │
│  ┌─────────────┐                                                     │
│  │    UNDO     │  Rollback all uncommitted transactions              │
│  │   PHASE     │  Process ATT in reverse LSN order                   │
│  │             │  Write Compensation Log Records (CLRs)              │
│  └─────────────┘                                                     │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### ARIES Key Principles

1. **Write-Ahead Logging**: Log before data page modification
2. **Repeating History**: Redo all actions, then undo losers
3. **Logging During Undo**: CLRs ensure idempotent recovery
4. **Steal/No-Force**:
   - **Steal**: Dirty pages can be flushed before commit
   - **No-Force**: Pages don't need to be flushed at commit time

### Why ARIES Won

| Approach | Steal | Force | Undo Needed | Redo Needed | Used By |
|----------|-------|-------|-------------|-------------|---------|
| ARIES | Yes | No | Yes | Yes | PostgreSQL, MySQL, Oracle |
| Shadow Paging | No | Yes | No | No | LMDB, CouchDB |
| System R (original) | No | Yes | No | Yes | Historical |

ARIES provides the best balance of:
- **Buffer pool flexibility** (steal policy)
- **Fast commits** (no-force policy)
- **Crash recovery guarantees**

---

## 3. Alternatives to WAL

### 3.1 Shadow Paging

**How it works**: Maintain two page tables—current and shadow. Writes go to new pages; commit atomically swaps the page table pointer.

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Shadow Paging                                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   BEFORE COMMIT                    AFTER COMMIT                      │
│                                                                      │
│   Shadow PT ──► [A][B][C]          Shadow PT ──► [A'][B][C']        │
│       ▲                                 ▲                            │
│       │                                 │                            │
│   Root Ptr                          Root Ptr (atomic swap)           │
│                                                                      │
│   Current PT ──► [A'][B][C']       (old pages can be freed)         │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Advantages**:
- No separate log file needed
- Instant recovery (just use shadow)
- Simpler mental model

**Disadvantages**:
- Storage fragmentation over time
- Garbage collection complexity
- Poor performance for small updates (must copy entire page)
- Doesn't compose well (LMDB on Btrfs = double COW overhead)

**Who uses it**: LMDB, CouchDB, SQLite (in rollback journal mode)

### 3.2 Log-Structured Merge Trees (LSM)

**How it works**: Buffer writes in memory (memtable), flush as immutable sorted files (SSTables), merge in background.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         LSM Tree Architecture                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Write ──► Memtable (in-memory, sorted)                             │
│                │                                                     │
│                │ flush when full                                     │
│                ▼                                                     │
│   Level 0:  [SST][SST][SST]  (recently flushed, may overlap)        │
│                │                                                     │
│                │ compaction                                          │
│                ▼                                                     │
│   Level 1:  [    SST    ][    SST    ]  (sorted, non-overlapping)   │
│                │                                                     │
│                │ compaction                                          │
│                ▼                                                     │
│   Level 2:  [  SST  ][  SST  ][  SST  ][  SST  ]  (larger files)    │
│                                                                      │
│   Read: Check memtable → L0 → L1 → L2 (use bloom filters)           │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Note**: LSM trees still use WAL for the memtable! The WAL protects in-flight data before SSTable flush.

**Advantages**:
- Exceptional write throughput (sequential I/O only)
- Good space efficiency (compression-friendly)
- No in-place updates = no torn writes

**Disadvantages**:
- Read amplification (must check multiple levels)
- Write amplification from compaction
- Space amplification during compaction
- Unpredictable latency spikes during compaction

**Who uses it**: RocksDB, LevelDB, Cassandra, HBase, Kafka (conceptually)

### 3.3 B-Tree vs LSM Trade-offs

| Metric | B-Tree + WAL | LSM Tree |
|--------|--------------|----------|
| **Write Throughput** | Lower (random I/O) | Higher (sequential) |
| **Read Latency** | Lower (single seek) | Higher (multiple levels) |
| **Space Efficiency** | Lower (~60-70% fill) | Higher (can be 100%) |
| **Write Amplification** | Variable | Higher (compaction) |
| **Read Amplification** | 1 (single path) | N (levels to check) |
| **Predictable Latency** | Yes | No (compaction spikes) |

**Rule of thumb**:
- **Read-heavy workloads** → B-Tree + WAL
- **Write-heavy workloads** → LSM Tree
- **Mixed workloads** → Depends on SLA requirements

### 3.4 Copy-on-Write Filesystems (ZFS, Btrfs)

These filesystems provide similar guarantees at the filesystem level:

**How COW works**:
1. Write goes to new block
2. Update parent pointers (also COW)
3. Atomic root pointer update commits everything
4. Old blocks freed after no references remain

**Trade-offs for databases**:

| Aspect | Impact on Database |
|--------|-------------------|
| Durability | Excellent—immune to power-loss corruption |
| Write amplification | 2-5x worse for random write workloads |
| Fragmentation | Degrades sequential read performance over time |
| Double-COW problem | Running COW database on COW filesystem = wasteful |

**Recommendation**:
- Disable COW for database data directories (`chattr +C` on Btrfs)
- Or accept the overhead for simpler operations

---

## 4. Modern Optimizations

### 4.1 Group Commit (Batching)

**Problem**: Each fsync costs ~1-10ms. At 1 transaction per fsync = 100-1000 TPS max.

**Solution**: Batch multiple transactions into single fsync.

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Group Commit Timeline                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Time ──────────────────────────────────────────────────────►       │
│                                                                      │
│   T1: ═══▶ write WAL ─┐                                              │
│   T2: ════════▶ write │─── wait ───┐                                 │
│   T3: ══════════════▶ │            │                                 │
│                       └── BATCH ───┴── fsync ──► all commit          │
│                                                                      │
│   Without group commit: 3 fsyncs = 30ms                              │
│   With group commit:    1 fsync  = 10ms                              │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**PostgreSQL tuning**:
```sql
-- Wait up to 10ms for more transactions to join the group
SET commit_delay = 10000;  -- microseconds

-- Only wait if at least 5 transactions are active
SET commit_siblings = 5;
```

**Trade-off**: Increased latency for individual transactions, but higher overall throughput.

### 4.2 Asynchronous Commit

**Concept**: Report commit success before fsync completes.

```
┌─────────────────────────────────────────────────────────────────────┐
│              Synchronous vs Asynchronous Commit                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   SYNCHRONOUS (default):                                             │
│   Client ──► Write WAL ──► fsync ──► ACK to client                   │
│                              │                                       │
│                          BLOCKING                                    │
│                                                                      │
│   ASYNCHRONOUS:                                                      │
│   Client ──► Write WAL ──► ACK to client (immediate)                 │
│                    │                                                 │
│                    └──► background fsync (within wal_writer_delay)   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Risk window**: Up to `3 × wal_writer_delay` (default 200ms × 3 = 600ms of potential data loss)

**Performance impact** (PostgreSQL benchmarks):
| Configuration | TPS Improvement | Latency Reduction |
|--------------|-----------------|-------------------|
| synchronous_commit=off | +3.5% | -3.4% |
| fsync=off (DANGEROUS) | +10.7% | -6.5% |

**When to use async commit**:
- Analytics pipelines
- Caching layers with external redundancy
- Non-financial transactions
- Systems where losing last 0.5s of data is acceptable

**When NOT to use**:
- Financial transactions (ATM, payments)
- Any external side effects (sent emails, API calls)
- Audit logs

### 4.3 NVMe and Persistent Memory Optimizations

Modern hardware changes the WAL calculus significantly:

**Latency comparison**:
| Storage Type | fsync Latency | Max TPS (single-threaded) |
|--------------|---------------|---------------------------|
| HDD | 2-10ms | 100-500 |
| SATA SSD | 0.5-2ms | 500-2,000 |
| NVMe SSD | 50-200μs | 5,000-20,000 |
| Persistent Memory | 0.3μs | 3,000,000+ |

**Modern architectures**:

1. **SpanDB approach**: Put WAL + hot data on NVMe, cold data on cheaper SSDs
2. **PMEM for WAL tail**: Use persistent memory for uncommitted WAL, async flush to NVMe
3. **Autonomous commit**: Exploit NVMe parallelism, skip group commit entirely

```
┌─────────────────────────────────────────────────────────────────────┐
│                   Multi-Tier WAL Architecture                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Tier 1: Persistent Memory (0.3μs)                                  │
│           └── WAL tail (last few MB)                                 │
│                    │                                                 │
│                    │ async drain                                     │
│                    ▼                                                 │
│   Tier 2: NVMe SSD (100μs)                                           │
│           └── WAL archive + hot data                                 │
│                    │                                                 │
│                    │ tiering                                         │
│                    ▼                                                 │
│   Tier 3: SATA SSD / Object Storage                                  │
│           └── Cold data + long-term WAL archive                      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.4 Autonomous Commit (2025 Research)

Traditional group commit doesn't scale on modern NVMe:

**Problem**: Group commit serializes on a single thread, can't exploit NVMe's 100K+ IOPS.

**Solution**: Each worker writes its own small WAL batches in parallel.

**Results**: Near-linear scalability, sub-millisecond p99 commit latency at high throughput.

---

## 5. WAL in Distributed Systems

### 5.1 Consensus Algorithms and WAL

Distributed systems use replicated logs (conceptually WAL) as the foundation for consensus:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Raft Log Replication                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   LEADER                                                             │
│   ┌────────────────────────────┐                                     │
│   │ Log: [1][2][3][4][5][6]    │                                     │
│   └──────────┬─────────────────┘                                     │
│              │ AppendEntries RPC                                     │
│              ▼                                                       │
│   FOLLOWERS                                                          │
│   ┌────────────────────────────┐                                     │
│   │ Log: [1][2][3][4][5][6]    │  ◄── Committed (quorum)            │
│   └────────────────────────────┘                                     │
│   ┌────────────────────────────┐                                     │
│   │ Log: [1][2][3][4][5]       │  ◄── One entry behind              │
│   └────────────────────────────┘                                     │
│                                                                      │
│   Entry committed when replicated to majority (quorum)               │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 Raft vs Paxos

| Aspect | Raft | Paxos |
|--------|------|-------|
| **Understandability** | Designed for clarity | Notoriously complex |
| **Leader election** | Explicit phase | Implicit in protocol |
| **Log handling** | Leader must have up-to-date log | Any node can lead, then catch up |
| **Production use** | etcd, CockroachDB, TiKV | Chubby, Spanner |
| **Performance** | Slightly more efficient (no log exchange during election) | Can be faster for certain failure modes |

### 5.3 Systems Using Replicated Logs

| System | Consensus | Log Purpose |
|--------|-----------|-------------|
| etcd | Raft | Key-value store replication |
| CockroachDB | Raft | Range-level replication |
| TiKV | Raft | Distributed KV store |
| Kafka | KRaft (Raft) | Metadata management |
| Spanner | Paxos | Global consistency |
| ZooKeeper | Zab (Paxos-like) | Configuration management |

### 5.4 WAL in Cloud-Native Databases

Modern cloud databases separate compute from storage:

```
┌─────────────────────────────────────────────────────────────────────┐
│                 Cloud-Native WAL Architecture                        │
│                      (Aurora, Neon, etc.)                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Compute Layer (stateless)                                          │
│   ┌─────────────────────────────────────────────────────────┐       │
│   │  PostgreSQL    PostgreSQL    PostgreSQL                  │       │
│   │  (primary)     (replica)     (replica)                   │       │
│   └───────┬────────────┬────────────┬───────────────────────┘       │
│           │            │            │                                │
│           │ WAL only   │ WAL stream │                                │
│           ▼            ▼            ▼                                │
│   Storage Layer (durable, distributed)                               │
│   ┌─────────────────────────────────────────────────────────┐       │
│   │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐       │       │
│   │  │ WAL Svc │ │ WAL Svc │ │ WAL Svc │ │ WAL Svc │       │       │
│   │  │  (AZ1)  │ │  (AZ2)  │ │  (AZ3)  │ │  (AZ4)  │       │       │
│   │  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘       │       │
│   │       └───────────┴───────────┴───────────┘             │       │
│   │                       │                                  │       │
│   │               Object Storage (S3)                        │       │
│   │          (long-term WAL archive, data pages)            │       │
│   └─────────────────────────────────────────────────────────┘       │
│                                                                      │
│   Key insight: "The log is the database"                             │
│   Storage layer reconstructs pages from WAL on demand                │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Key innovations**:
- **Write only WAL to network** (6x less data than traditional replication)
- **Storage computes pages** from WAL (offload from compute)
- **Instant scaling** (compute is stateless)
- **Point-in-time recovery** by replaying WAL to any LSN

---

## 6. Event Sourcing vs WAL

Event Sourcing and WAL are conceptually similar but serve different purposes:

### Comparison

| Aspect | WAL | Event Sourcing |
|--------|-----|----------------|
| **Purpose** | Durability + crash recovery | Business event history |
| **Granularity** | Physical operations (page writes) | Logical business events |
| **Retention** | Truncated after checkpoint | Kept forever (audit trail) |
| **Query pattern** | Never queried directly | Queried for event replay |
| **Schema** | Fixed, internal format | Business-domain entities |
| **Consumers** | Database recovery only | Multiple read models, projections |

### When to Use What

```
┌─────────────────────────────────────────────────────────────────────┐
│                  WAL vs Event Sourcing Decision                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Use WAL (default database behavior) when:                          │
│   • You need ACID transactions                                       │
│   • Current state is what matters                                    │
│   • Standard CRUD operations                                         │
│   • Operational durability (crash recovery)                          │
│                                                                      │
│   Use Event Sourcing when:                                           │
│   • Full audit trail is required                                     │
│   • Time-travel queries needed                                       │
│   • Multiple read models from same events                            │
│   • Event-driven architecture with consumers                         │
│   • Domain events have business meaning                              │
│                                                                      │
│   Note: Event Sourcing systems STILL use WAL internally!             │
│   (Kafka uses WAL, EventStoreDB uses WAL, etc.)                      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### CQRS + Event Sourcing Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   Commands ──► Command Handler ──► Event Store (append-only)         │
│                                          │                           │
│                                          │ events                    │
│                                          ▼                           │
│                                    ┌───────────┐                     │
│                                    │ Projector │                     │
│                                    └─────┬─────┘                     │
│                          ┌───────────────┼───────────────┐           │
│                          ▼               ▼               ▼           │
│                    ┌──────────┐   ┌──────────┐   ┌──────────┐       │
│                    │ Read DB  │   │ Search   │   │ Reports  │       │
│                    │ (SQL)    │   │ (Elastic)│   │ (Analytics)      │
│                    └────┬─────┘   └────┬─────┘   └────┬─────┘       │
│                         │              │              │              │
│   Queries ◄─────────────┴──────────────┴──────────────┘              │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 7. Trade-offs and Decision Framework

### 7.1 Durability vs Performance Spectrum

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                      │
│   MAXIMUM DURABILITY                          MAXIMUM PERFORMANCE    │
│   ◄────────────────────────────────────────────────────────────────► │
│                                                                      │
│   Sync WAL      Group       Async         Unlogged      In-Memory   │
│   + fsync       Commit      Commit        Tables        Only        │
│                                                                      │
│   │             │           │             │             │            │
│   │ Every       │ Batch     │ Background  │ No WAL      │ No disk   │
│   │ commit      │ commits   │ fsync       │ at all      │ writes    │
│   │ waits       │ together  │ periodic    │             │            │
│   │                                                                  │
│   Data loss:    Data loss:  Data loss:    Data loss:    Data loss:  │
│   0             0           <1s window    Full table    Everything  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 7.2 Decision Matrix

| Requirement | Recommended Approach |
|-------------|---------------------|
| Financial transactions | Synchronous WAL + replication |
| General OLTP | Group commit with tuning |
| Analytics ingestion | Async commit or batch loading |
| Cache warming | Unlogged tables |
| Time-series high-volume | LSM-based store (InfluxDB, TimescaleDB) |
| Audit compliance | Event sourcing + immutable log |
| Global distribution | Raft/Paxos replicated log |

### 7.3 The Amplification Trade-off

Every storage engine optimizes for at most 2 of 3:

```
                    Read Amplification
                          /\
                         /  \
                        /    \
                       /      \
                      / B-Tree \
                     /   +WAL   \
                    /____________\
                   /              \
                  /                \
   Write        /                  \    Space
   Amplification ──────────────────── Amplification
                        LSM
```

- **B-Tree + WAL**: Low read amp, variable write amp, moderate space amp
- **LSM Tree**: Higher read amp, lower write amp (for inserts), better space amp
- **Shadow Paging**: Low read amp, high write amp, high space amp (fragmentation)

---

## 8. Production Recommendations

### 8.1 PostgreSQL WAL Tuning

```sql
-- For high-throughput OLTP
wal_level = replica                    -- minimal, replica, or logical
wal_buffers = 64MB                     -- increase for write-heavy
checkpoint_completion_target = 0.9     -- spread checkpoint I/O
max_wal_size = 4GB                     -- before forced checkpoint

-- Group commit tuning (if many concurrent transactions)
commit_delay = 10000                   -- microseconds to wait
commit_siblings = 5                    -- minimum concurrent transactions

-- For replication
synchronous_commit = on                -- or 'remote_apply' for strongest
synchronous_standby_names = 'replica1' -- synchronous replicas
```

### 8.2 When to Disable/Relax WAL

| Scenario | Configuration | Risk |
|----------|---------------|------|
| ETL bulk load | `UNLOGGED` tables | Table lost on crash |
| CI/CD test databases | `fsync=off` | Full DB corruption possible |
| Analytics with external redundancy | `synchronous_commit=off` | <1s data loss window |
| Development only | `fsync=off` | Never in production |

### 8.3 Monitoring Checklist

- **WAL generation rate**: GB/hour, indicates write pressure
- **Checkpoint frequency**: Too frequent = IO spikes, too rare = long recovery
- **WAL archive lag**: For replication/PITR, should be minimal
- **fsync latency**: Directly impacts commit latency
- **Replication lag**: For distributed systems

### 8.4 Hardware Recommendations

| Component | Recommendation |
|-----------|---------------|
| WAL disk | NVMe SSD, separate from data |
| RAID | RAID 10 for WAL (avoid RAID 5/6 write penalty) |
| Battery-backed cache | Required for stated fsync performance |
| Network (distributed) | 10Gbps+ for replicated WAL |

---

## 9. Failure Modes and Recovery Edge Cases

### 9.1 Torn Writes and Partial Sector Writes

A **torn write** occurs when a multi-sector write is interrupted mid-way, leaving some sectors with new data and others with old data.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Torn Write Scenario                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Write Request: 8KB page (16 sectors × 512 bytes)                  │
│                                                                      │
│   BEFORE CRASH:                                                      │
│   ┌────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┬────┐    │
│   │ S1 │ S2 │ S3 │ S4 │ S5 │ S6 │ S7 │ S8 │ S9 │S10 │S11 │S12 │    │
│   │new │new │new │new │new │new │ ?? │ ?? │old │old │old │old │    │
│   └────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┴────┘    │
│                               ▲                                      │
│                         POWER FAILURE                                │
│                                                                      │
│   Result: Page is corrupted—neither old state nor new state          │
│                                                                      │
│   PARTIAL SECTOR WRITE (worse):                                      │
│   Even a single 512-byte sector can be partially written:            │
│   ┌──────────────────────────────────────────────────────┐          │
│   │ Sector: [valid data | garbage | valid old data]      │          │
│   └──────────────────────────────────────────────────────┘          │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Why this matters for WAL**:
- WAL records can span multiple sectors
- A torn WAL record could cause silent data corruption during replay
- Recovery code must detect and handle partial writes

**Protection strategies**:

| Strategy | How It Works | Used By |
|----------|--------------|---------|
| **Doublewrite buffer** | Write pages to separate buffer first, then to final location | MySQL/InnoDB |
| **Full page writes** | Write entire page image to WAL after checkpoint | PostgreSQL |
| **Atomic sector writes** | Hardware guarantees (4KB atomic on some NVMe) | Modern SSDs |
| **Checksums** | Detect corruption after the fact | All systems |

### 9.2 Checksum Strategies

Checksums are the primary defense against silent corruption:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Page-Level vs Record-Level Checksums             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   PAGE-LEVEL CHECKSUM:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Page Header │ Tuple 1 │ Tuple 2 │ ... │ Free │ CRC32 │      │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                            ▲                         │
│                                    Covers entire page                │
│                                                                      │
│   Pros: Simple, low overhead                                         │
│   Cons: Must re-checksum entire page on any update                  │
│                                                                      │
│   RECORD-LEVEL CHECKSUM:                                             │
│   ┌───────────────────────────────────────────────────────────┐     │
│   │ WAL Record:                                                │     │
│   │ ┌────────┬────────────┬──────────────┬─────────┬────────┐ │     │
│   │ │ Header │ TxID, LSN  │ Before/After │ Payload │ CRC32  │ │     │
│   │ └────────┴────────────┴──────────────┴─────────┴────────┘ │     │
│   └───────────────────────────────────────────────────────────┘     │
│                                                                      │
│   Pros: Validates individual records, enables partial recovery       │
│   Cons: Higher overhead, more checksums to compute                   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Algorithm choices**:

| Algorithm | Speed | Collision Resistance | Use Case |
|-----------|-------|---------------------|----------|
| CRC32C | Very fast (hardware) | Good for random errors | PostgreSQL, RocksDB |
| xxHash | Extremely fast | Good | Modern systems |
| SHA-256 | Slow | Cryptographic | When integrity is paramount |
| FNV-1a | Fast | Moderate | Simple implementations |

**PostgreSQL checksum behavior**:
```sql
-- Enable at initdb time (cannot change later)
initdb --data-checksums

-- Check status
SHOW data_checksums;

-- Verify checksums offline
pg_checksums --check /path/to/data
```

### 9.3 Log Corruption Detection and Repair

**Detection methods**:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    WAL Corruption Detection                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   1. CHECKSUM MISMATCH                                               │
│      Record CRC ≠ Computed CRC → Corruption detected                │
│                                                                      │
│   2. MAGIC NUMBER VIOLATION                                          │
│      Expected: 0xD066F0E5                                            │
│      Found:    0x00000000 → Corruption or truncation                │
│                                                                      │
│   3. STRUCTURAL VALIDATION                                           │
│      - Record length exceeds remaining file size                     │
│      - LSN not monotonically increasing                              │
│      - Transaction ID references future/invalid transaction          │
│                                                                      │
│   4. SEMANTIC VALIDATION                                             │
│      - Page referenced doesn't exist                                 │
│      - Operation type is invalid                                     │
│      - Before-image doesn't match current page state                 │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Repair strategies**:

| Scenario | Strategy | Data Loss |
|----------|----------|-----------|
| Corruption at end of WAL | Truncate to last valid record | Recent uncommitted |
| Corruption in middle | Stop recovery, manual intervention | Varies |
| Bit flip in committed record | Restore from replica/backup | Depends on detection time |
| Entire WAL segment lost | Restore from archive + replay | Segment's worth |

**PostgreSQL tools**:
```bash
# Dump WAL contents for inspection
pg_waldump /path/to/wal/000000010000000000000001

# Reset to specific LSN (DANGEROUS - data loss)
pg_resetwal -l 000000010000000000000042 /path/to/data
```

### 9.4 Crash Loops Caused by Bad WAL Replay

A **crash loop** occurs when recovery itself causes a crash, creating an unrecoverable state:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Crash Loop Scenario                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Start Database                                                     │
│        │                                                             │
│        ▼                                                             │
│   Begin Recovery                                                     │
│        │                                                             │
│        ▼                                                             │
│   Replay WAL Record N ──► CRASH (OOM, assertion, bug)               │
│        │                                                             │
│        │ restart                                                     │
│        ▼                                                             │
│   Begin Recovery (again)                                             │
│        │                                                             │
│        ▼                                                             │
│   Replay WAL Record N ──► CRASH (same record, same crash)           │
│        │                                                             │
│        └──────► INFINITE LOOP                                        │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Causes**:
- Bug in recovery code triggered by specific data pattern
- Corrupted WAL record that passes checksum but has invalid semantics
- Resource exhaustion during recovery (large transaction)
- Missing referenced objects (dropped table, missing file)

**Solutions**:

| Approach | When to Use |
|----------|-------------|
| Skip corrupted record | If data loss is acceptable, record is isolated |
| Increase resources | If OOM during large transaction replay |
| Point-in-time recovery | Restore to before the problematic transaction |
| Manual WAL editing | Last resort, expert-only, backup first |
| Restore from replica | If replica is ahead of corruption point |

**PostgreSQL recovery configuration**:
```
# postgresql.conf for debugging crash loops
recovery_target_lsn = '0/1A2B3C4D'    # Stop before bad record
recovery_target_action = 'pause'       # Don't promote, allow inspection
```

### 9.5 Idempotency Guarantees During Redo

**The idempotency problem**: If recovery crashes and restarts, some records will be replayed multiple times. The redo operation must produce the same result regardless of how many times it's applied.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Idempotent Redo Example                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   NON-IDEMPOTENT (WRONG):                                           │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL Record: "INCREMENT counter BY 1"                         │   │
│   │                                                              │   │
│   │ First replay:  counter = 5 → 6                              │   │
│   │ Second replay: counter = 6 → 7  ← WRONG!                    │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   IDEMPOTENT (CORRECT):                                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL Record: "SET counter TO 6 (was 5)"                       │   │
│   │                                                              │   │
│   │ First replay:  counter = 5 → 6                              │   │
│   │ Second replay: counter = 6 → 6  ← Correct!                  │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   ARIES APPROACH - LSN-based skip:                                  │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Page LSN = 100, WAL Record LSN = 95                          │   │
│   │ → Skip: page already has this change                         │   │
│   │                                                              │   │
│   │ Page LSN = 90, WAL Record LSN = 95                           │   │
│   │ → Apply: page needs this update                              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Idempotency mechanisms**:

| Mechanism | How It Works | Used By |
|-----------|--------------|---------|
| **Page LSN comparison** | Skip if page LSN ≥ record LSN | ARIES, PostgreSQL |
| **Physical logging** | Log exact byte changes, not operations | Most RDBMS |
| **Compensation Log Records** | CLRs mark what was undone, prevent re-undo | ARIES |
| **Tombstones** | Mark deletions explicitly | LSM systems |

---

## 10. Performance Internals Deep Dive

### 10.1 Group Commit Internals

Group commit batches multiple transactions into a single fsync. The implementation is more complex than the concept:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Group Commit State Machine                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Transaction arrives                                                │
│        │                                                             │
│        ▼                                                             │
│   ┌─────────────────┐                                               │
│   │ Write to WAL    │  (memcpy to shared buffer)                    │
│   │ buffer          │                                                │
│   └────────┬────────┘                                               │
│            │                                                         │
│            ▼                                                         │
│   ┌─────────────────┐    No leader?     ┌──────────────────┐        │
│   │ Check: Am I the ├──────────────────►│ Become leader    │        │
│   │ first waiter?   │                   │ Start wait timer │        │
│   └────────┬────────┘                   └────────┬─────────┘        │
│            │ No, leader exists                   │                   │
│            │                                     │ Timer expires OR  │
│            ▼                                     │ buffer full       │
│   ┌─────────────────┐                           │                   │
│   │ Wait on         │                           │                   │
│   │ condition var   │◄──────────────────────────┘                   │
│   └────────┬────────┘                                               │
│            │ Leader signals                                          │
│            ▼                                                         │
│   ┌─────────────────┐                                               │
│   │ Leader: fsync   │  (single syscall for all waiters)             │
│   │ Wake all        │                                                │
│   └────────┬────────┘                                               │
│            │                                                         │
│            ▼                                                         │
│   All transactions committed                                         │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Tuning parameters**:

| Parameter | Effect of Increasing | Trade-off |
|-----------|---------------------|-----------|
| Group size | More TPS, higher latency | Individual tx waits longer |
| Wait timeout | More batching opportunity | Tail latency increases |
| Buffer size | Larger batches possible | Memory usage |

### 10.2 fsync Semantics Across Filesystems

**The critical question**: When does fsync actually guarantee persistence?

```
┌─────────────────────────────────────────────────────────────────────┐
│                    fsync Behavior by Filesystem                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   EXPECTED BEHAVIOR:                                                 │
│   write() → fsync() → return → DATA IS ON DISK                      │
│                                                                      │
│   ACTUAL BEHAVIOR (varies):                                          │
│                                                                      │
│   EXT4:                                                              │
│   ├── Default: Metadata journaled, data may not be                  │
│   ├── data=ordered: Data written before metadata commit             │
│   └── data=journal: Full data journaling (slowest, safest)          │
│                                                                      │
│   XFS:                                                               │
│   ├── Metadata-only journal by default                              │
│   ├── Allocating writes may expose stale data on crash              │
│   └── Use O_DSYNC or fsync for data integrity                       │
│                                                                      │
│   BTRFS:                                                             │
│   ├── COW semantics—usually safe                                     │
│   ├── But: nodatacow files bypass COW                               │
│   └── Checksum verification on read                                  │
│                                                                      │
│   ZFS:                                                               │
│   ├── Always consistent due to COW + checksums                      │
│   ├── sync=standard: fsync commits to ZIL                           │
│   └── sync=always: Every write is synchronous                       │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Critical mount options for databases**:

| Filesystem | Safe Options | Unsafe Options |
|------------|--------------|----------------|
| ext4 | `data=ordered` (default), `barrier=1` | `data=writeback`, `barrier=0` |
| XFS | `wsync`, default barriers | `nobarrier` |
| Btrfs | defaults | `nodatacow` for DB files |
| ZFS | `sync=standard` | `sync=disabled` |

### 10.3 Direct I/O vs Buffered I/O

```
┌─────────────────────────────────────────────────────────────────────┐
│                    I/O Path Comparison                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   BUFFERED I/O (default):                                           │
│                                                                      │
│   Application ──► Page Cache ──► Disk                               │
│       │              │                                               │
│       │              └── OS controls when to flush                  │
│       │                                                              │
│       └── write() returns immediately                               │
│                                                                      │
│   Pros: OS can optimize, good for read caching                      │
│   Cons: Double buffering (DB cache + OS cache), unpredictable flush │
│                                                                      │
│   ─────────────────────────────────────────────────────────────────  │
│                                                                      │
│   DIRECT I/O (O_DIRECT):                                            │
│                                                                      │
│   Application ──────────────────► Disk                              │
│       │                                                              │
│       └── Bypasses page cache entirely                              │
│                                                                      │
│   Pros: Predictable latency, no double buffering                    │
│   Cons: No OS read caching, alignment requirements                  │
│                                                                      │
│   ALIGNMENT REQUIREMENTS:                                            │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Buffer address: must be 512-byte or 4KB aligned             │   │
│   │ Offset:         must be sector-aligned                      │   │
│   │ Length:         must be multiple of sector size             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Database choices**:

| Database | I/O Mode | Reasoning |
|----------|----------|-----------|
| PostgreSQL | Buffered (default) | Relies on OS for read caching |
| MySQL/InnoDB | Direct I/O (default) | Manages own buffer pool |
| Oracle | Direct I/O | Complete control over caching |
| RocksDB | Configurable | Direct for WAL, buffered for SST reads |

### 10.4 WAL Contention on Multi-Core Systems

```
┌─────────────────────────────────────────────────────────────────────┐
│                    WAL Contention Points                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Core 0   Core 1   Core 2   Core 3                                 │
│     │        │        │        │                                     │
│     │        │        │        │                                     │
│     ▼        ▼        ▼        ▼                                     │
│   ┌─────────────────────────────────────┐                           │
│   │     WAL Buffer Lock (CONTENTION)    │  ◄── Single lock for all │
│   └─────────────────────────────────────┘                           │
│                    │                                                 │
│                    ▼                                                 │
│   ┌─────────────────────────────────────┐                           │
│   │     WAL Write Lock (SERIALIZED)     │  ◄── Single writer       │
│   └─────────────────────────────────────┘                           │
│                    │                                                 │
│                    ▼                                                 │
│   ┌─────────────────────────────────────┐                           │
│   │           fsync                     │  ◄── Disk bottleneck     │
│   └─────────────────────────────────────┘                           │
│                                                                      │
│   BOTTLENECKS:                                                       │
│   1. WAL buffer insertion lock                                       │
│   2. Log write position (single sequential pointer)                  │
│   3. fsync serialization                                             │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Mitigation strategies**:

| Strategy | How It Works | Scalability |
|----------|--------------|-------------|
| **Lock-free buffer** | CAS operations for buffer slots | ~8-16 cores |
| **Partitioned WAL** | Separate logs per partition/shard | Linear with shards |
| **Parallel group commit** | Multiple concurrent fsync groups | ~4-8x improvement |
| **Pipelining** | Overlap write and fsync of different batches | 2x throughput |
| **Autonomous commit** | Per-thread WAL segments | Near-linear |

### 10.5 NUMA Effects

Non-Uniform Memory Access creates hidden performance cliffs:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    NUMA Topology and WAL                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   ┌─────────────────────┐         ┌─────────────────────┐           │
│   │      NUMA Node 0    │         │      NUMA Node 1    │           │
│   │  ┌──────┐ ┌──────┐  │         │  ┌──────┐ ┌──────┐  │           │
│   │  │Core 0│ │Core 1│  │         │  │Core 4│ │Core 5│  │           │
│   │  └──────┘ └──────┘  │         │  └──────┘ └──────┘  │           │
│   │  ┌──────┐ ┌──────┐  │         │  ┌──────┐ ┌──────┐  │           │
│   │  │Core 2│ │Core 3│  │         │  │Core 6│ │Core 7│  │           │
│   │  └──────┘ └──────┘  │         │  └──────┘ └──────┘  │           │
│   │     ┌─────────┐     │         │     ┌─────────┐     │           │
│   │     │  Local  │     │◄───────►│     │  Local  │     │           │
│   │     │  Memory │     │   QPI   │     │  Memory │     │           │
│   │     └─────────┘     │  ~100ns │     └─────────┘     │           │
│   └─────────────────────┘  extra  └─────────────────────┘           │
│                                                                      │
│   WAL BUFFER PLACEMENT MATTERS:                                      │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL buffer on Node 0 → Core 4-7 pay 100ns penalty per access│   │
│   │ At 10M log writes/sec = 1 second of overhead per second!    │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**NUMA-aware optimizations**:

| Technique | Implementation |
|-----------|----------------|
| **Buffer interleaving** | Spread WAL buffer across all nodes |
| **Thread pinning** | Pin WAL writer thread to specific node |
| **Local allocation** | Each thread allocates from local node |
| **Replicated structures** | Read-only structures replicated per node |

### 10.6 SSD vs NVMe vs Persistent Memory

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Storage Media Characteristics                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Metric              SATA SSD    NVMe SSD    Optane PMEM           │
│   ─────────────────────────────────────────────────────────────────  │
│   Sequential Write    500 MB/s    3,000 MB/s  2,500 MB/s            │
│   4KB Random Write    50K IOPS    500K IOPS   ~1M IOPS              │
│   Write Latency       ~100μs      ~10μs       ~300ns                │
│   fsync Latency       ~1ms        ~100μs      <1μs (clflush)        │
│   Queue Depth         32          64K         N/A (byte-addr)       │
│   Parallelism         1 channel   Many lanes  Direct CPU access     │
│                                                                      │
│   IMPLICATIONS FOR WAL:                                              │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │                                                              │   │
│   │   SATA SSD: Group commit essential (1000 TPS → 50K TPS)     │   │
│   │   NVMe:     Group commit still helps, but less critical     │   │
│   │   PMEM:     Group commit overhead > benefit, skip it        │   │
│   │                                                              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 10.7 Write Amplification Analysis

Write amplification (WA) measures how many bytes are written to storage for each byte of user data:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Write Amplification Sources                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   User Write: 100 bytes                                              │
│        │                                                             │
│        ▼                                                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL Record:                                                  │   │
│   │   Header (24 bytes) + TxID (8) + LSN (8) + Before (100)     │   │
│   │   + After (100) + Padding (alignment) + CRC (4)             │   │
│   │   = ~250 bytes                                               │   │
│   │   WAL WA = 2.5x                                              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│        │                                                             │
│        ▼                                                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Full Page Write (after checkpoint):                          │   │
│   │   8KB page for 100-byte change                               │   │
│   │   FPW WA = 80x (worst case)                                  │   │
│   └─────────────────────────────────────────────────────────────┘   │
│        │                                                             │
│        ▼                                                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ SSD Internal (FTL):                                          │   │
│   │   Block erase size = 256KB-1MB                               │   │
│   │   Write 4KB → may cause 256KB rewrite                        │   │
│   │   SSD WA = 1-64x depending on fragmentation                  │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   TOTAL WA = WAL × FPW × SSD = 2.5 × 80 × 10 = 2000x (worst case)  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Reducing write amplification**:

| Technique | Reduction | Trade-off |
|-----------|-----------|-----------|
| Disable FPW after initial checkpoint | Eliminate 80x FPW factor | Risk of torn pages |
| Increase checkpoint interval | Fewer FPWs | Longer recovery |
| Use LSM instead of B-tree | Lower random write WA | Higher read WA |
| Compress WAL records | 2-5x smaller WAL | CPU overhead |
| Use larger block size | Better SSD efficiency | Wasted space |

---

## 11. Storage Layer and Filesystem Interactions

### 11.1 POSIX Durability Lies

**The uncomfortable truth**: POSIX fsync semantics don't guarantee what you think:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    fsync Guarantees (or lack thereof)               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   WHAT YOU EXPECT:                                                   │
│   fsync(fd) returns → data is on non-volatile storage               │
│                                                                      │
│   WHAT ACTUALLY HAPPENS:                                             │
│                                                                      │
│   Layer 1: Application                                               │
│            └── fsync() called                                        │
│                    │                                                 │
│   Layer 2: Filesystem                                                │
│            └── Flushes to block device... maybe                     │
│                    │                                                 │
│   Layer 3: Block Device Driver                                       │
│            └── Issues flush command... maybe                        │
│                    │                                                 │
│   Layer 4: Disk Controller/HBA                                       │
│            └── Has write-back cache... enabled by default!          │
│                    │                                                 │
│   Layer 5: Disk Firmware                                             │
│            └── May lie about completion (consumer SSDs!)            │
│                    │                                                 │
│   Layer 6: Actual Media                                              │
│            └── Finally persistent                                    │
│                                                                      │
│   KNOWN PROBLEMS:                                                    │
│   • Consumer SSDs: May acknowledge flush before complete            │
│   • Some HDDs: Ignore flush commands by default                     │
│   • Virtualization: Host may batch flushes                          │
│   • ext4 pre-4.13: Lost writes after crash in some modes            │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Defense in depth**:

| Layer | Protection |
|-------|------------|
| Application | Use O_SYNC or O_DSYNC in addition to fsync |
| Filesystem | Enable barriers (`barrier=1`) |
| Controller | Disable write-back cache OR use BBU |
| Drive | Enterprise SSDs with power-loss protection |
| Monitoring | Track `fsync` latency anomalies |

### 11.2 Journaled Filesystems vs WAL Layering

Running a database with WAL on a journaling filesystem creates layers of logging:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Double Journaling Problem                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Application Layer:                                                 │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ PostgreSQL: Write to pg_wal/                                 │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                          │                                           │
│                          ▼                                           │
│   Filesystem Layer:                                                  │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ ext4: Write to journal first, then to data blocks            │   │
│   │       (data=journal mode = DOUBLE WRITE!)                    │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                          │                                           │
│                          ▼                                           │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Total writes: 2x WAL data + metadata overhead                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   RECOMMENDATIONS:                                                   │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Use data=ordered (not data=journal) for WAL directory     │   │
│   │ • Or: Use XFS with default settings                         │   │
│   │ • Or: Use a raw device / separate simple filesystem         │   │
│   │ • Best: Dedicated WAL device with minimal filesystem        │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.3 Barriers, Flushes, and Volatile Write Caches

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Write Ordering Guarantees                        │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   WRITE BARRIER:                                                     │
│   Ensures all writes before barrier complete before any after.      │
│                                                                      │
│   Write A ───┐                                                       │
│   Write B ───┼──► BARRIER ──► Write C ───┐                          │
│   Write C ───┘               Write D ───┼──► FLUSH ──► Durable     │
│                              Write E ───┘                            │
│                                                                      │
│   CACHE FLUSH:                                                       │
│   Forces all cached writes to persistent media.                      │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ Volatile Write Cache (drive/controller)                       │  │
│   │ ┌─────┬─────┬─────┬─────┬─────┐                              │  │
│   │ │ W1  │ W2  │ W3  │ W4  │ W5  │  ─── FLUSH ──► To platters  │  │
│   │ └─────┴─────┴─────┴─────┴─────┘                              │  │
│   │                                                               │  │
│   │ If power lost before flush: W1-W5 LOST                       │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   LINUX COMMANDS:                                                    │
│   • hdparm -W0 /dev/sda  → Disable write cache                      │
│   • /sys/block/sda/queue/write_cache → Check cache status           │
│   • blockdev --flushbufs /dev/sda → Force flush                     │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.4 Mount Options That Break Durability

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Dangerous Mount Options                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   DANGEROUS (never use for databases):                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ nobarrier / barrier=0    → Write ordering not guaranteed     │   │
│   │ data=writeback          → Data may appear before metadata    │   │
│   │ async                   → Writes may be arbitrarily delayed  │   │
│   │ noatime (mostly safe)   → OK, reduces writes                 │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   SAFE CONFIGURATIONS:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │                                                              │   │
│   │ ext4:  -o data=ordered,barrier=1,noatime                    │   │
│   │ XFS:   -o noatime  (barriers on by default)                 │   │
│   │ ZFS:   sync=standard  (default is safe)                     │   │
│   │                                                              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   CHECKING CURRENT SETTINGS:                                         │
│   $ mount | grep /data                                               │
│   /dev/sda1 on /data type ext4 (rw,noatime,data=ordered)            │
│                                                                      │
│   $ cat /sys/block/sda/queue/write_cache                             │
│   write back  ← DANGEROUS without BBU                                │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 11.5 Cloud Block Storage Guarantees

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Cloud Storage Durability                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   AWS EBS:                                                           │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Volumes replicated within AZ (not across AZs)              │   │
│   │ • fsync guarantee: Yes, after EBS flush completes            │   │
│   │ • Durability: 99.999% annual durability (within AZ)          │   │
│   │ • Risk: AZ failure loses unreplicated data                   │   │
│   │ • io2 Block Express: Higher durability (99.999%)             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   GCP Persistent Disk:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Replicated within zone (regional option available)         │   │
│   │ • fsync guarantee: Yes                                       │   │
│   │ • Regional PD: Synchronous replication to 2 zones            │   │
│   │ • SSD PD: Higher IOPS, same durability                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   Azure Managed Disks:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • LRS: 3 copies within datacenter                            │   │
│   │ • ZRS: Replicated across zones (Premium SSD v2 only)        │   │
│   │ • fsync: Honored at storage layer                            │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   IMPORTANT CAVEATS:                                                 │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Instance store / local SSD: NO durability guarantee        │   │
│   │ • Network latency adds to fsync latency (~1-3ms)             │   │
│   │ • Burst credits: Performance may degrade when exhausted      │   │
│   │ • Snapshots are NOT instant (eventually consistent)          │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 12. Operational Concerns

### 12.1 Log Truncation vs Checkpoint Safety

**The problem**: WAL grows unbounded without truncation, but premature truncation causes data loss.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Safe WAL Truncation                              │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   WAL Timeline:                                                      │
│   ════════════════════════════════════════════════════════════════  │
│   │        │           │                    │             │          │
│   └────────┴───────────┴────────────────────┴─────────────┴────────  │
│   Oldest   Last        Oldest active        Last         Current   │
│   LSN      Checkpoint  Transaction          Flush        LSN       │
│                        Start LSN            LSN                     │
│                                                                      │
│   SAFE TO TRUNCATE:                                                  │
│   Everything before MIN(checkpoint LSN, oldest active tx LSN,       │
│                         replication slot LSN, archive position)     │
│                                                                      │
│   TRUNCATION TRIGGERS:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ 1. Size-based: WAL exceeds max_wal_size                      │   │
│   │ 2. Segment-based: N segments since last checkpoint          │   │
│   │ 3. Time-based: Checkpoint timeout reached                    │   │
│   │ 4. Manual: CHECKPOINT command                                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.2 Archival Strategies

```
┌─────────────────────────────────────────────────────────────────────┐
│                    WAL Archival Approaches                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   OPTION 1: archive_command (PostgreSQL)                            │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ archive_command = 'cp %p /archive/%f'                        │   │
│   │                                                              │   │
│   │ Pros: Simple, synchronous, guaranteed delivery              │   │
│   │ Cons: Blocks if archive slow/unavailable                    │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   OPTION 2: Streaming replication + archive                         │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Primary ──WAL stream──► Replica ──archive──► S3              │   │
│   │                                                              │   │
│   │ Pros: Doesn't block primary, can archive from replica       │   │
│   │ Cons: Replica must keep up                                   │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   OPTION 3: pg_receivewal / similar                                 │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Standalone process connects and archives continuously        │   │
│   │                                                              │   │
│   │ Pros: Decoupled from database, can run anywhere             │   │
│   │ Cons: Another component to monitor                           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   OPTION 4: Cloud-native (pgEdge, Neon, etc.)                       │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL written directly to object storage                       │   │
│   │                                                              │   │
│   │ Pros: Infinite retention, instant PITR                       │   │
│   │ Cons: Vendor lock-in, latency                                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.3 Online Compaction

For systems using WAL + LSM or similar structures:

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Online Compaction Strategies                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   LEVELED COMPACTION (RocksDB default):                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ L0: [SST][SST][SST][SST] → Trigger at 4 files               │   │
│   │      │                                                       │   │
│   │      ▼ compact                                               │   │
│   │ L1: [    SST    ][    SST    ] → 10x size of L0             │   │
│   │      │                                                       │   │
│   │      ▼ compact                                               │   │
│   │ L2: [SST][SST][SST][SST][SST] → 10x size of L1              │   │
│   │                                                              │   │
│   │ Pros: Bounded space amp, predictable read amp               │   │
│   │ Cons: High write amp (10-30x)                               │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   TIERED/UNIVERSAL COMPACTION:                                       │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Merge all SSTables of similar size together                  │   │
│   │                                                              │   │
│   │ Pros: Lower write amp (2-4x)                                │   │
│   │ Cons: Higher space amp (2x), longer compactions             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   FIFO COMPACTION (time-series):                                    │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Just delete oldest SST when max size reached                 │   │
│   │                                                              │   │
│   │ Pros: No compaction overhead, predictable                   │   │
│   │ Cons: Only works for append-only/TTL workloads              │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.4 Long-Running Transactions Blocking Truncation

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Long Transaction Impact                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Normal operation (transactions commit quickly):                    │
│   ═══════════════════════════════════════════════════════           │
│   │    │    │    │    │    │    │    │    │    │                    │
│   └────┴────┴────┴────┴────┴────┴────┴────┴────┴───────────────     │
│   Checkpoint ─────────────────────────► Truncate here               │
│                                                                      │
│   Long-running transaction (1 hour old):                            │
│   ═══════════════════════════════════════════════════════           │
│   │                                                      │          │
│   └──────────────────────────────────────────────────────┴───────   │
│   Long tx start ◄──────── WAL CANNOT BE TRUNCATED ──────► Now      │
│                                                                      │
│   CONSEQUENCES:                                                      │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • WAL grows unbounded                                        │   │
│   │ • Disk fills up                                              │   │
│   │ • Recovery time increases (more WAL to replay)               │   │
│   │ • Replication slots may become inactive                      │   │
│   │ • Vacuum cannot clean up dead rows (in PostgreSQL)           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   MONITORING:                                                        │
│   -- PostgreSQL                                                      │
│   SELECT pid, now() - xact_start AS duration, query                 │
│   FROM pg_stat_activity                                              │
│   WHERE state != 'idle' AND xact_start < now() - interval '5 min';  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.5 Backpressure When WAL Grows Unbounded

```
┌─────────────────────────────────────────────────────────────────────┐
│                    WAL Backpressure Mechanisms                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   APPROACH 1: Block writes (PostgreSQL)                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ max_wal_size reached → Force checkpoint                      │   │
│   │ max_wal_size × 2 reached → PANIC (prevent disk full)        │   │
│   │                                                              │   │
│   │ Effect: Write queries block until checkpoint completes       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   APPROACH 2: Throttle writes (RocksDB)                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ L0 files > soft_pending_compaction_bytes_limit               │   │
│   │     → Slow down writes proportionally                        │   │
│   │                                                              │   │
│   │ L0 files > hard_pending_compaction_bytes_limit               │   │
│   │     → Stall all writes                                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   APPROACH 3: Reject writes (application level)                     │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Monitor WAL lag → Return 503 to clients when critical        │   │
│   │                                                              │   │
│   │ Better UX: Fast failure vs. slow timeout                     │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   METRICS TO MONITOR:                                                │
│   • WAL generation rate (MB/s)                                       │
│   • WAL size (current vs max)                                        │
│   • Checkpoint frequency and duration                                │
│   • Oldest transaction age                                           │
│   • Replication lag (if applicable)                                  │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 12.6 Operational Tooling for WAL Visibility

```
┌─────────────────────────────────────────────────────────────────────┐
│                    WAL Observability Tools                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   POSTGRESQL:                                                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ pg_waldump          → Decode WAL records (offline)           │   │
│   │ pg_stat_wal         → WAL statistics (PG14+)                 │   │
│   │ pg_stat_archiver    → Archive status                         │   │
│   │ pg_ls_waldir()      → List WAL files                         │   │
│   │ pg_current_wal_lsn() → Current write position                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   MYSQL:                                                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ SHOW BINARY LOGS         → List binlog files                 │   │
│   │ SHOW MASTER STATUS       → Current binlog position           │   │
│   │ mysqlbinlog              → Decode binlog (offline)           │   │
│   │ performance_schema       → I/O and sync statistics           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   ROCKSDB:                                                           │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ ldb dump_wal            → Decode WAL files                   │   │
│   │ Statistics::getTickerCount → WAL sync/write counts           │   │
│   │ GetLiveFilesMetaData    → SST + WAL file listing             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   KEY METRICS TO EXPORT:                                             │
│   • wal_bytes_written_total (counter)                                │
│   • wal_syncs_total (counter)                                        │
│   • wal_sync_duration_seconds (histogram)                            │
│   • wal_size_bytes (gauge)                                           │
│   • oldest_transaction_age_seconds (gauge)                           │
│   • checkpoint_duration_seconds (histogram)                          │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 13. WAL as Replication Protocol

### 13.1 Physical vs Logical WAL

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Physical vs Logical Replication                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   PHYSICAL WAL:                                                      │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Record: "Write bytes 0xDEADBEEF at page 42, offset 128"      │   │
│   │                                                              │   │
│   │ Pros:                                                        │   │
│   │   • Exact byte-for-byte replica                              │   │
│   │   • Simple: just replay bytes                                │   │
│   │   • Supports all data types and operations                   │   │
│   │                                                              │   │
│   │ Cons:                                                        │   │
│   │   • Major version upgrade requires full sync                 │   │
│   │   • Can't replicate to different architecture                │   │
│   │   • Can't filter by table/database                           │   │
│   │   • Large volume (full page writes)                          │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   LOGICAL WAL:                                                       │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Record: "INSERT INTO users (id, name) VALUES (42, 'Alice')"  │   │
│   │                                                              │   │
│   │ Pros:                                                        │   │
│   │   • Cross-version replication                                │   │
│   │   • Table/database filtering                                 │   │
│   │   • Smaller payload (no page images)                         │   │
│   │   • Can replicate to different databases (CDC)               │   │
│   │                                                              │   │
│   │ Cons:                                                        │   │
│   │   • Requires primary key for updates/deletes                 │   │
│   │   • DDL usually not replicated                               │   │
│   │   • Sequences/large objects need special handling            │   │
│   │   • Decoding overhead                                        │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 13.2 Causal Ordering Guarantees

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Ordering in Replicated Logs                      │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   TOTAL ORDER (Raft, Paxos):                                        │
│   All nodes see exactly the same sequence:                          │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ Node 1: [A][B][C][D][E]                                      │  │
│   │ Node 2: [A][B][C][D][E]                                      │  │
│   │ Node 3: [A][B][C][D][E]                                      │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   CAUSAL ORDER (weaker, higher availability):                       │
│   Causally related operations ordered, concurrent ops may differ:   │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ If A happened-before B, all nodes see A before B             │  │
│   │ If A and C are concurrent, nodes may see A→C or C→A          │  │
│   │                                                              │  │
│   │ Node 1: [A][B][C][D]                                         │  │
│   │ Node 2: [A][C][B][D]  ← Different order for concurrent B,C   │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   EVENTUAL (no ordering guarantee):                                  │
│   All updates eventually applied, order varies:                     │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ Node 1: [A][B][C]                                            │  │
│   │ Node 2: [B][A][C]                                            │  │
│   │ Node 3: [C][A][B]                                            │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   REQUIREMENTS BY USE CASE:                                          │
│   • Financial: Total order (or serializability)                     │
│   • Social feeds: Causal order (comments after posts)               │
│   • Counters/analytics: Eventual (CRDTs)                            │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 13.3 Follower Lag Management

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Replication Lag Strategies                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   LAG CAUSES:                                                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Network latency/bandwidth                                  │   │
│   │ • Replica disk slower than primary                           │   │
│   │ • Replica CPU bottleneck (single-threaded replay)            │   │
│   │ • Large transactions (must apply atomically)                 │   │
│   │ • Replica doing heavy read queries                           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   MITIGATION STRATEGIES:                                             │
│                                                                      │
│   1. PARALLEL REPLAY                                                 │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ MySQL 5.7+: slave_parallel_workers                           │   │
│   │ PostgreSQL: Doesn't support parallel replay (as of PG16)     │   │
│   │                                                              │   │
│   │ Parallelism modes:                                           │   │
│   │ • By database: Different DBs replay in parallel              │   │
│   │ • By transaction: Non-conflicting txs in parallel            │   │
│   │ • By table: Write-set based parallelism                      │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   2. READ ROUTING                                                    │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Route reads based on acceptable lag:                         │   │
│   │                                                              │   │
│   │ if (read.consistency == "strong"):                          │   │
│   │     route_to_primary()                                       │   │
│   │ elif (read.max_lag < replica.current_lag):                   │   │
│   │     route_to_primary()  # or wait                            │   │
│   │ else:                                                        │   │
│   │     route_to_replica()                                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   3. SYNCHRONOUS REPLICATION                                         │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Wait for replica acknowledgment before commit                │   │
│   │                                                              │   │
│   │ Levels (PostgreSQL):                                         │   │
│   │ • remote_write: Replica received (not necessarily applied)   │   │
│   │ • on: Replica flushed to disk                               │   │
│   │ • remote_apply: Replica applied (visible to queries)         │   │
│   │                                                              │   │
│   │ Trade-off: Zero lag vs. commit latency                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 13.4 Split-Brain Implications

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Split-Brain Scenarios                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   SPLIT-BRAIN: Multiple nodes believe they are primary              │
│                                                                      │
│   ┌─────────────────────┐     ┌─────────────────────┐               │
│   │     Network A       │     │     Network B       │               │
│   │  ┌─────────────┐    │     │    ┌─────────────┐  │               │
│   │  │  Primary A  │    │     │    │  Primary B  │  │               │
│   │  │  (original) │    │ ✗✗✗ │    │  (promoted) │  │               │
│   │  └──────┬──────┘    │     │    └──────┬──────┘  │               │
│   │         │           │     │           │         │               │
│   │  ┌──────▼──────┐    │     │    ┌──────▼──────┐  │               │
│   │  │  Clients    │    │     │    │  Clients    │  │               │
│   │  │  (writing)  │    │     │    │  (writing)  │  │               │
│   │  └─────────────┘    │     │    └─────────────┘  │               │
│   └─────────────────────┘     └─────────────────────┘               │
│                                                                      │
│   RESULT: Divergent WAL histories, data loss on merge               │
│                                                                      │
│   PREVENTION MECHANISMS:                                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ 1. FENCING (STONITH - Shoot The Other Node In The Head)     │   │
│   │    • Old primary killed before new primary activated         │   │
│   │    • Requires reliable fencing mechanism (IPMI, PDU)         │   │
│   │                                                              │   │
│   │ 2. QUORUM-BASED CONSENSUS                                    │   │
│   │    • Majority required to elect leader                       │   │
│   │    • Minority partition cannot elect new leader              │   │
│   │                                                              │   │
│   │ 3. LEASE-BASED LEADERSHIP                                    │   │
│   │    • Leader must renew lease periodically                    │   │
│   │    • Lease expires → leader steps down                       │   │
│   │    • Requires synchronized clocks (NTP)                      │   │
│   │                                                              │   │
│   │ 4. EXTERNAL ARBITER                                          │   │
│   │    • Third-party decides who is primary (etcd, ZK)           │   │
│   │    • Single point of failure unless HA                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 13.5 Exactly-Once vs At-Least-Once WAL Replay

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Delivery Guarantees in Replication               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   AT-MOST-ONCE:                                                      │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Send record, don't retry on failure                          │   │
│   │ → May lose data                                              │   │
│   │ Used: UDP, non-critical telemetry                           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   AT-LEAST-ONCE:                                                     │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Retry until acknowledged                                     │   │
│   │ → May duplicate data                                         │   │
│   │                                                              │   │
│   │ Primary: Send WAL record                                     │   │
│   │ Replica: Apply... crash before ACK                           │   │
│   │ Primary: Retry (record applied TWICE)                        │   │
│   │                                                              │   │
│   │ SOLUTION: Idempotent replay (LSN-based skip)                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   EXACTLY-ONCE (effectively):                                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ At-least-once + idempotent consumer = effectively once       │   │
│   │                                                              │   │
│   │ Techniques:                                                  │   │
│   │ • LSN tracking (skip if already applied)                    │   │
│   │ • Transactional outbox (apply + ack atomically)             │   │
│   │ • Deduplication table (remember processed IDs)              │   │
│   │                                                              │   │
│   │ Kafka example:                                               │   │
│   │   enable.idempotence=true                                    │   │
│   │   transactional.id=my-txn-id                                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 13.6 Rewind After Failover

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Timeline Divergence and Rewind                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   SCENARIO: Primary fails, replica promoted, old primary recovers   │
│                                                                      │
│   Timeline BEFORE failover:                                          │
│   Primary:  ═══════════════════════════════════► LSN 1000           │
│   Replica:  ══════════════════════════► LSN 950 (lagging)           │
│                                                                      │
│   Timeline AFTER failover:                                           │
│   Old Primary: ═════════════════════════════════► LSN 1000 (dead)   │
│   New Primary: ═══════════════════════════════════► LSN 1050        │
│                                           ▲                          │
│                                     Promoted at 950                  │
│                                                                      │
│   THE PROBLEM:                                                       │
│   Old primary has LSN 951-1000 that new primary doesn't have        │
│   These timelines have DIVERGED                                      │
│                                                                      │
│   REWIND PROCESS (pg_rewind):                                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ 1. Find divergence point (LSN 950)                           │   │
│   │ 2. Copy changed blocks from new primary since divergence     │   │
│   │ 3. Replay new primary's WAL from divergence point            │   │
│   │ 4. Old primary now consistent with new primary               │   │
│   │                                                              │   │
│   │ REQUIREMENT: wal_log_hints=on OR data checksums enabled      │   │
│   │ (needed to detect which blocks changed)                      │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   ALTERNATIVES:                                                      │
│   • Full resync (pg_basebackup) - slower but simpler                │
│   • Keep old primary read-only (forensics)                          │
│   • Accept divergent history (rare, dangerous)                      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 14. Beyond Traditional WAL

### 14.1 In-Memory-First Systems

```
┌─────────────────────────────────────────────────────────────────────┐
│                    In-Memory Database Durability                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   TRADITIONAL (disk-first):                                          │
│   Write → WAL → Disk → ACK → Apply to memory                        │
│                                                                      │
│   IN-MEMORY-FIRST:                                                   │
│   Write → Apply to memory → ACK → Async persist                     │
│                                                                      │
│   DURABILITY OPTIONS:                                                │
│                                                                      │
│   1. NO DURABILITY (pure in-memory)                                 │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Redis (default), Memcached                                   │   │
│   │ • Restart = data loss                                        │   │
│   │ • Use case: Caching, sessions                                │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   2. PERIODIC SNAPSHOTS                                              │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Redis RDB                                                    │   │
│   │ • Fork + serialize state every N seconds                     │   │
│   │ • Lose up to snapshot interval of data                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   3. WAL + SNAPSHOTS                                                 │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Redis AOF, VoltDB                                            │   │
│   │ • Append operations to log                                   │   │
│   │ • Periodic snapshot for fast recovery                        │   │
│   │ • fsync options: always, everysec, no                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   4. SYNCHRONOUS REPLICATION                                         │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ MemSQL/SingleStore, VoltDB                                   │   │
│   │ • Replicate to multiple nodes before ACK                     │   │
│   │ • Durability via redundancy, not disk                        │   │
│   │ • Fast recovery (data already in memory elsewhere)           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 14.2 Append-Only + Snapshot Hybrids

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Hybrid Durability Models                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   APPEND-ONLY LOG + PERIODIC SNAPSHOT:                               │
│                                                                      │
│   ┌──────────────────────────────────────────────────────────────┐  │
│   │ Log: [op1][op2][op3][op4][SNAPSHOT_MARKER][op5][op6][op7]    │  │
│   │                              │                                │  │
│   │                              ▼                                │  │
│   │                    Snapshot file (full state)                 │  │
│   │                                                              │  │
│   │ Recovery:                                                    │  │
│   │ 1. Load latest snapshot                                      │  │
│   │ 2. Replay ops after snapshot marker                          │  │
│   │ 3. Done (much faster than full replay)                       │  │
│   └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│   USED BY:                                                           │
│   • Redis (AOF + RDB)                                                │
│   • etcd (WAL + snapshot)                                            │
│   • Kafka (log + compaction as pseudo-snapshot)                      │
│   • Event sourcing systems                                           │
│                                                                      │
│   SNAPSHOT STRATEGIES:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Copy-on-write fork (Redis): Fast, but 2x memory spike     │   │
│   │ • Incremental (etcd): Smaller, more frequent                │   │
│   │ • Background thread: No fork, but consistency complexity    │   │
│   │ • Log compaction: Merge old entries, keep latest per key    │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 14.3 CRDT-Based Systems

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CRDTs and Durability                             │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   CRDT (Conflict-free Replicated Data Type):                        │
│   Data structures that automatically resolve conflicts              │
│                                                                      │
│   WHY DIFFERENT FROM WAL:                                            │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL approach: Total order required, conflicts are errors     │   │
│   │ CRDT approach: Any order works, conflicts auto-resolve       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   EXAMPLE - G-Counter:                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Node A increments: {A: 5, B: 0}                              │   │
│   │ Node B increments: {A: 0, B: 3}                              │   │
│   │                                                              │   │
│   │ Merge (max per node): {A: 5, B: 3}                           │   │
│   │ Value = sum = 8                                              │   │
│   │                                                              │   │
│   │ No log needed—just merge states!                             │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   DURABILITY IN CRDT SYSTEMS:                                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Option 1: Persist CRDT state periodically                    │   │
│   │   • Lose operations since last persist                       │   │
│   │   • But: other nodes have the operations                     │   │
│   │                                                              │   │
│   │ Option 2: Log operations + periodic state snapshot           │   │
│   │   • Full durability                                          │   │
│   │   • Replay applies operations idempotently                   │   │
│   │                                                              │   │
│   │ Option 3: Rely on replication                                │   │
│   │   • N nodes, tolerate N-1 failures                           │   │
│   │   • No local durability needed                               │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   USED BY: Riak, Redis CRDTs, Automerge, Yjs                        │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 14.4 Log-Free Persistent Memory Designs

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Persistent Memory Architecture                   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   TRADITIONAL (with WAL):                                            │
│   CPU ──► DRAM ──► WAL (NVMe) ──► Data pages (NVMe)                 │
│                                                                      │
│   PMEM (log-free possible):                                          │
│   CPU ──► Persistent Memory (byte-addressable, durable)             │
│                                                                      │
│   WHY LOG-FREE IS POSSIBLE:                                          │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • 8-byte aligned writes are atomic on x86                    │   │
│   │ • CLWB + SFENCE guarantees persistence                       │   │
│   │ • No torn writes at cache line granularity (64 bytes)        │   │
│   │ • Direct CPU access—no filesystem layer                      │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   LOG-FREE DATA STRUCTURES:                                          │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ PMEM-optimized B+Tree:                                       │   │
│   │ • 8-byte pointer swap for atomic tree modification           │   │
│   │ • Failure-atomic in-place updates                            │   │
│   │ • No WAL needed for single-key operations                    │   │
│   │                                                              │   │
│   │ PMEM Hash Table:                                             │   │
│   │ • Compare-and-swap for atomic insert                         │   │
│   │ • Resize via shadow table + pointer swap                     │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   WHEN WAL STILL NEEDED WITH PMEM:                                   │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Multi-key transactions (atomicity across objects)          │   │
│   │ • Replication (need log for shipping changes)                │   │
│   │ • Point-in-time recovery                                     │   │
│   │ • Audit requirements                                         │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   RESEARCH SYSTEMS: PMDK, SAP HANA, Intel Optane-optimized DBs      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 14.5 Latency-Sensitive Workloads

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Ultra-Low-Latency Approaches                     │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   LATENCY BUDGET BREAKDOWN (p99):                                    │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Target: 1ms total latency                                    │   │
│   │                                                              │   │
│   │ Network round trip:     200μs                                │   │
│   │ Query parsing:           50μs                                │   │
│   │ Query execution:        200μs                                │   │
│   │ WAL write + fsync:      500μs  ← DOMINANT FACTOR            │   │
│   │ Response serialization:  50μs                                │   │
│   │                                                              │   │
│   │ Problem: fsync alone exceeds budget!                         │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   TECHNIQUES:                                                        │
│                                                                      │
│   1. PMEM FOR WAL TAIL                                               │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ WAL write to PMEM: 0.3μs (vs 500μs for NVMe)                │   │
│   │ Background drain PMEM → NVMe for capacity                   │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   2. SYNCHRONOUS REPLICATION INSTEAD OF DISK                        │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ Network ACK from replica: 100-200μs (in same datacenter)    │   │
│   │ Faster than local disk fsync!                               │   │
│   │ Durability via redundancy                                    │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   3. ASYNC COMMIT WITH BOUNDED LOSS                                  │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ ACK immediately, fsync in background                         │   │
│   │ Accept up to N ms of potential data loss                     │   │
│   │ Application must tolerate this                               │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   4. HARDWARE ACCELERATION                                           │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ FPGA/SmartNIC offload for log append                        │   │
│   │ RDMA for zero-copy replication                               │   │
│   │ Kernel bypass (DPDK, SPDK)                                  │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 14.6 Cost-of-Recovery Trade-offs

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Recovery Time vs Write Performance               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   THE FUNDAMENTAL TRADE-OFF:                                         │
│                                                                      │
│   Fast Recovery ◄─────────────────────────────────► Fast Writes     │
│   (frequent checkpoints)                    (infrequent checkpoints) │
│                                                                      │
│   CHECKPOINT FREQUENCY IMPACT:                                       │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │ Checkpoint Interval │ Recovery Time │ Write Overhead       │    │
│   │─────────────────────│───────────────│─────────────────────│    │
│   │ 1 minute            │ ~1 minute     │ High (many FPW)      │    │
│   │ 5 minutes           │ ~5 minutes    │ Moderate             │    │
│   │ 30 minutes          │ ~30 minutes   │ Low                  │    │
│   │ Never (crash only)  │ Hours         │ Minimal              │    │
│   └────────────────────────────────────────────────────────────┘    │
│                                                                      │
│   STRATEGIES BY USE CASE:                                            │
│                                                                      │
│   HIGH AVAILABILITY (minimize recovery):                             │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Frequent checkpoints                                       │   │
│   │ • Hot standby (recovery happens on replica)                  │   │
│   │ • Multi-node consensus (no single-node recovery)             │   │
│   │ • Parallel recovery (if supported)                           │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   HIGH THROUGHPUT (minimize write overhead):                         │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Infrequent checkpoints                                     │   │
│   │ • Accept longer recovery time                                │   │
│   │ • Batch jobs can tolerate restart delays                     │   │
│   │ • Incremental checkpoints if available                       │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   BALANCED:                                                          │
│   ┌─────────────────────────────────────────────────────────────┐   │
│   │ • Checkpoint on WAL size threshold                           │   │
│   │ • Spread checkpoint I/O over time                            │   │
│   │ • Monitor recovery time in DR drills                         │   │
│   │ • Adjust based on actual failure frequency                   │   │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                      │
│   POSTGRESQL TUNING:                                                 │
│   checkpoint_timeout = 5min          -- Max time between checkpoints │
│   max_wal_size = 1GB                 -- Trigger checkpoint on size  │
│   checkpoint_completion_target = 0.9 -- Spread I/O over 90% of time │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Summary: Key Takeaways

1. **WAL is the foundation** of durability in databases. Understand it deeply before building distributed systems.

2. **ARIES protocol** (steal/no-force with repeating history) is the industry standard for good reason—it provides the best balance of performance and correctness.

3. **LSM trees are not a replacement for WAL**—they still use WAL for memtable durability. They're a different indexing structure optimized for writes.

4. **Group commit is critical** for production throughput. Single-transaction fsync limits you to ~100-1000 TPS.

5. **Async commit is a durability trade-off**, not a performance optimization. Understand exactly what you're giving up.

6. **Modern hardware (NVMe, PMEM) changes the calculus**. Many traditional optimizations (group commit) become less important.

7. **Distributed consensus = replicated WAL**. Raft, Paxos, Zab are all fundamentally about agreeing on a log order.

8. **Event sourcing is WAL for the application layer**. Different purpose, different retention, same fundamental pattern.

---

## References

- [PostgreSQL WAL Documentation](https://www.postgresql.org/docs/current/wal-intro.html)
- [ARIES Paper (Stanford)](https://web.stanford.edu/class/cs345d-01/rl/aries.pdf)
- [Write-Ahead Logging - Wikipedia](https://en.wikipedia.org/wiki/Write-ahead_logging)
- [The Storage Wars: Shadow Paging, LSM, and WAL](https://ayende.com/blog/163393/the-storage-wars-shadow-paging-log-structured-merge-and-write-ahead-logging)
- [B-Tree vs LSM-Tree - TiKV](https://tikv.org/deep-dive/key-value-engine/b-tree-vs-lsm/)
- [Raft Consensus Algorithm](https://raft.github.io/)
- [Moving Beyond Group Commit - ACM 2025](https://dl.acm.org/doi/10.1145/3725328)
- [Modern NVMe Storage - VLDB](https://vldb.org/pvldb/vol16/p2090-haas.pdf)
- [Event Sourcing Pattern - Microsoft](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
- [CQRS Pattern - Microsoft](https://learn.microsoft.com/en-us/azure/architecture/patterns/cqrs)
