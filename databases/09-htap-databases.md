# HTAP Databases: A Staff-Engineer Deep Dive

## Executive Summary

Hybrid Transactional/Analytical Processing (HTAP) databases unify OLTP and OLAP workloads into a single system, eliminating the traditional ETL pipeline between operational databases and data warehouses. This document covers architecture patterns, deep dives into major HTAP systems (TiDB, Spanner, CockroachDB, SingleStore, SAP HANA, AlloyDB), trade-offs, and a decision framework for when HTAP is the right choice.

---

## Table of Contents

1. [The HTAP Vision](#1-the-htap-vision)
2. [HTAP Architecture Patterns](#2-htap-architecture-patterns)
3. [TiDB Deep Dive](#3-tidb-deep-dive)
4. [Google Spanner + AlloyDB](#4-google-spanner--alloydb)
5. [CockroachDB](#5-cockroachdb)
6. [SingleStore (formerly MemSQL)](#6-singlestore-formerly-memsql)
7. [SAP HANA](#7-sap-hana)
8. [F1 and Spanner at Google](#8-f1-and-spanner-at-google)
9. [HTAP Challenges and Trade-offs](#9-htap-challenges-and-trade-offs)
10. [HTAP Decision Framework](#10-htap-decision-framework)
11. [HTAP Comparison Table](#11-htap-comparison-table)

---

## 1. The HTAP Vision

### The Traditional Split: OLTP + ETL + Data Warehouse

For decades, the industry standard has been a strict separation between transactional and analytical workloads:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Traditional OLTP / OLAP Split                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────┐     ┌──────────────┐     ┌──────────────────────┐    │
│  │              │     │              │     │                      │    │
│  │  Application │────►│  OLTP DB     │     │   Data Warehouse     │    │
│  │  (writes)    │     │  (MySQL,     │     │   (Redshift,         │    │
│  │              │     │   Postgres)  │     │    Snowflake,        │    │
│  └──────────────┘     │              │     │    BigQuery)         │    │
│                       │  Row-store   │     │                      │    │
│                       │  Normalized  │     │   Column-store       │    │
│                       │  B-tree idx  │     │   Denormalized       │    │
│                       └──────┬───────┘     │   Bitmap/zone maps   │    │
│                              │             └──────────▲───────────┘    │
│                              │                        │                │
│                              │    ┌───────────────┐   │                │
│                              └───►│  ETL Pipeline  │──┘                │
│                                   │               │                    │
│                                   │  - Extract    │                    │
│                                   │  - Transform  │                    │
│                                   │  - Load       │                    │
│                                   │               │                    │
│                                   │  Runs every   │                    │
│                                   │  1-24 hours   │                    │
│                                   └───────────────┘                    │
│                                                                         │
│  Analytics Dashboard reads from warehouse with HOURS of data lag        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why This Split Is Painful

| Problem | Description | Business Impact |
|---------|-------------|-----------------|
| **Data staleness** | ETL runs hourly/daily; dashboards show stale data | Fraud detected hours late; inventory out of sync |
| **ETL complexity** | Schema mapping, type coercion, dedup, error handling | 40-60% of data engineering time spent on ETL |
| **Infrastructure cost** | Two full database clusters + ETL compute + orchestration | 2-3x infrastructure spend vs single system |
| **Schema drift** | OLTP schema changes break ETL pipelines silently | Broken dashboards, silent data loss |
| **Operational burden** | Monitor two systems, two backup strategies, two teams | Higher headcount, on-call complexity |
| **Data consistency** | ETL snapshots may capture partial transactions | Analytics reports don't reconcile with operations |

### The HTAP Promise

HTAP databases aim to serve both workloads from a single system:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          HTAP Architecture                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────┐     ┌──────────────────────────────────────────┐     │
│  │              │     │           HTAP Database                   │     │
│  │  Application │────►│                                          │     │
│  │  (writes)    │     │  ┌────────────┐    ┌─────────────────┐   │     │
│  └──────────────┘     │  │  Row Store │───►│  Column Store    │   │     │
│                       │  │  (OLTP)    │    │  (OLAP)          │   │     │
│  ┌──────────────┐     │  │            │    │                  │   │     │
│  │  Analytics   │────►│  │  Point     │    │  Full scans      │   │     │
│  │  Dashboard   │     │  │  lookups   │    │  Aggregations    │   │     │
│  │  (reads)     │     │  │  Txns      │    │  Joins           │   │     │
│  └──────────────┘     │  └────────────┘    └─────────────────┘   │     │
│                       │                                          │     │
│                       │  Smart query router decides format       │     │
│                       │  Data freshness: seconds, not hours      │     │
│                       └──────────────────────────────────────────┘     │
│                                                                         │
│  NO ETL pipeline. Analytics runs on live, fresh transactional data.     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Gartner's HTAP Definition

Gartner coined the term HTAP in 2014, defining it as:

> "An application architecture that breaks down the wall between transaction processing and analytics. It enables more informed and 'in-business-process' decision making."

Key properties of a true HTAP system:

1. **Single data store** -- no separate copy for analytics
2. **Real-time freshness** -- analytics sees committed transactions within seconds
3. **Workload isolation** -- OLAP queries don't degrade OLTP latency
4. **Transactional guarantees** -- full ACID on the OLTP side
5. **Analytical performance** -- competitive with purpose-built OLAP systems

### Fresh Data Analytics Without ETL Lag

The value proposition is measured in data freshness:

```
Data Freshness Spectrum
────────────────────────────────────────────────────────────────────

  Traditional ETL          CDC/Streaming         HTAP
  ┌──────────┐            ┌──────────┐         ┌──────────┐
  │ 1-24 hrs │            │ 1-60 sec │         │ < 1 sec  │
  │  stale   │            │  delay   │         │  fresh   │
  └──────────┘            └──────────┘         └──────────┘
       │                       │                    │
       │  Batch ETL            │  Kafka/Debezium    │  In-engine
       │  (Airflow,            │  + streaming       │  replication
       │   dbt,                │  warehouse         │  (Raft learner,
       │   Informatica)        │                    │   delta store)
       │                       │                    │
       ▼                       ▼                    ▼
  "What happened             "What happened      "What is happening
   yesterday?"                a minute ago?"       right now?"
```

Real-world use cases demanding fresh analytics:

- **Fraud detection**: Must analyze transaction patterns in real time
- **Inventory management**: Stock levels must be current for order acceptance
- **Dynamic pricing**: Price optimization needs current demand signals
- **Operational dashboards**: SRE metrics, order pipeline, logistics tracking
- **Personalization**: Real-time recommendation based on current session + history

---

## 2. HTAP Architecture Patterns

### Pattern 1: Dual-Format Storage

The most common HTAP approach: maintain both row-oriented and column-oriented representations of the same data within one database engine.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                   Pattern 1: Dual-Format Storage                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│                        ┌──────────────────┐                             │
│                        │   SQL Layer       │                             │
│                        │   Query Optimizer │                             │
│                        └────────┬─────────┘                             │
│                                 │                                       │
│                    ┌────────────┴────────────┐                          │
│                    │    Query Router          │                          │
│                    │    (cost-based decision) │                          │
│                    └────┬──────────────┬──────┘                         │
│                         │              │                                │
│              OLTP path  │              │  OLAP path                     │
│                         ▼              ▼                                │
│              ┌──────────────┐  ┌──────────────────┐                    │
│              │  Row Store   │  │  Column Store     │                    │
│              │              │  │                   │                    │
│              │  B-tree      │  │  Columnar files   │                    │
│              │  indexes     │  │  Zone maps        │                    │
│              │              │  │  Compression       │                   │
│              │  Hot data    │  │  Vectorized exec  │                    │
│              │  Random I/O  │  │  Sequential I/O   │                    │
│              └──────┬───────┘  └───────▲──────────┘                    │
│                     │                  │                                │
│                     │   Async Sync     │                                │
│                     │   (Raft learner, │                                │
│                     │    background    │                                │
│                     │    merge)        │                                │
│                     └──────────────────┘                                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**How data synchronization works:**

1. **Write path**: All writes go to the row store (optimized for single-row operations)
2. **Async replication**: A background process converts row data to columnar format
3. **Consistency**: The columnar replica either uses log-based replication (Raft learner in TiDB) or background merge (SQL Server columnstore)
4. **Read path**: The optimizer decides which store to read from based on query pattern

**Examples:**

| System | Row Store | Column Store | Sync Mechanism |
|--------|-----------|-------------|----------------|
| TiDB | TiKV (LSM-tree KV) | TiFlash (Delta Tree) | Raft learner replication |
| SQL Server | Rowstore heap/B-tree | Columnstore index | Background tuple mover |
| Oracle | In-memory row format | In-Memory Column Store | Background population |

**SQL Server dual-format example:**

```sql
-- Create a table with both row and column storage
CREATE TABLE orders (
    order_id     BIGINT PRIMARY KEY CLUSTERED,  -- B-tree rowstore
    customer_id  INT NOT NULL,
    order_date   DATE NOT NULL,
    total_amount DECIMAL(18,2),
    status       VARCHAR(20)
);

-- Add a nonclustered columnstore index for analytics
CREATE NONCLUSTERED COLUMNSTORE INDEX ix_orders_analytics
ON orders (customer_id, order_date, total_amount, status);

-- OLTP query -> optimizer uses B-tree rowstore
SELECT * FROM orders WHERE order_id = 12345;

-- OLAP query -> optimizer uses columnstore index
SELECT
    customer_id,
    YEAR(order_date) AS order_year,
    SUM(total_amount) AS total_spend,
    COUNT(*) AS order_count
FROM orders
GROUP BY customer_id, YEAR(order_date)
HAVING SUM(total_amount) > 10000;
```

### Pattern 2: In-Memory with Columnar

Keep the entire working dataset in memory, using columnar format for analytical acceleration. The delta store absorbs writes in row format, then merges into the main columnar store.

```
┌─────────────────────────────────────────────────────────────────────────┐
│              Pattern 2: In-Memory with Columnar                         │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│                    ┌─────────────────────────────┐                      │
│                    │         DRAM                 │                      │
│                    │                              │                      │
│                    │  ┌───────────┐ ┌──────────┐ │                      │
│                    │  │ Delta     │ │ Main     │ │                      │
│                    │  │ Store    ─┼─►  Store   │ │                      │
│                    │  │ (row)    │ │ (column) │ │                      │
│                    │  │          │ │          │ │                      │
│                    │  │ Recent   │ │ Bulk     │ │                      │
│                    │  │ inserts  │ │ data     │ │                      │
│                    │  │ updates  │ │ compressed│ │                      │
│                    │  └───────────┘ └──────────┘ │                      │
│                    │       │              │       │                      │
│                    │       │    Merge     │       │                      │
│                    │       └──────────────┘       │                      │
│                    │                              │                      │
│                    └──────────────┬───────────────┘                      │
│                                  │                                      │
│                           ┌──────▼───────┐                              │
│                           │  Persistent  │                              │
│                           │  Storage     │                              │
│                           │  (logs,      │                              │
│                           │   savepoints,│                              │
│                           │   cold data) │                              │
│                           └──────────────┘                              │
│                                                                         │
│  Writes   --> Delta Store (row format, fast random writes)              │
│  Merge    --> Background compaction into Main Store (columnar)          │
│  OLTP     --> Delta Store lookup + Main Store point lookup              │
│  OLAP     --> Main Store columnar scan (vectorized, SIMD)              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Examples:**

- **SAP HANA**: Pure in-memory columnar with delta/main merge
- **SingleStore (MemSQL)**: In-memory rowstore + disk-based columnstore

**Advantages**: Extreme speed for both workloads when data fits in memory.
**Disadvantages**: Cost of DRAM, memory pressure under concurrent load, cold-start time on restart.

### Pattern 3: Distributed SQL with Analytics Extensions

Start with a distributed OLTP database and add analytical query capabilities (follower reads, changefeeds, vectorized execution).

```
┌─────────────────────────────────────────────────────────────────────────┐
│         Pattern 3: Distributed SQL + Analytics Extensions               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│    ┌──────────┐  ┌──────────┐  ┌──────────┐                            │
│    │  SQL     │  │  SQL     │  │  SQL     │   Stateless SQL nodes      │
│    │  Gateway │  │  Gateway │  │  Gateway │                            │
│    └────┬─────┘  └────┬─────┘  └────┬─────┘                            │
│         │             │             │                                   │
│         └─────────────┼─────────────┘                                   │
│                       │                                                 │
│    ┌──────────────────┼──────────────────────┐                          │
│    │          Distributed KV Layer           │                          │
│    │                                          │                          │
│    │  ┌────────┐  ┌────────┐  ┌────────┐    │                          │
│    │  │Range 1 │  │Range 2 │  │Range 3 │    │  Raft groups             │
│    │  │Leader  │  │Leader  │  │Leader  │    │                          │
│    │  │Replica │  │Replica │  │Replica │    │                          │
│    │  │Replica │  │Replica │  │Replica │    │                          │
│    │  └────────┘  └────────┘  └────────┘    │                          │
│    │                                          │                          │
│    │  OLTP: reads/writes to leaders           │                          │
│    │  OLAP: follower reads (stale but fast)   │                          │
│    │  CDC:  changefeeds to external systems   │                          │
│    └──────────────────────────────────────────┘                          │
│                                                                         │
│  Not a "true" dual-format HTAP, but pragmatic for moderate analytics.  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Examples**: CockroachDB, YugabyteDB

**Pros**: Strong consistency, geo-distribution, SQL compatibility.
**Cons**: Analytical performance limited -- no columnar format, no vectorized execution. Best suited for light-to-moderate analytics, not heavy OLAP.

### Pattern 4: Cloud-Native HTAP

Leverage cloud infrastructure to separate storage and compute, with intelligent routing between OLTP and OLAP engines.

```
┌─────────────────────────────────────────────────────────────────────────┐
│              Pattern 4: Cloud-Native HTAP                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │                     Shared Storage Layer                         │   │
│  │              (S3, GCS, EBS, Cloud-native log)                   │   │
│  └───────────────────┬──────────────────┬──────────────────────────┘   │
│                      │                  │                               │
│           ┌──────────▼──────┐  ┌───────▼──────────────┐               │
│           │  OLTP Compute   │  │  OLAP Compute         │               │
│           │                 │  │                       │               │
│           │  Row-optimized  │  │  Column-optimized     │               │
│           │  Read/write     │  │  Read-only replicas   │               │
│           │  Low latency    │  │  High throughput      │               │
│           │                 │  │  Vectorized engine    │               │
│           └─────────────────┘  └───────────────────────┘               │
│                                                                         │
│  Examples:                                                              │
│  - AlloyDB: PG-compatible OLTP + columnar analytics engine             │
│  - Aurora zero-ETL: Aurora -> Redshift automatic replication           │
│  - PolarDB: Alibaba's shared-storage HTAP                              │
│                                                                         │
│  Key insight: storage disaggregation lets OLTP and OLAP compute        │
│  scale independently while sharing the same data.                      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Pattern Comparison Summary

| Aspect | Dual-Format | In-Memory Columnar | Distributed SQL + Ext | Cloud-Native |
|--------|------------|--------------------|-----------------------|-------------|
| OLTP perf | Excellent | Excellent | Excellent | Excellent |
| OLAP perf | Very good | Excellent | Moderate | Very good |
| Data freshness | Seconds | Sub-second | Seconds-minutes | Seconds |
| Memory cost | Moderate | Very high | Moderate | Low (elastic) |
| Complexity | Moderate | High | Low | Low (managed) |
| Example | TiDB, SQL Server | SAP HANA, SingleStore | CockroachDB | AlloyDB |

---

## 3. TiDB Deep Dive

### Architecture

TiDB is the most widely-adopted open-source HTAP database. It separates compute, row storage, columnar storage, and cluster management into four distinct components:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        TiDB Architecture                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   Client (MySQL Protocol)                                               │
│       │                                                                 │
│       ▼                                                                 │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐                             │
│   │ TiDB     │  │ TiDB     │  │ TiDB     │   Stateless SQL layer      │
│   │ Server   │  │ Server   │  │ Server   │   MySQL-compatible         │
│   │          │  │          │  │          │   Parser, optimizer,        │
│   │          │  │          │  │          │   executor, cost model      │
│   └────┬─────┘  └────┬─────┘  └────┬─────┘                             │
│        │             │             │                                    │
│        └─────────────┼─────────────┘                                    │
│                      │                                                  │
│        ┌─────────────┼─────────────────────────┐                       │
│        │             │                         │                       │
│        ▼             ▼                         ▼                       │
│   ┌──────────┐  ┌──────────┐            ┌───────────┐                  │
│   │  TiKV    │  │  TiKV    │            │ TiFlash   │                  │
│   │  Node 1  │  │  Node 2  │  ...       │ Node 1    │  ...             │
│   │          │  │          │            │           │                  │
│   │ Region   │  │ Region   │            │ Columnar  │                  │
│   │ [a-m]    │  │ [n-z]    │            │ replica   │                  │
│   │ Raft     │  │ Raft     │            │ of all    │                  │
│   │ Leader   │  │ Leader   │            │ Regions   │                  │
│   │          │  │          │            │           │                  │
│   │ LSM-tree │  │ LSM-tree │            │ Delta     │                  │
│   │ (RocksDB)│  │ (RocksDB)│            │ Tree      │                  │
│   └──────────┘  └──────────┘            └───────────┘                  │
│                                               ▲                        │
│        Raft Learner replication ───────────────┘                       │
│                                                                         │
│   ┌────────────────────────────────────────┐                            │
│   │  PD (Placement Driver) Cluster         │                            │
│   │                                        │                            │
│   │  - Timestamp Oracle (TSO)              │                            │
│   │  - Region metadata & routing           │                            │
│   │  - Scheduling & load balancing         │                            │
│   │  - Raft group membership               │                            │
│   └────────────────────────────────────────┘                            │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Component responsibilities:**

| Component | Role | Stateful? | Scalability |
|-----------|------|-----------|-------------|
| **TiDB Server** | SQL parsing, optimization, execution | No (stateless) | Add nodes freely |
| **TiKV** | Distributed KV store, row data, Raft leader/follower | Yes | Add nodes, auto-rebalance |
| **TiFlash** | Columnar store, OLAP acceleration, MPP engine | Yes | Add nodes independently |
| **PD** | Metadata, TSO, scheduling | Yes (Raft-based) | 3 or 5 nodes (odd count) |

### How HTAP Works in TiDB

**Step 1: OLTP writes go to TiKV**

```
Client writes INSERT INTO orders VALUES (1, 'Alice', 99.99)
    │
    ▼
TiDB Server
    │  Finds the Region (shard) owning key prefix "orders_1"
    │  Sends Raft proposal to Region leader
    ▼
TiKV Region Leader
    │  Appends to Raft log
    │  Replicates to TiKV followers (majority quorum)
    │  Commits and applies to RocksDB (LSM-tree)
    ▼
Write acknowledged to client
```

**Step 2: TiFlash receives data via Raft Learner**

```
TiKV Region Leader
    │
    │  Raft log entry replicated to TiFlash
    │  (TiFlash is a Raft LEARNER -- does not vote,
    │   does not affect write quorum or latency)
    │
    ▼
TiFlash Learner Node
    │  Receives Raft log entries asynchronously
    │  Applies them to Delta Tree structure:
    │    1. Delta layer (recent writes, unsorted)
    │    2. Stable layer (sorted columnar segments)
    │  Background merge: delta -> stable
    ▼
Data available for OLAP queries (typically < 1 second lag)
```

**Step 3: Query router decides the path**

```sql
-- OLTP: point lookup -> routed to TiKV
SELECT * FROM orders WHERE order_id = 12345;
-- Optimizer: index scan on primary key, TiKV is optimal

-- OLAP: aggregation scan -> routed to TiFlash
SELECT
    DATE(order_time) AS day,
    COUNT(*) AS num_orders,
    SUM(amount) AS revenue
FROM orders
WHERE order_time >= '2024-01-01'
GROUP BY DATE(order_time);
-- Optimizer: full table scan + aggregation, TiFlash is optimal

-- Force a specific engine with hints:
SELECT /*+ READ_FROM_STORAGE(TIKV[orders]) */ * FROM orders WHERE order_id = 1;
SELECT /*+ READ_FROM_STORAGE(TIFLASH[orders]) */ COUNT(*) FROM orders;
```

**Step 4: MPP engine for distributed analytics**

TiFlash includes a Massively Parallel Processing (MPP) engine that distributes analytical queries across all TiFlash nodes:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    TiFlash MPP Execution                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Query: SELECT region, SUM(amount) FROM orders GROUP BY region          │
│                                                                         │
│  TiDB Server (coordinator)                                              │
│       │                                                                 │
│       │  Builds MPP execution plan                                      │
│       │  Distributes tasks to TiFlash nodes                             │
│       │                                                                 │
│       ├──────────────┬──────────────┬──────────────┐                   │
│       ▼              ▼              ▼              ▼                   │
│  ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐              │
│  │TiFlash 1│   │TiFlash 2│   │TiFlash 3│   │TiFlash 4│              │
│  │         │   │         │   │         │   │         │              │
│  │Scan     │   │Scan     │   │Scan     │   │Scan     │              │
│  │local    │   │local    │   │local    │   │local    │              │
│  │segments │   │segments │   │segments │   │segments │              │
│  │         │   │         │   │         │   │         │              │
│  │Partial  │   │Partial  │   │Partial  │   │Partial  │              │
│  │GROUP BY │   │GROUP BY │   │GROUP BY │   │GROUP BY │              │
│  └────┬────┘   └────┬────┘   └────┬────┘   └────┬────┘              │
│       │             │             │             │                     │
│       │    Exchange (shuffle by hash(region))   │                     │
│       └─────────────┼─────────────┼─────────────┘                    │
│                     ▼             ▼                                    │
│              ┌─────────┐   ┌─────────┐                                │
│              │Agg Node │   │Agg Node │   Final aggregation            │
│              │(regions │   │(regions │                                │
│              │ A-M)    │   │ N-Z)    │                                │
│              └────┬────┘   └────┬────┘                                │
│                   │             │                                      │
│                   └──────┬──────┘                                      │
│                          ▼                                              │
│                   TiDB Server                                           │
│                   (merge results, return to client)                     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Consistency Model

TiDB achieves snapshot isolation across both TiKV and TiFlash:

1. **Timestamp Oracle (TSO)**: PD issues globally ordered timestamps
2. **Read timestamp**: When a query starts, it gets a timestamp `T` from TSO
3. **TiKV snapshot**: TiKV returns data as of timestamp `T` (MVCC)
4. **TiFlash snapshot**: TiFlash checks its Raft applied index; if it has replicated up to `T`, it serves the read. If not, it waits briefly until the Raft learner catches up.

```
Timeline:
─────────────────────────────────────────────────────────────────►
     T1        T2        T3        T4        T5
     │         │         │         │         │
   Write     Write    TiFlash   Query     TiFlash
   to TiKV   to TiKV  catches  starts    serves
   (row 1)   (row 2)  up to T2 (gets T4) query at T4
                                  │
                                  │ TiFlash has data up to T2
                                  │ but query needs T4 snapshot
                                  │ TiFlash waits for Raft learner
                                  │ to catch up to T4
                                  │ (typically < 100ms)
                                  ▼
                              Query served with T4-consistent data
```

### Performance Characteristics

| Metric | TiKV (OLTP) | TiFlash (OLAP) |
|--------|-------------|-----------------|
| Point read latency | 2-5 ms | Not designed for this |
| Insert/update latency | 5-15 ms | N/A (read-only replica) |
| Full table scan (10M rows) | 30-60 seconds | 1-3 seconds |
| Aggregation (100M rows) | Minutes | 5-15 seconds |
| Data freshness | Real-time | < 1 second lag |
| Storage efficiency | 1x (uncompressed rows) | 0.3-0.5x (columnar compression) |

### TiDB Setup Example

```sql
-- Create a table (standard MySQL syntax)
CREATE TABLE events (
    id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    event_type VARCHAR(50),
    payload    JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_user (user_id),
    INDEX idx_time (created_at)
);

-- Enable TiFlash replica (this is the HTAP switch)
ALTER TABLE events SET TIFLASH REPLICA 1;
-- Now TiFlash will asynchronously replicate this table

-- Check replication status
SELECT * FROM information_schema.tiflash_replica;

-- OLTP: insert events (goes to TiKV)
INSERT INTO events (user_id, event_type, payload)
VALUES (42, 'purchase', '{"item": "widget", "price": 9.99}');

-- OLAP: real-time analytics (routed to TiFlash automatically)
SELECT
    event_type,
    COUNT(*) AS event_count,
    COUNT(DISTINCT user_id) AS unique_users
FROM events
WHERE created_at >= NOW() - INTERVAL 1 HOUR
GROUP BY event_type
ORDER BY event_count DESC;

-- Check which engine the optimizer chose
EXPLAIN SELECT COUNT(*) FROM events WHERE created_at >= NOW() - INTERVAL 1 DAY;
-- Look for "cop[tiflash]" in the plan = TiFlash was chosen
-- Look for "cop[tikv]" = TiKV was chosen
```

---

## 4. Google Spanner + AlloyDB

### Google Spanner

Spanner is Google's globally distributed, strongly consistent database. It is not a traditional HTAP system, but its architecture has profoundly influenced the HTAP space.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      Google Spanner Architecture                        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│    ┌─────────────────────────────────────────────────────────┐          │
│    │                    Spanner Frontend                     │          │
│    │              SQL Parser, Optimizer, Executor            │          │
│    └────────────────────────┬────────────────────────────────┘          │
│                             │                                           │
│    ┌────────────────────────┼────────────────────────────────┐          │
│    │              Distribution Layer                         │          │
│    │        Split routing, transaction coordination          │          │
│    └────────────────────────┬────────────────────────────────┘          │
│                             │                                           │
│    ┌────────────────────────┼────────────────────────────────┐          │
│    │                   Splits (Shards)                       │          │
│    │                                                         │          │
│    │  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  │          │
│    │  │Split 1  │  │Split 2  │  │Split 3  │  │Split 4  │  │          │
│    │  │         │  │         │  │         │  │         │  │          │
│    │  │ Paxos   │  │ Paxos   │  │ Paxos   │  │ Paxos   │  │          │
│    │  │ group   │  │ group   │  │ group   │  │ group   │  │          │
│    │  │         │  │         │  │         │  │         │  │          │
│    │  │ US-East │  │ US-West │  │ Europe  │  │ Asia    │  │          │
│    │  │ US-West │  │ Europe  │  │ US-East │  │ US-East │  │          │
│    │  │ Europe  │  │ Asia    │  │ Asia    │  │ Europe  │  │          │
│    │  └─────────┘  └─────────┘  └─────────┘  └─────────┘  │          │
│    │                                                         │          │
│    └─────────────────────────────────────────────────────────┘          │
│                                                                         │
│    ┌─────────────────────────────────────────────────────────┐          │
│    │                     Colossus (GFS)                      │          │
│    │              Distributed File System                    │          │
│    └─────────────────────────────────────────────────────────┘          │
│                                                                         │
│    ┌─────────────────────────────────────────────────────────┐          │
│    │                      TrueTime                           │          │
│    │         GPS receivers + Atomic clocks in every DC       │          │
│    │         API: TT.now() -> [earliest, latest]             │          │
│    │         Uncertainty interval: typically < 7ms            │          │
│    └─────────────────────────────────────────────────────────┘          │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

#### TrueTime: The Secret Sauce

TrueTime is a globally synchronized clock API that returns an interval, not a point in time:

```
TrueTime API:

  TT.now()  -->  TTinterval { earliest: Timestamp, latest: Timestamp }
  TT.after(t)  -->  bool  (true if t is definitely in the past)
  TT.before(t) -->  bool  (true if t is definitely in the future)

  Uncertainty window (epsilon):
  ┌────────────────────────────────────────┐
  │        |<--- epsilon (< 7ms) --->|     │
  │        │                         │     │
  │   earliest              latest         │
  │        │    true time is         │     │
  │        │    somewhere here       │     │
  └────────────────────────────────────────┘

  How commits use TrueTime:

  1. Transaction acquires locks
  2. Picks commit timestamp s = TT.now().latest
  3. WAITS until TT.after(s) is true
     (this is the "commit wait" -- typically < 7ms)
  4. Now s is guaranteed to be in the past for ALL nodes globally
  5. Releases locks and makes data visible at timestamp s

  Result: External Consistency
  If transaction T1 commits before T2 starts (in real time),
  then T1's timestamp < T2's timestamp. Guaranteed. Globally.
```

#### External Consistency

External consistency is stronger than linearizability:

| Property | Guarantee |
|----------|-----------|
| Serializability | Transactions appear to execute in some serial order |
| Linearizability | Operations appear to execute atomically at some point between invocation and response |
| **External consistency** | If T1 finishes before T2 starts (in absolute real time across any machines), T1 is ordered before T2 |

This is what makes Spanner unique: it provides globally meaningful timestamps, enabling consistent reads from any replica worldwide without coordination.

#### Spanner for Analytics

Spanner supports analytical queries through:

1. **Stale reads**: Read at a timestamp in the past (no lock contention)
2. **Read-only transactions**: Snapshot reads at a consistent timestamp, can run on any replica
3. **Batch reads**: Partition-based parallel reads for large scans
4. **Change streams**: Real-time CDC for streaming to BigQuery or other analytical systems

```sql
-- Spanner SQL (Google Standard SQL dialect)

-- Strong read (latest data, may need to contact leader)
SELECT department, COUNT(*) as headcount, AVG(salary) as avg_salary
FROM employees
GROUP BY department;

-- Stale read (up to 15 seconds old, can read from nearest replica)
-- Much faster for analytics, no lock contention
@{READ_TIMESTAMP=TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 15 SECOND)}
SELECT department, COUNT(*) as headcount, AVG(salary) as avg_salary
FROM employees
GROUP BY department;

-- Hierarchical schema (interleaving for co-location)
CREATE TABLE customers (
    customer_id INT64 NOT NULL,
    name        STRING(100),
    email       STRING(200)
) PRIMARY KEY (customer_id);

-- Orders are interleaved (physically co-located) with parent customer
CREATE TABLE orders (
    customer_id INT64 NOT NULL,
    order_id    INT64 NOT NULL,
    amount      FLOAT64,
    created_at  TIMESTAMP
) PRIMARY KEY (customer_id, order_id),
  INTERLEAVE IN PARENT customers ON DELETE CASCADE;

-- This join is extremely fast because data is co-located:
SELECT c.name, SUM(o.amount) as total_spend
FROM customers c
JOIN orders o ON c.customer_id = o.customer_id
GROUP BY c.name
ORDER BY total_spend DESC
LIMIT 100;
```

### Google AlloyDB

AlloyDB is Google's PostgreSQL-compatible cloud database with built-in HTAP capabilities.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                      AlloyDB Architecture                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│    ┌──────────────────────────────────────────────┐                     │
│    │             Primary Instance                  │                     │
│    │                                               │                     │
│    │  ┌──────────────────────────────────────┐    │                     │
│    │  │  PostgreSQL Engine (modified)         │    │                     │
│    │  │  - Standard PG wire protocol         │    │                     │
│    │  │  - Buffer pool + shared memory       │    │                     │
│    │  │  - Transaction processing            │    │                     │
│    │  └─────────────────┬────────────────────┘    │                     │
│    │                    │                          │                     │
│    │  ┌─────────────────▼────────────────────┐    │                     │
│    │  │  Columnar Engine (embedded)          │    │                     │
│    │  │                                      │    │                     │
│    │  │  - Automatic columnar cache          │    │                     │
│    │  │  - ML-based prediction of columns    │    │                     │
│    │  │    to cache in columnar format       │    │                     │
│    │  │  - Vectorized execution              │    │                     │
│    │  │  - No explicit configuration needed  │    │                     │
│    │  └──────────────────────────────────────┘    │                     │
│    └──────────────────────┬───────────────────────┘                     │
│                           │  WAL stream                                  │
│                           ▼                                              │
│    ┌──────────────────────────────────────────────┐                     │
│    │     Log Processing Service (LPS)             │                     │
│    │     - Processes WAL records                  │                     │
│    │     - Writes to distributed storage          │                     │
│    │     - Feeds read replicas                    │                     │
│    └──────────────────────┬───────────────────────┘                     │
│                           │                                              │
│                           ▼                                              │
│    ┌──────────────────────────────────────────────┐                     │
│    │     Google Distributed Storage               │                     │
│    │     - 99.999% durability                     │                     │
│    │     - Automatic replication                  │                     │
│    │     - Transparent to compute layer           │                     │
│    └──────────────────────────────────────────────┘                     │
│                                                                         │
│    ┌──────────────────────────────────────────────┐                     │
│    │     Read Pool Instances (0..N)               │                     │
│    │     - Scale-out read replicas                │                     │
│    │     - Each has own columnar engine           │                     │
│    │     - Route analytics here to isolate OLTP   │                     │
│    └──────────────────────────────────────────────┘                     │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

**AlloyDB Columnar Engine -- Key innovation:**

The columnar engine automatically identifies frequently scanned columns and caches them in columnar format in memory. No DBA intervention required.

```
How AlloyDB's ML-based columnar caching works:

1. Workload Monitor observes query patterns over time
       │
       ▼
2. ML Model predicts which columns will be scanned
       │
       ▼
3. Background process converts those columns to columnar format
   in a reserved memory region
       │
       ▼
4. Query executor checks: "Is this column in columnar cache?"
       │
       ├── Yes: Use vectorized columnar scan (10-100x faster)
       │
       └── No:  Use traditional PostgreSQL heap scan
```

**AlloyDB vs Aurora zero-ETL:**

| Feature | AlloyDB | Aurora zero-ETL |
|---------|---------|-----------------|
| Approach | Built-in columnar engine | Automatic replication to Redshift |
| Analytics engine | Embedded in same process | Separate Redshift cluster |
| Freshness | Sub-second | Minutes |
| Additional cost | None (included) | Redshift cluster cost |
| SQL dialect | PostgreSQL | PostgreSQL (Aurora) + Redshift SQL |
| Complexity | Single system | Two systems, automatic sync |

---

## 5. CockroachDB

### Architecture

CockroachDB is a distributed SQL database inspired by Google Spanner, designed for strong consistency and survivability. Its HTAP story is pragmatic rather than revolutionary -- it is primarily OLTP with extensions for analytical workloads.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                   CockroachDB Architecture                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│         Client (PostgreSQL wire protocol)                               │
│              │                                                          │
│              ▼                                                          │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │                      SQL Layer                                │      │
│  │                                                               │      │
│  │  ┌──────────┐  ┌───────────┐  ┌────────────┐  ┌──────────┐  │      │
│  │  │ Parser   │─►│ Optimizer │─►│ Dist SQL   │─►│ Executor │  │      │
│  │  │          │  │ (cost-    │  │ (parallel  │  │          │  │      │
│  │  │          │  │  based)   │  │  across    │  │          │  │      │
│  │  │          │  │           │  │  nodes)    │  │          │  │      │
│  │  └──────────┘  └───────────┘  └────────────┘  └──────────┘  │      │
│  └───────────────────────────────────┬───────────────────────────┘      │
│                                      │                                  │
│  ┌───────────────────────────────────┼───────────────────────────┐      │
│  │              Transaction Layer     │                           │      │
│  │     (MVCC, timestamp allocation, distributed txn protocol)    │      │
│  └───────────────────────────────────┼───────────────────────────┘      │
│                                      │                                  │
│  ┌───────────────────────────────────┼───────────────────────────┐      │
│  │              Distribution Layer                               │      │
│  │                                                               │      │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │      │
│  │  │ Range 1  │  │ Range 2  │  │ Range 3  │  │ Range N  │    │      │
│  │  │          │  │          │  │          │  │          │    │      │
│  │  │ Leasehld │  │ Follower │  │ Leasehld │  │ Follower │    │      │
│  │  │          │  │          │  │          │  │          │    │      │
│  │  │ Raft log │  │ Raft log │  │ Raft log │  │ Raft log │    │      │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘    │      │
│  │                                                               │      │
│  │  Each range: ~512 MB of contiguous keyspace                   │      │
│  │  3 or 5 replicas per range, Raft consensus                   │      │
│  │  Automatic splitting, merging, rebalancing                   │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │              Storage Layer (Pebble -- Go LSM engine)          │      │
│  │              MVCC key encoding: /table/index/key/timestamp    │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  Key difference from Spanner: no TrueTime. Uses HLC (Hybrid            │
│  Logical Clocks) with a max clock offset parameter (default 500ms).    │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### HTAP Capabilities

CockroachDB is not a dual-format HTAP system. It stores data only in row format (LSM-tree). Its analytical capabilities come from distributed SQL execution and specific features:

#### 1. Follower Reads

Follower reads allow analytical queries to read from Raft followers instead of the leaseholder, distributing read load:

```sql
-- Exact staleness: read data as of 5 seconds ago
-- Can be served by any replica (including followers in other regions)
SELECT count(*), sum(amount)
FROM orders
WHERE created_at > '2024-01-01'
AS OF SYSTEM TIME '-5s';

-- Bounded staleness: read the freshest data available on the nearest replica
-- without a network round trip to the leaseholder
SELECT count(*), sum(amount)
FROM orders
WHERE created_at > '2024-01-01'
AS OF SYSTEM TIME with_max_staleness('10s');
```

#### 2. Changefeeds (CDC)

For heavy analytics, CockroachDB recommends streaming changes to a dedicated analytical system:

```sql
-- Create a changefeed to Kafka for downstream analytics
CREATE CHANGEFEED FOR TABLE orders, order_items
INTO 'kafka://broker:9092'
WITH format = 'json',
     updated,
     resolved = '10s',
     kafka_sink_config = '{"Flush": {"MaxMessages": 1000}}';

-- Enterprise changefeed to cloud storage
CREATE CHANGEFEED FOR TABLE orders
INTO 's3://my-bucket/cdc?AUTH=implicit'
WITH format = 'parquet',
     partition_format = 'daily';
-- Parquet files land in S3 -> query with Athena/Trino/Spark
```

#### 3. DistSQL Execution Engine

CockroachDB distributes query execution across nodes, pushing computation to where data lives:

```
Query: SELECT region, SUM(revenue) FROM sales GROUP BY region

┌──────────┐     ┌──────────┐     ┌──────────┐
│  Node 1  │     │  Node 2  │     │  Node 3  │
│          │     │          │     │          │
│ TableRdr │     │ TableRdr │     │ TableRdr │
│ (local   │     │ (local   │     │ (local   │
│  ranges) │     │  ranges) │     │  ranges) │
│    │     │     │    │     │     │    │     │
│ Partial  │     │ Partial  │     │ Partial  │
│ Agg      │     │ Agg      │     │ Agg      │
│    │     │     │    │     │     │    │     │
└────┼─────┘     └────┼─────┘     └────┼─────┘
     │                │                │
     └────────────────┼────────────────┘
                      │
                 Final Agg
                 (gateway node)
                      │
                      ▼
                   Result
```

### SQL Compatibility

CockroachDB implements the PostgreSQL wire protocol and a substantial subset of PostgreSQL SQL:

```sql
-- Standard PostgreSQL features supported
CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      STRING UNIQUE NOT NULL,
    name       STRING NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    metadata   JSONB,
    INDEX idx_email (email),
    INVERTED INDEX idx_metadata (metadata)
);

-- CockroachDB-specific: multi-region configuration
ALTER DATABASE mydb PRIMARY REGION "us-east1";
ALTER DATABASE mydb ADD REGION "us-west1";
ALTER DATABASE mydb ADD REGION "europe-west1";

-- Regional by row: each row lives in a specific region
ALTER TABLE users SET LOCALITY REGIONAL BY ROW;

-- Global tables: replicated to all regions, fast reads everywhere
ALTER TABLE config SET LOCALITY GLOBAL;
```

---

## 6. SingleStore (formerly MemSQL)

### Architecture

SingleStore combines an in-memory rowstore with a disk-based columnstore in a single distributed database, making it one of the more capable HTAP systems for mixed workloads.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    SingleStore Architecture                              │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Client (MySQL wire protocol)                                           │
│       │                                                                 │
│       ▼                                                                 │
│  ┌─────────────────────────────────────────────────────┐                │
│  │              Aggregator Layer                        │                │
│  │                                                     │                │
│  │  ┌───────────┐  ┌───────────┐  ┌───────────┐      │                │
│  │  │  Agg Node │  │  Agg Node │  │  Agg Node │      │                │
│  │  │  (master) │  │  (child)  │  │  (child)  │      │                │
│  │  │           │  │           │  │           │      │                │
│  │  │ - DDL     │  │ - Query   │  │ - Query   │      │                │
│  │  │ - Cluster │  │   routing │  │   routing │      │                │
│  │  │   mgmt    │  │ - Result  │  │ - Result  │      │                │
│  │  │ - Query   │  │   merge   │  │   merge   │      │                │
│  │  │   routing │  │           │  │           │      │                │
│  │  └───────────┘  └───────────┘  └───────────┘      │                │
│  └─────────────────────────┬───────────────────────────┘                │
│                            │                                            │
│  ┌─────────────────────────┼───────────────────────────┐                │
│  │              Leaf Layer  │                           │                │
│  │                         │                           │                │
│  │  ┌──────────────────────┼────────────────────────┐  │                │
│  │  │          Leaf Node 1 │                        │  │                │
│  │  │                      │                        │  │                │
│  │  │  ┌───────────────────┼──────────────────┐     │  │                │
│  │  │  │   Partitions (shards)                │     │  │                │
│  │  │  │                                      │     │  │                │
│  │  │  │  ┌────────────┐  ┌────────────────┐  │     │  │                │
│  │  │  │  │ Rowstore   │  │ Columnstore    │  │     │  │                │
│  │  │  │  │ Partition  │  │ Partition      │  │     │  │                │
│  │  │  │  │            │  │                │  │     │  │                │
│  │  │  │  │ Skip list  │  │ Sorted segment │  │     │  │                │
│  │  │  │  │ in memory  │  │ files on disk  │  │     │  │                │
│  │  │  │  │            │  │                │  │     │  │                │
│  │  │  │  │ Lock-free  │  │ Compressed     │  │     │  │                │
│  │  │  │  │ hash index │  │ columnar       │  │     │  │                │
│  │  │  │  │            │  │ encoding       │  │     │  │                │
│  │  │  │  └────────────┘  └────────────────┘  │     │  │                │
│  │  │  └──────────────────────────────────────┘     │  │                │
│  │  └───────────────────────────────────────────────┘  │                │
│  │                                                     │                │
│  │  ┌───────────────────────────────────────────────┐  │                │
│  │  │          Leaf Node 2 (similar structure)      │  │                │
│  │  └───────────────────────────────────────────────┘  │                │
│  │                                                     │                │
│  │  ┌───────────────────────────────────────────────┐  │                │
│  │  │          Leaf Node N (similar structure)      │  │                │
│  │  └───────────────────────────────────────────────┘  │                │
│  └─────────────────────────────────────────────────────┘                │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Universal Storage: Rowstore + Columnstore

SingleStore allows choosing between rowstore and columnstore on a per-table basis, and they can coexist in the same database and join across formats:

```sql
-- Rowstore table: for OLTP (primary keys, point lookups, transactions)
CREATE TABLE users (
    user_id    BIGINT PRIMARY KEY,
    username   VARCHAR(100) NOT NULL,
    email      VARCHAR(255) UNIQUE,
    created_at DATETIME DEFAULT NOW(),
    INDEX (username),
    SHARD KEY (user_id)                -- distribution key
) USING ROWSTORE;                      -- in-memory skip list

-- Columnstore table: for OLAP (scans, aggregations, time series)
CREATE TABLE events (
    event_id   BIGINT,
    user_id    BIGINT,
    event_type VARCHAR(50),
    properties JSON,
    ts         DATETIME,
    SHARD KEY (user_id),
    SORT KEY (ts),                      -- physical sort order on disk
    KEY (event_type) USING CLUSTERED COLUMNSTORE
);

-- Cross-format join (rowstore JOIN columnstore -- the HTAP sweet spot)
SELECT
    u.username,
    e.event_type,
    COUNT(*) AS event_count,
    MIN(e.ts) AS first_seen,
    MAX(e.ts) AS last_seen
FROM users u                            -- rowstore (fast user lookup)
JOIN events e ON u.user_id = e.user_id  -- columnstore (fast scan)
WHERE e.ts >= NOW() - INTERVAL 7 DAY
GROUP BY u.username, e.event_type
ORDER BY event_count DESC
LIMIT 50;
```

### Key Features

#### Compiled Query Execution (Code Generation)

SingleStore compiles SQL queries into machine code using LLVM, avoiding the interpretation overhead of traditional database executors:

```
Traditional execution:
  Query Plan -> Volcano Iterator Model -> interpret each row -> slow

SingleStore code generation:
  Query Plan -> LLVM IR -> Native Machine Code -> execute directly -> fast

  Example: A simple filter + aggregation

  Traditional (interpreted):
    for each row:
      call virtual function next()
      call virtual function filter()    <-- vtable lookup overhead
      call virtual function aggregate() <-- per-row function call
      ... repeat millions of times

  SingleStore (compiled):
    ; Generated native code (conceptual)
    .loop:
      load next row from segment
      cmp [row + offset_status], 'active'  ; inline filter
      jne .skip
      add [accumulator], [row + offset_amount]  ; inline aggregate
      inc [counter]
    .skip:
      dec [remaining]
      jnz .loop
    ; No virtual dispatch, no interpretation overhead
```

#### Pipeline Execution Model

Instead of materializing intermediate results, SingleStore streams data through a pipeline:

```
┌────────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
│  Segment   │───►│  Filter  │───►│   Hash   │───►│  Output  │
│  Scan      │    │          │    │   Agg    │    │          │
│            │    │ status = │    │ GROUP BY │    │  Result  │
│ Columnar   │    │ 'active' │    │ region   │    │  set     │
│ segment    │    │          │    │          │    │          │
│ file       │    │ Runs in  │    │ Runs in  │    │          │
│            │    │ same     │    │ same     │    │          │
│            │    │ thread   │    │ thread   │    │          │
└────────────┘    └──────────┘    └──────────┘    └──────────┘
     No materialization between stages -- data flows through pipeline
```

#### Real-Time Kafka Ingest

SingleStore has a built-in Kafka consumer, enabling real-time ingestion without external tools:

```sql
-- Create a pipeline that ingests from Kafka into a columnstore table
CREATE PIPELINE events_pipeline AS
LOAD DATA KAFKA 'broker1:9092/events-topic'
BATCH_INTERVAL 1000                  -- flush every 1 second
INTO TABLE events
FIELDS TERMINATED BY '\t'
(event_id, user_id, event_type, @properties, ts)
SET properties = @properties;

-- Start the pipeline
START PIPELINE events_pipeline;

-- Monitor pipeline status
SELECT * FROM information_schema.PIPELINES_CURSORS;
-- Shows: consumer lag, rows ingested, errors, throughput
```

---

## 7. SAP HANA

SAP HANA is the original in-memory columnar HTAP database, designed to eliminate the gap between SAP's transactional ERP systems and their analytical/BI layer.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        SAP HANA Architecture                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │                    Application Layer                           │      │
│  │  XS Engine (embedded app server), Calculation Engine,         │      │
│  │  Planning Engine, Predictive Analytics, Text Analysis         │      │
│  └───────────────────────────────────┬───────────────────────────┘      │
│                                      │                                  │
│  ┌───────────────────────────────────┼───────────────────────────┐      │
│  │                    SQL Engine      │                           │      │
│  │  SQL Parser, Optimizer, Executor, MDX, SQLScript, Graph       │      │
│  └───────────────────────────────────┼───────────────────────────┘      │
│                                      │                                  │
│  ┌───────────────────────────────────┼───────────────────────────┐      │
│  │                    In-Memory Engines                           │      │
│  │                                                               │      │
│  │  ┌──────────────────────────────────────────────────────┐    │      │
│  │  │              Column Store (primary)                   │    │      │
│  │  │                                                      │    │      │
│  │  │  ┌──────────────┐    ┌───────────────────────────┐  │    │      │
│  │  │  │ Delta Store  │    │ Main Store                 │  │    │      │
│  │  │  │              │    │                            │  │    │      │
│  │  │  │ Row-oriented │    │ Column-oriented            │  │    │      │
│  │  │  │ Unsorted     │──► │ Dictionary-encoded         │  │    │      │
│  │  │  │ L1 (hot)     │    │ Sorted                     │  │    │      │
│  │  │  │ L2 (warm)    │    │ Compressed (up to 10x)     │  │    │      │
│  │  │  │              │    │ SIMD vectorized scans      │  │    │      │
│  │  │  │ INSERT/UPDATE│    │                            │  │    │      │
│  │  │  │ goes here    │    │ Read-optimized             │  │    │      │
│  │  │  └──────────────┘    └───────────────────────────┘  │    │      │
│  │  │              ▲                                       │    │      │
│  │  │              │ Delta Merge (background)              │    │      │
│  │  │              │ Converts delta rows -> main columns   │    │      │
│  │  └──────────────┼───────────────────────────────────────┘    │      │
│  │                 │                                             │      │
│  │  ┌──────────────┼──────────────────┐                         │      │
│  │  │ Row Store (secondary, optional) │                         │      │
│  │  │ For small lookup tables,        │                         │      │
│  │  │ system catalog                  │                         │      │
│  │  └─────────────────────────────────┘                         │      │
│  │                                                               │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │              Persistence Layer                                │      │
│  │  WAL (redo log), Savepoints, Data volumes                    │      │
│  │  Data tiering: hot (DRAM) -> warm (NVM/PMEM) -> cold (disk) │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │              Scale-Out (optional)                              │      │
│  │  Multiple HANA nodes with table/partition distribution        │      │
│  │  Distributed query execution                                  │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Delta Store and Main Store -- The HTAP Mechanism

The delta/main architecture is the core of HANA's HTAP capability:

```
Write Path:
                                                      Time
  ──────────────────────────────────────────────────────────►

  INSERT row (id=1, name='Alice', salary=100000)
       │
       ▼
  ┌────────────────┐
  │  L1 Delta      │  Row-oriented, unsorted, in-memory
  │  (hot writes)  │  Optimized for point inserts/updates
  │                │  Small (MB-sized)
  └───────┬────────┘
          │  When L1 reaches threshold (~10K rows)
          ▼
  ┌────────────────┐
  │  L2 Delta      │  Still row-oriented, but sorted
  │  (warm writes) │  Larger (MB-GB sized)
  │                │  Allows more efficient lookups
  └───────┬────────┘
          │  Delta Merge (background, periodic or on threshold)
          ▼
  ┌────────────────┐
  │  Main Store    │  Column-oriented, dictionary-encoded
  │  (bulk data)   │  Highly compressed (5-10x)
  │                │  Optimized for scans, SIMD, cache-friendly
  │                │  Read-only (immutable until next merge)
  └────────────────┘

Read Path:
  SELECT SUM(salary) FROM employees WHERE dept = 'Engineering'
       │
       ├── Scan Main Store (columnar, fast, vectorized)
       │   - Dictionary lookup: 'Engineering' -> code 7
       │   - Scan encoded column with SIMD
       │   - Decompress salary column
       │   - Vectorized SUM
       │
       ├── Scan L2 Delta (row scan, filter, aggregate)
       │
       ├── Scan L1 Delta (row scan, filter, aggregate)
       │
       └── Merge results from all three sources
           (union with MVCC visibility filtering)
```

### MVCC with Timestamp-Based Visibility

HANA uses MVCC to isolate OLTP transactions from OLAP scans:

```
Transaction Timeline:
  T100: UPDATE employees SET salary = 110000 WHERE id = 1
  T101: BEGIN (OLAP read)
  T102: COMMIT T100

  OLAP at T101 sees the BEFORE image (salary = 100000)
  because T100 was not committed when T101 started.

  MVCC Version Chain:
  ┌─────────────────┐     ┌─────────────────┐
  │ Current version │     │ Old version     │
  │ salary = 110000 │────►│ salary = 100000 │
  │ valid: T102-    │     │ valid: T50-T102 │
  │                 │     │                 │
  └─────────────────┘     └─────────────────┘

  T101 reads old version because T101 < T102
```

### NUMA-Aware Processing

HANA is explicitly designed for Non-Uniform Memory Access architectures:

```
┌─────────────────────────────────────────────────────────────────┐
│                    NUMA Topology                                 │
│                                                                  │
│  ┌─────────────────┐              ┌─────────────────┐           │
│  │   NUMA Node 0   │              │   NUMA Node 1   │           │
│  │                  │              │                  │           │
│  │  CPU 0-15       │  QPI/UPI     │  CPU 16-31      │           │
│  │  Local DRAM     │◄────────────►│  Local DRAM     │           │
│  │  256 GB         │  (slower)    │  256 GB         │           │
│  │                  │              │                  │           │
│  │  Table A, part1 │              │  Table A, part2 │           │
│  │  (columns 1-5)  │              │  (columns 6-10) │           │
│  └─────────────────┘              └─────────────────┘           │
│                                                                  │
│  HANA places column partitions on NUMA nodes and schedules      │
│  threads to process local data, minimizing cross-node memory    │
│  access (which is 2-3x slower than local access).               │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Data Tiering

For datasets larger than DRAM, HANA supports multi-tier storage:

| Tier | Storage Medium | Access Speed | Use Case |
|------|---------------|-------------|----------|
| Hot | DRAM | ~100 ns | Active transactional data |
| Warm | Intel Optane PMEM / NVMe | ~1 us | Recent historical, moderate query frequency |
| Cold | Disk (SSD/HDD) | ~100 us | Archive, compliance, rare access |
| Data lake | Hadoop/S3 (via Smart Data Access) | ~1 s | External big data |

```sql
-- HANA data tiering example
-- Move partitions older than 1 year to warm storage
ALTER TABLE sales
  PARTITION BY RANGE (sale_date)
  (PARTITION p_current VALUES <= ('2024-12-31') IN MEMORY FULL,
   PARTITION p_2023   VALUES <= ('2023-12-31') PAGE LOADABLE,
   PARTITION p_old    VALUES <= ('2020-12-31') PAGE LOADABLE NO LOAD);
```

---

## 8. F1 and Spanner at Google

### The Migration Story

Google's advertising system was originally built on a heavily sharded MySQL deployment. The migration to F1/Spanner is one of the most instructive large-scale database migrations in history.

```
Before (2005-2012):                    After (2012+):
┌─────────────────────────┐           ┌─────────────────────────┐
│  MySQL Shards           │           │  F1 (Stateless SQL)     │
│                         │           │        │                │
│  ┌───┐ ┌───┐ ┌───┐     │           │        ▼                │
│  │ S1│ │ S2│ │ S3│ ... │           │  Spanner (Distributed   │
│  │   │ │   │ │   │     │           │   storage + txns)       │
│  └───┘ └───┘ └───┘     │           │        │                │
│                         │           │        ▼                │
│  - Manual sharding      │           │  Colossus (GFS)        │
│  - No cross-shard txns  │           │                         │
│  - No cross-shard joins │           │  - Automatic sharding   │
│  - Manual rebalancing   │           │  - Global transactions  │
│  - Resharding = outage  │           │  - Cross-shard joins    │
│  - App-level routing    │           │  - Auto rebalancing     │
└─────────────────────────┘           └─────────────────────────┘
```

### F1 Architecture

F1 is a distributed SQL engine built on top of Spanner, designed specifically for Google's AdWords (now Google Ads) system:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          F1 Architecture                                │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────────────────────────────────────────────┐           │
│  │                    F1 Servers (stateless)                 │           │
│  │                                                          │           │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐              │           │
│  │  │ F1 SQL   │  │ F1 SQL   │  │ F1 SQL   │  ...         │           │
│  │  │ Server   │  │ Server   │  │ Server   │              │           │
│  │  │          │  │          │  │          │              │           │
│  │  │ Parser   │  │ Parser   │  │ Parser   │              │           │
│  │  │ Optimize │  │ Optimize │  │ Optimize │              │           │
│  │  │ Execute  │  │ Execute  │  │ Execute  │              │           │
│  │  └──────────┘  └──────────┘  └──────────┘              │           │
│  │                                                          │           │
│  │  Features:                                               │           │
│  │  - Distributed joins (hash, broadcast, lookup)          │           │
│  │  - Distributed aggregations                             │           │
│  │  - Parallel query execution across Spanner splits       │           │
│  │  - Protocol buffer support (structured columns)         │           │
│  └──────────────────────────────┬───────────────────────────┘           │
│                                 │                                       │
│  ┌──────────────────────────────┼───────────────────────────┐           │
│  │               Spanner        │                           │           │
│  │                              ▼                           │           │
│  │  Handles: storage, replication, transactions, TrueTime  │           │
│  └──────────────────────────────────────────────────────────┘           │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────┐           │
│  │  F1 Change History                                       │           │
│  │                                                          │           │
│  │  Every mutation generates a ChangeBatch record:          │           │
│  │  {                                                       │           │
│  │    "table": "Campaign",                                  │           │
│  │    "key": [customer_id, campaign_id],                    │           │
│  │    "columns_changed": ["budget", "status"],              │           │
│  │    "old_values": [1000, "ACTIVE"],                       │           │
│  │    "new_values": [2000, "PAUSED"],                       │           │
│  │    "timestamp": "2024-01-15T10:30:00.123456Z"            │           │
│  │  }                                                       │           │
│  │                                                          │           │
│  │  Used for:                                               │           │
│  │  - Real-time analytics pipelines                         │           │
│  │  - Cache invalidation                                    │           │
│  │  - Downstream system synchronization                     │           │
│  │  - Audit logging                                         │           │
│  └──────────────────────────────────────────────────────────┘           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Hierarchical Schema Design (Interleaving)

F1/Spanner use hierarchical schema to co-locate related data, critical for performance:

```
Schema without interleaving (bad for joins):
  Customer rows: scattered across many splits
  Campaign rows: scattered across many splits
  AdGroup rows:  scattered across many splits

  JOIN Customer-Campaign-AdGroup = cross-split reads = slow

Schema with interleaving (good for joins):
  Split 1: Customer 1 -> Campaign 1A -> AdGroup 1A1, 1A2
                       -> Campaign 1B -> AdGroup 1B1
  Split 2: Customer 2 -> Campaign 2A -> AdGroup 2A1

  JOIN Customer-Campaign-AdGroup = single split read = fast

  Physical key layout:
  ┌────────────────────────────────────────────────────────┐
  │  Key                              │ Value              │
  ├────────────────────────────────────┼────────────────────┤
  │  /Customer/1                      │ {name: "Acme"}     │
  │  /Customer/1/Campaign/1A          │ {budget: 1000}     │
  │  /Customer/1/Campaign/1A/AdGrp/1  │ {bid: 2.50}       │
  │  /Customer/1/Campaign/1A/AdGrp/2  │ {bid: 3.00}       │
  │  /Customer/1/Campaign/1B          │ {budget: 500}      │
  │  /Customer/1/Campaign/1B/AdGrp/1  │ {bid: 1.75}       │
  │  ─── split boundary ───           │                    │
  │  /Customer/2                      │ {name: "Globex"}   │
  │  /Customer/2/Campaign/2A          │ {budget: 2000}     │
  └────────────────────────────────────┴────────────────────┘
```

### Change Streams for Real-Time Analytics

Modern Spanner provides Change Streams as a first-class feature:

```sql
-- Create a change stream to watch specific tables
CREATE CHANGE STREAM orders_stream
  FOR orders, order_items
  OPTIONS (
    retention_period = '7d',
    value_capture_type = 'NEW_AND_OLD_VALUES'
  );

-- Read changes programmatically (Go example)
-- This is conceptual pseudocode for the Spanner client library
func watchChanges(ctx context.Context, client *spanner.Client) {
    partitions := client.PartitionChangeStream(ctx, "orders_stream",
        spanner.Timestamp{/* start */})

    for _, p := range partitions {
        go func(partition ChangeStreamPartition) {
            for record := range partition.Records() {
                switch r := record.(type) {
                case DataChangeRecord:
                    // r.Table, r.Keys, r.NewValues, r.OldValues
                    // Push to BigQuery, Pub/Sub, or analytics pipeline
                    publishToBigQuery(r)
                case HeartbeatRecord:
                    // No changes, but confirms liveness
                case ChildPartitionsRecord:
                    // Split/merge happened, handle new partitions
                }
            }
        }(p)
    }
}
```

---

## 9. HTAP Challenges and Trade-offs

### Resource Contention: The Fundamental Tension

OLTP and OLAP have fundamentally opposing resource access patterns:

```
┌─────────────────────────────────────────────────────────────────────────┐
│              Resource Contention: OLTP vs OLAP                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Resource        OLTP Pattern              OLAP Pattern                 │
│  ─────────────────────────────────────────────────────────────────      │
│  CPU             Short bursts (< 1ms)      Long sustained (seconds)    │
│                  Latency-sensitive          Throughput-optimized        │
│                                                                         │
│  Memory          Small working set          Huge working set            │
│                  B-tree pages, lock table   Sort buffers, hash tables   │
│                  Many concurrent txns       Few massive queries         │
│                                                                         │
│  I/O             Random reads (index)       Sequential scans            │
│                  Small (single page)        Large (full partitions)     │
│                  Latency-bound              Bandwidth-bound             │
│                                                                         │
│  Network         Small payloads             Large result shuffling      │
│                  Client <-> single node     Node <-> node (shuffle)     │
│                                                                         │
│  Locks/Latches   Row-level, short-held      Table-level, long scans    │
│                  Contention-sensitive       Can block writers           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Memory Pressure: The Buffer Pool Problem

Analytical queries can evict OLTP hot pages from the buffer pool:

```
Before OLAP query:
┌──────────────────────────────────────────┐
│  Buffer Pool (8 GB)                       │
│                                           │
│  ┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐   │
│  │Users││Index││Order││Prdct││Cstmr│   │  Hot OLTP pages
│  │ hot ││ hot ││ hot ││ hot ││ hot │   │  Frequently accessed
│  └─────┘└─────┘└─────┘└─────┘└─────┘   │  99% cache hit rate
│                                           │
└──────────────────────────────────────────┘

OLAP query: SELECT SUM(amount) FROM orders WHERE date > '2023-01-01'
  -- Scans 500 GB of order data through 8 GB buffer pool

After OLAP query:
┌──────────────────────────────────────────┐
│  Buffer Pool (8 GB)                       │
│                                           │
│  ┌─────┐┌─────┐┌─────┐┌─────┐┌─────┐   │
│  │Old  ││Old  ││Old  ││Old  ││Old  │   │  Cold OLAP scan pages
│  │order││order││order││order││order│   │  Will never be read again
│  └─────┘└─────┘└─────┘└─────┘└─────┘   │  OLTP hit rate drops to 40%
│                                           │
│  All OLTP hot pages have been evicted!    │
│  OLTP latency spikes from 2ms to 50ms    │
└──────────────────────────────────────────┘

Mitigations:
  1. Separate buffer pools (HANA: delta store vs main store memory)
  2. Separate storage engines (TiDB: TiKV has its own block cache)
  3. Scan-resistant eviction (MySQL: young/old LRU sublists)
  4. Separate compute (AlloyDB: analytics on read replicas)
```

### The Noisy Neighbor Problem

In a shared HTAP system, one workload type can degrade the other:

```
Scenario: OLAP query runs a massive hash join, consuming all CPU

  CPU utilization over time:
  100%│  ████████████████████████████
     │  █ OLAP hash join            █
  75%│  █                           █
     │  █                           █
  50%│  █                           █
     │──█───────────────────────────█──── OLTP needs 20% CPU
  25%│  █   OLTP transactions       █     but is starved
     │  █   waiting for CPU         █
   0%│──█───────────────────────────█────────────────────►
     T0  T1                         T2                  Time

  OLTP p99 latency during OLAP query:
  Before: 5ms
  During: 200ms (40x degradation)

  Solutions by system:
  ┌──────────────┬─────────────────────────────────────────────────┐
  │ System       │ Isolation Mechanism                              │
  ├──────────────┼─────────────────────────────────────────────────┤
  │ TiDB         │ Separate processes: TiKV (OLTP) vs TiFlash     │
  │              │ (OLAP) on different machines                     │
  │ SAP HANA     │ Workload classes with CPU/memory limits         │
  │ SingleStore  │ Resource pools with CPU and memory caps          │
  │ AlloyDB      │ Read pool instances for analytics               │
  │ CockroachDB  │ Admission control + follower reads              │
  │ SQL Server   │ Resource Governor + separate columnstore reads  │
  └──────────────┴─────────────────────────────────────────────────┘
```

### Network Overhead for Distributed Analytics

Distributed analytical queries require shuffling data between nodes:

```
Query: SELECT customer_id, SUM(amount)
       FROM orders JOIN customers ON orders.cust_id = customers.id
       GROUP BY customer_id

Data distribution:
  Node 1: orders partition A,  customers partition X
  Node 2: orders partition B,  customers partition Y
  Node 3: orders partition C,  customers partition Z

Shuffle required for hash join:
  ┌────────┐         ┌────────┐         ┌────────┐
  │ Node 1 │ ──────► │ Node 2 │ ──────► │ Node 3 │
  │        │ ◄────── │        │ ◄────── │        │
  │ 2 GB   │         │ 2 GB   │         │ 2 GB   │
  │ shuffle│         │ shuffle│         │ shuffle│
  └────────┘         └────────┘         └────────┘

  Total network transfer: ~12 GB (each node sends to every other)
  On 10 Gbps network: ~10 seconds just for data transfer
  On 25 Gbps network: ~4 seconds

  This is why co-located schemas (Spanner interleaving) and
  smart shard keys (SingleStore SHARD KEY) matter enormously.
```

### Consistency vs Freshness Trade-offs

```
                    Strong                              Eventual
Consistency    ◄─────────────────────────────────────────────► Consistency
                    │              │              │
                    │              │              │
                Spanner         TiDB          CockroachDB
                (external      (snapshot       (follower
                consistency,   isolation,      reads,
                commit wait)   Raft learner    stale reads)
                               < 1s lag)
                    │              │              │
                    │              │              │
Freshness:      Real-time      < 1 second      5-15 seconds
Cost:           High latency   Moderate         Low latency
                (commit wait)  (async repl)     (any replica)

  The trade-off question for HTAP:
  ┌────────────────────────────────────────────────────────────────────┐
  │ "Can your analytics tolerate data that is 1 second old?"          │
  │                                                                    │
  │  YES (99% of cases) --> TiDB/SingleStore style async replication  │
  │                         is fine. Much better performance.          │
  │                                                                    │
  │  NO  (fraud, trading)  --> Need strong consistency reads from     │
  │                            leader/primary. Pay the latency cost.  │
  └────────────────────────────────────────────────────────────────────┘
```

### When HTAP Makes Sense vs Separate Systems

```
┌─────────────────────────────────────────────────────────────────────────┐
│                 Decision Matrix: HTAP vs Separate                       │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  HTAP is GOOD when:                    Separate is GOOD when:           │
│  ─────────────────                     ────────────────────             │
│  ✓ Data freshness < 1 min matters      ✓ OLAP workload is heavy        │
│  ✓ OLAP queries are moderate           ✓ Data volume > 10 TB           │
│  ✓ Operational simplicity valued       ✓ Complex ETL transformations   │
│  ✓ Data volume < 10 TB                 ✓ Multiple data sources         │
│  ✓ Team is small                       ✓ ML/training workloads         │
│  ✓ Real-time dashboards needed         ✓ Long-running batch analytics  │
│  ✓ Transactional analytics             ✓ Regulatory data separation    │
│    (e.g., "show account balance        ✓ OLAP needs different schema   │
│     and spending trends")                (star schema, cubes)          │
│                                                                         │
│  Warning signs HTAP is wrong:                                           │
│  ✗ OLAP queries take > 30 minutes                                      │
│  ✗ Analytics needs data from 5+ different source systems               │
│  ✗ You need a dedicated data science / ML training environment         │
│  ✗ OLTP p99 latency budget is < 5ms with zero tolerance               │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 10. HTAP Decision Framework

### Step 1: Classify Your Workload

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    Workload Classification                               │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  Score each dimension (1-5):                                            │
│                                                                         │
│  A. OLTP Intensity                                                      │
│     1 = Few writes, simple lookups                                      │
│     5 = Thousands of TPS, complex transactions                         │
│                                                                         │
│  B. OLAP Intensity                                                      │
│     1 = Simple counts, few dashboards                                   │
│     5 = Complex joins, ML features, heavy aggregations                 │
│                                                                         │
│  C. Freshness Requirement                                               │
│     1 = Daily reports are fine                                          │
│     5 = Sub-second freshness required                                   │
│                                                                         │
│  D. Data Volume                                                         │
│     1 = < 100 GB                                                        │
│     5 = > 10 TB                                                         │
│                                                                         │
│  E. Operational Complexity Tolerance                                    │
│     1 = Need simple, managed solution                                   │
│     5 = Have dedicated platform team                                    │
│                                                                         │
│  Scoring:                                                               │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │  If A >= 3 AND B >= 3 AND C >= 4 AND D <= 3:                │      │
│  │     --> Strong HTAP candidate                                 │      │
│  │                                                               │      │
│  │  If B >= 4 AND D >= 4:                                       │      │
│  │     --> Dedicated OLAP (warehouse) likely needed              │      │
│  │                                                               │      │
│  │  If A >= 4 AND B <= 2:                                       │      │
│  │     --> Pure OLTP database sufficient                         │      │
│  │                                                               │      │
│  │  If C <= 2:                                                   │      │
│  │     --> Traditional ETL + warehouse is fine                   │      │
│  │                                                               │      │
│  │  If E <= 2:                                                   │      │
│  │     --> Prefer managed cloud-native (AlloyDB, Aurora)         │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Step 2: Data Freshness Requirements

| Freshness Level | Acceptable Lag | Solution | Example Use Case |
|----------------|---------------|----------|-----------------|
| Real-time | < 1 second | HTAP (TiDB, SingleStore, HANA) | Fraud scoring, live pricing |
| Near real-time | 1-60 seconds | HTAP or CDC streaming | Operational dashboards |
| Low latency | 1-15 minutes | CDC + streaming warehouse | Business KPIs, monitoring |
| Batch | 1-24 hours | Traditional ETL | Daily reports, compliance |
| Archive | Days-weeks | Batch ETL + data lake | Historical analysis, ML training |

### Step 3: Cost Comparison

```
Scenario: 2 TB dataset, 5000 OLTP TPS, moderate analytics

Option A: Separate OLTP + OLAP
┌────────────────────────────────────────────────────┐
│  PostgreSQL (OLTP)                                  │
│    3x r6g.2xlarge (8 vCPU, 64 GB)                  │
│    = $3,200/month                                   │
│                                                     │
│  Redshift (OLAP)                                    │
│    2x ra3.xlplus                                    │
│    = $2,600/month                                   │
│                                                     │
│  ETL (Airflow + Glue)                               │
│    Compute + orchestration                          │
│    = $800/month                                     │
│                                                     │
│  Data engineer (partial FTE for ETL maintenance)    │
│    = $5,000/month (amortized)                       │
│                                                     │
│  TOTAL: ~$11,600/month                              │
└────────────────────────────────────────────────────┘

Option B: HTAP (TiDB)
┌────────────────────────────────────────────────────┐
│  TiDB Server (3 nodes)                              │
│    3x 8 vCPU, 32 GB                                │
│    = $1,800/month                                   │
│                                                     │
│  TiKV (3 nodes)                                     │
│    3x 8 vCPU, 64 GB, 2 TB SSD                      │
│    = $4,200/month                                   │
│                                                     │
│  TiFlash (2 nodes)                                  │
│    2x 16 vCPU, 128 GB, 2 TB SSD                    │
│    = $3,400/month                                   │
│                                                     │
│  PD (3 nodes, small)                                │
│    = $600/month                                     │
│                                                     │
│  No ETL, no separate warehouse, no ETL engineer     │
│                                                     │
│  TOTAL: ~$10,000/month                              │
└────────────────────────────────────────────────────┘

Option C: Cloud-Native HTAP (AlloyDB)
┌────────────────────────────────────────────────────┐
│  AlloyDB Primary                                    │
│    16 vCPU, 128 GB                                  │
│    = $3,800/month                                   │
│                                                     │
│  AlloyDB Read Pool (2 nodes for analytics)          │
│    2x 16 vCPU, 128 GB                               │
│    = $3,800/month                                   │
│                                                     │
│  Storage (2 TB)                                     │
│    = $600/month                                     │
│                                                     │
│  No ETL, built-in columnar, fully managed           │
│                                                     │
│  TOTAL: ~$8,200/month                               │
└────────────────────────────────────────────────────┘
```

### Step 4: Migration Strategies

#### Strategy 1: Shadow Traffic Migration

```
Phase 1: Deploy HTAP alongside existing OLTP
┌───────────┐     ┌──────────┐
│  App      │────►│ OLTP DB  │  (existing, primary)
│           │     └──────────┘
│           │     ┌──────────┐
│           │────►│ HTAP DB  │  (new, receives shadow writes)
└───────────┘     └──────────┘

Phase 2: Validate data consistency
  - Compare query results between OLTP and HTAP
  - Benchmark latency and throughput
  - Run analytics on HTAP, compare with warehouse

Phase 3: Switch reads to HTAP
┌───────────┐     ┌──────────┐
│  App      │────►│ OLTP DB  │  (writes only)
│           │     └──────────┘
│           │     ┌──────────┐
│           │◄───│ HTAP DB  │  (reads + analytics)
└───────────┘     └──────────┘

Phase 4: Switch writes to HTAP, decommission old OLTP + warehouse
┌───────────┐     ┌──────────┐
│  App      │◄──►│ HTAP DB  │  (everything)
└───────────┘     └──────────┘
```

#### Strategy 2: Analytics-First Migration

Migrate only the analytical workload first, keep OLTP as-is:

```
Current:  OLTP DB --> ETL --> Warehouse --> Dashboards

Step 1:   OLTP DB --> CDC --> HTAP DB --> Dashboards
          OLTP DB --> ETL --> Warehouse (still running, comparison)

Step 2:   OLTP DB --> CDC --> HTAP DB --> Dashboards
          (warehouse decommissioned)

Step 3:   HTAP DB (now also handling OLTP) --> Dashboards
          (OLTP DB decommissioned)
```

### Step 5: HTAP Maturity Model

| Level | Description | Characteristics |
|-------|------------|-----------------|
| **Level 0: Separated** | OLTP + batch ETL + warehouse | Hours of data lag, high operational burden |
| **Level 1: Streaming** | OLTP + CDC + streaming warehouse | Minutes of lag, still two systems |
| **Level 2: Hybrid** | HTAP for light analytics + warehouse for heavy | Best of both worlds, moderate complexity |
| **Level 3: Converged** | Single HTAP system for all workloads | Minimal lag, simple operations, possible noisy neighbor |
| **Level 4: Intelligent** | HTAP with ML-driven optimization (AlloyDB) | Automatic workload routing, self-tuning |

Most organizations should target **Level 2** as a pragmatic sweet spot: use HTAP for operational analytics (real-time dashboards, fraud scoring, inventory checks) while keeping a warehouse for heavy batch analytics (ML training, historical trend analysis, ad-hoc exploration).

---

## 11. HTAP Comparison Table

### Feature Comparison

```
┌────────────────┬──────────┬──────────┬───────────┬────────────┬──────────┬──────────┐
│                │  TiDB    │ Spanner  │CockroachDB│SingleStore │ AlloyDB  │ SAP HANA │
├────────────────┼──────────┼──────────┼───────────┼────────────┼──────────┼──────────┤
│ OLTP           │ Very     │ Very     │ Very      │ Very       │ Very     │ Very     │
│ Performance    │ Good     │ Good     │ Good      │ Good       │ Good     │ Good     │
│                │          │          │           │            │          │          │
│ OLAP           │ Very     │ Moderate │ Limited   │ Excellent  │ Very     │ Excellent│
│ Performance    │ Good     │          │           │            │ Good     │          │
│                │ (TiFlash)│          │           │            │          │          │
│                │          │          │           │            │          │          │
│ Consistency    │ Snapshot │ External │ Serializ- │ Snapshot   │ Snapshot │ Snapshot │
│ Model          │ Isolation│ Consist. │ able      │ Isolation  │ Isolation│ Isolation│
│                │          │ (strong- │ (strong)  │            │ (PG      │          │
│                │          │ est)     │           │            │ levels)  │          │
│                │          │          │           │            │          │          │
│ SQL            │ MySQL    │ Google   │ PostgreSQL│ MySQL      │PostgreSQL│ ANSI SQL │
│ Compatibility  │ protocol │ Standard │ wire      │ wire       │ wire     │ + ext.   │
│                │          │ SQL      │ protocol  │ protocol   │ protocol │          │
│                │          │          │           │            │          │          │
│ Horizontal     │ Yes      │ Yes      │ Yes       │ Yes        │ Read     │ Limited  │
│ Scalability    │ (TiKV +  │ (auto-   │ (auto-    │ (add leaf  │ replicas │ (scale-  │
│                │ TiFlash) │ split)   │ rebalance)│ nodes)     │ only     │ up main) │
│                │          │          │           │            │          │          │
│ Cloud / On-    │ Both     │ Cloud    │ Both      │ Both       │ Cloud    │ Both     │
│ Prem           │ (open    │ only     │ (open     │ (managed + │ only     │ (on-prem │
│                │ source)  │ (GCP)    │ source)   │ on-prem)   │ (GCP)    │ + cloud) │
│                │          │          │           │            │          │          │
│ Data Fresh-    │ < 1 sec  │ Real-    │ 5-15 sec  │ < 1 sec    │ Sub-     │ Sub-     │
│ ness (OLAP)    │ (Raft    │ time     │ (follower │ (delta     │ second   │ second   │
│                │ learner) │ (strong  │ reads)    │ merge)     │ (built-  │ (delta   │
│                │          │ reads)   │           │            │ in)      │ merge)   │
│                │          │          │           │            │          │          │
│ Licensing      │ Apache   │ Pay per  │ BSL/      │ Commercial │ Pay per  │ Commercial│
│ / Cost         │ 2.0      │ node-hr  │ Apache    │ license    │ compute  │ license  │
│                │ (free)   │ + storage│ (free     │ ($$$$)     │ + storage│ ($$$$$)  │
│                │          │ ($$$)    │ core)     │            │ ($$)     │          │
│                │          │          │           │            │          │          │
│ Best For       │ MySQL    │ Global   │ PG apps   │ Real-time  │ PG apps  │ SAP ERP  │
│                │ migration│ strong   │ needing   │ analytics  │ needing  │ + analyt │
│                │ + real-  │ consist. │ resilience│ + fast     │ easy     │ combined │
│                │ time     │ at scale │           │ ingest     │ analytics│          │
│                │ analytics│          │           │            │          │          │
└────────────────┴──────────┴──────────┴───────────┴────────────┴──────────┴──────────┘
```

### Performance Benchmarks (Approximate, Normalized)

These are approximate relative performance numbers based on published benchmarks and industry experience. Actual performance depends heavily on hardware, configuration, data distribution, and query patterns.

```
OLTP: TPC-C-like workload (transactions per second, normalized)
────────────────────────────────────────────────────────────────
TiDB          ████████████████████░░░░░  80%  (vs MySQL baseline)
Spanner       ██████████████████░░░░░░░  72%  (cross-region penalty)
CockroachDB   ████████████████░░░░░░░░░  64%  (Raft + Go overhead)
SingleStore   ████████████████████████░  96%  (in-memory rowstore)
AlloyDB       ██████████████████████░░░  88%  (PG-optimized)
SAP HANA      ████████████████████████░  96%  (in-memory)

OLAP: TPC-H-like workload (throughput, normalized to Redshift baseline)
────────────────────────────────────────────────────────────────
TiDB          ██████████████████░░░░░░░  72%  (TiFlash MPP)
Spanner       ██████████░░░░░░░░░░░░░░░  40%  (not columnar)
CockroachDB   ██████░░░░░░░░░░░░░░░░░░░  24%  (row store only)
SingleStore   ██████████████████████░░░  88%  (columnstore + codegen)
AlloyDB       ████████████████░░░░░░░░░  64%  (columnar engine)
SAP HANA      █████████████████████████  100% (in-memory columnar)

Data Freshness for OLAP (lower is better):
────────────────────────────────────────────────────────────────
TiDB          ██░░░░░░░░░░░░░░░░░░░░░░░  < 1 sec
Spanner       █░░░░░░░░░░░░░░░░░░░░░░░░  real-time (strong reads)
CockroachDB   █████████░░░░░░░░░░░░░░░░  5-15 sec (follower reads)
SingleStore   ██░░░░░░░░░░░░░░░░░░░░░░░  < 1 sec
AlloyDB       ██░░░░░░░░░░░░░░░░░░░░░░░  < 1 sec
SAP HANA      █░░░░░░░░░░░░░░░░░░░░░░░░  sub-second
```

### When to Choose Each

| Choose This | When You Need |
|-------------|--------------|
| **TiDB** | MySQL compatibility, open source, strong HTAP with separate OLTP/OLAP engines, horizontal scale, cost-effective |
| **Spanner** | Global strong consistency, multi-region active-active, Google Cloud native, financial/regulatory workloads |
| **CockroachDB** | PostgreSQL compatibility, multi-region resilience, strong consistency, moderate analytics needs |
| **SingleStore** | Maximum mixed-workload performance, real-time Kafka ingest, in-memory speed, commercial support |
| **AlloyDB** | PostgreSQL compatibility, minimal operational overhead, Google Cloud, automatic columnar optimization |
| **SAP HANA** | SAP ecosystem integration, extreme in-memory performance, enterprise support, on-premises requirement |

### Architecture Decision Tree

```
Start
  │
  ├── Need global strong consistency?
  │     ├── Yes ── On GCP? ── Yes ──► Spanner
  │     │                  └─ No ───► CockroachDB
  │     │
  │     └── No (snapshot isolation is fine)
  │           │
  │           ├── Need heavy OLAP (TPC-H class)?
  │           │     ├── Yes ── Budget for commercial license?
  │           │     │           ├── Yes ── In-memory feasible?
  │           │     │           │           ├── Yes ──► SAP HANA
  │           │     │           │           └── No ───► SingleStore
  │           │     │           │
  │           │     │           └── No ── Need MySQL compat?
  │           │     │                      ├── Yes ──► TiDB
  │           │     │                      └── No ───► TiDB or AlloyDB
  │           │     │
  │           │     └── No (light-moderate OLAP)
  │           │           ├── PostgreSQL required?
  │           │           │     ├── Yes ── Managed/cloud?
  │           │           │     │           ├── Yes ──► AlloyDB
  │           │           │     │           └── No ───► CockroachDB
  │           │           │     │
  │           │           │     └── No ── MySQL required?
  │           │           │               ├── Yes ──► TiDB
  │           │           │               └── No ───► SingleStore
  │           │           │
  │           │           └── Already on SAP?
  │           │                 ├── Yes ──► SAP HANA
  │           │                 └── No ───► (see above)
  │           │
  │           └── Data volume > 50 TB?
  │                 ├── Yes ── HTAP alone won't cut it.
  │                 │          Use HTAP for operational analytics
  │                 │          + dedicated warehouse for heavy OLAP.
  │                 │
  │                 └── No ── Any of the above HTAP systems work.
  │                           Choose based on SQL compat and ops model.
  │
  └── End
```

---

## Key Takeaways

1. **HTAP eliminates ETL** for operational analytics, providing seconds of data freshness instead of hours. This is transformative for fraud detection, real-time dashboards, and dynamic pricing.

2. **Dual-format storage** (row + column) is the most proven HTAP pattern. TiDB's Raft learner approach and SQL Server's columnstore indexes are production-hardened implementations.

3. **Workload isolation is the hardest problem**. Without physical separation (separate processes or machines for OLTP vs OLAP), the noisy neighbor problem will bite you. TiDB solves this well by running TiKV and TiFlash on separate nodes.

4. **HTAP is not a silver bullet**. For heavy analytical workloads (multi-TB data lakes, complex ML pipelines, data from many sources), a dedicated warehouse is still the right choice. HTAP shines for operational analytics on a single system's data.

5. **Cloud-native HTAP** (AlloyDB, Aurora zero-ETL) is the lowest-friction path for teams already on cloud. The managed columnar engines remove operational complexity entirely.

6. **Start at Level 2** of the maturity model: use HTAP for operational analytics and keep your warehouse for heavy batch work. This gives you real-time dashboards without risking your OLTP stability.

7. **Measure before migrating**. Run shadow traffic, compare query results, benchmark p99 latency under combined load. The HTAP promise is real, but the trade-offs are also real.

---

## Further Reading

- **TiDB**: [Architecture Overview](https://docs.pingcap.com/tidb/stable/tidb-architecture) -- Raft learner replication to TiFlash
- **Spanner**: Corbett et al., "Spanner: Google's Globally-Distributed Database" (OSDI 2012)
- **F1**: Shute et al., "F1: A Distributed SQL Database That Scales" (VLDB 2013)
- **TrueTime**: Included in the Spanner paper -- GPS + atomic clocks for external consistency
- **SAP HANA**: Farber et al., "SAP HANA Database: Data Management for Modern Business Applications" (SIGMOD Record 2012)
- **SingleStore**: [Universal Storage Architecture](https://docs.singlestore.com/cloud/reference/sql-reference/) -- rowstore + columnstore design
- **AlloyDB**: [Columnar Engine](https://cloud.google.com/alloydb/docs/columnar-engine/about) -- ML-driven automatic columnar caching
- **CockroachDB**: [Architecture Overview](https://www.cockroachlabs.com/docs/stable/architecture/overview.html) -- distributed SQL on Raft
- **Gartner HTAP**: Coined in 2014 by Gartner analysts, described in "Hybrid Transaction/Analytical Processing Will Foster Opportunities for Dramatic Business Innovation"
