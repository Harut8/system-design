# Chapter 23: Batch Processing — Spark Architecture and the MapReduce Concept

A production-grade deep dive into batch processing for senior engineers who need to design large-scale data pipelines, ETL systems, and distributed computation frameworks. Covers MapReduce from first principles, Apache Spark's architecture and internals (DAG scheduler, Catalyst optimizer, Tungsten execution engine, shuffle mechanics), and the practical patterns that power petabyte-scale data processing in production.

Prerequisites: Distributed filesystems (HDFS/S3) from `14-distributed-filesystems-gfs-hdfs-ceph-juicefs.md` (roadmap), stream processing from `22-stream-processing-flink-watermarks-eos.md`, and sharding concepts from `10-sharding-and-consistent-hashing.md`. This chapter provides the batch processing foundation that complements the streaming chapter — together they cover the full spectrum of distributed data processing.

---

## Table of Contents

1. [Batch Processing Fundamentals](#1-batch-processing-fundamentals)
2. [MapReduce: The Original Distributed Batch Framework](#2-mapreduce-the-original-distributed-batch-framework)
3. [MapReduce Limitations and the Road to Spark](#3-mapreduce-limitations-and-the-road-to-spark)
4. [Apache Spark Architecture](#4-apache-spark-architecture)
5. [The RDD Abstraction](#5-the-rdd-abstraction)
6. [Spark SQL and the DataFrame API](#6-spark-sql-and-the-dataframe-api)
7. [Catalyst Optimizer Deep Dive](#7-catalyst-optimizer-deep-dive)
8. [Tungsten Execution Engine](#8-tungsten-execution-engine)
9. [Shuffle Internals](#9-shuffle-internals)
10. [Memory Management and Spilling](#10-memory-management-and-spilling)
11. [Spark on Kubernetes and YARN](#11-spark-on-kubernetes-and-yarn)
12. [Fault Tolerance and Lineage](#12-fault-tolerance-and-lineage)
13. [Adaptive Query Execution (AQE)](#13-adaptive-query-execution-aqe)
14. [Data Skew and Performance Tuning](#14-data-skew-and-performance-tuning)
15. [Batch vs Stream: Lambda and Kappa Architectures](#15-batch-vs-stream-lambda-and-kappa-architectures)
16. [Production Patterns and Anti-Patterns](#16-production-patterns-and-anti-patterns)

---

## 1. Batch Processing Fundamentals

### What Batch Processing Is

Batch processing is the execution of a series of computations on a **finite, bounded dataset** without manual intervention. The defining characteristics: the input is complete before processing begins, latency is measured in minutes to hours (not milliseconds), and throughput is the primary optimization target.

```
Batch Processing vs Other Paradigms:
════════════════════════════════════

┌─────────────────┬──────────────────┬──────────────────┬──────────────────┐
│                 │ Batch            │ Micro-Batch      │ Stream           │
├─────────────────┼──────────────────┼──────────────────┼──────────────────┤
│ Input           │ Bounded dataset  │ Small bounded    │ Unbounded        │
│                 │ (files, tables)  │ windows          │ (events)         │
│ Latency         │ Minutes–hours    │ Seconds          │ Milliseconds     │
│ Throughput      │ Very high        │ High             │ Medium           │
│ Completeness    │ Exact (all data) │ Near-exact       │ Approximate      │
│ State           │ Recomputed       │ Incremental      │ Incremental      │
│ Failure model   │ Restart job      │ Replay micro-    │ Checkpoint +     │
│                 │ (idempotent)     │ batch            │ replay           │
│ Example systems │ Spark, MapReduce │ Spark Streaming  │ Flink, KStreams  │
│ Use cases       │ ETL, ML training │ Dashboard aggs   │ Fraud detection  │
│                 │ reports, backfill│ near-RT metrics  │ RT features      │
└─────────────────┴──────────────────┴──────────────────┴──────────────────┘
```

### Why Batch Processing Still Matters

Even in the age of real-time streaming, batch processing remains dominant for several workloads:

```
Workloads where batch wins:
═══════════════════════════

1. ML Model Training
   Read: 10 TB of labeled training data
   Compute: gradient descent across 1000 GPUs for 6 hours
   Output: trained model checkpoint
   → Streaming adds no value — all data must be seen multiple epochs

2. Historical Backfills
   "Recompute the last 2 years of user features with the new logic"
   → Unbounded by definition of the task, but the data is bounded
   → Must produce EXACT results (financial reports, compliance)

3. Large-Scale ETL
   Raw events (S3, 50 TB/day) → cleaned, deduplicated, enriched
   → Throughput matters more than latency
   → Must handle late-arriving data (reprocess yesterday)

4. Data Quality and Validation
   "Find all orders where amount < 0 across 3 years of data"
   → Full scan, no approximation acceptable

5. Cost
   Batch clusters scale to zero between runs.
   Streaming clusters run 24/7.
   For workloads that tolerate hourly latency, batch is 3-10x cheaper.
```

---

## 2. MapReduce: The Original Distributed Batch Framework

### The Core Abstraction

MapReduce (Google, 2004) introduced a simple contract: the programmer writes two functions — `map` and `reduce` — and the framework handles distribution, fault tolerance, and data movement.

```
MapReduce Programming Model:
═════════════════════════════

                        Input Data
                     (splits on HDFS/GFS)
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
    ┌───────────┐     ┌───────────┐     ┌───────────┐
    │  Map Task │     │  Map Task │     │  Map Task │
    │  (split 0)│     │  (split 1)│     │  (split 2)│
    │           │     │           │     │           │
    │ map(key,  │     │ map(key,  │     │ map(key,  │
    │   value)  │     │   value)  │     │   value)  │
    │ → [(k,v)] │     │ → [(k,v)] │     │ → [(k,v)] │
    └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
          │                 │                 │
          └────────┬────────┴────────┬────────┘
                   │   SHUFFLE       │
                   │   (sort +       │
                   │    partition    │
                   │    by key)      │
          ┌────────┴────────┬────────┴────────┐
          ▼                 ▼                 ▼
    ┌───────────┐     ┌───────────┐     ┌───────────┐
    │Reduce Task│     │Reduce Task│     │Reduce Task│
    │(partition │     │(partition │     │(partition │
    │    0)     │     │    1)     │     │    2)     │
    │           │     │           │     │           │
    │reduce(key,│     │reduce(key,│     │reduce(key,│
    │  [values])│     │  [values])│     │  [values])│
    │ → [(k,v)] │     │ → [(k,v)] │     │ → [(k,v)] │
    └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
          │                 │                 │
          ▼                 ▼                 ▼
                     Output Data
                  (files on HDFS/GFS)
```

### Word Count: The Canonical Example

```python
# MapReduce Word Count (conceptual)

def map(key: str, value: str) -> list[tuple[str, int]]:
    """key = filename, value = file contents"""
    result = []
    for word in value.split():
        result.append((word.lower(), 1))
    return result

def reduce(key: str, values: list[int]) -> tuple[str, int]:
    """key = word, values = list of 1s"""
    return (key, sum(values))

# Input:  "the cat sat on the mat"
# Map:    [("the",1), ("cat",1), ("sat",1), ("on",1), ("the",1), ("mat",1)]
# Shuffle: group by key → {"the":[1,1], "cat":[1], "sat":[1], "on":[1], "mat":[1]}
# Reduce: [("the",2), ("cat",1), ("sat",1), ("on",1), ("mat",1)]
```

### The Shuffle: MapReduce's Critical Path

```
Shuffle in detail (the most expensive phase):
══════════════════════════════════════════════

Map side:
  1. Map function emits (key, value) pairs
  2. Partitioner assigns each key to a reduce partition:
     partition = hash(key) % num_reducers
  3. Pairs are written to an in-memory buffer (100 MB default)
  4. When buffer fills → sort by (partition, key) → spill to disk
  5. Multiple spills merged into a single sorted file per map task
  6. Optionally, a COMBINER runs locally (mini-reduce before shuffle):
     e.g., ("the",1),("the",1) → ("the",2) — reduces network I/O

                    Map Output (sorted, partitioned)
                    ┌────┬────┬────┐
                    │ P0 │ P1 │ P2 │  ← partitions for 3 reducers
                    └──┬─┴──┬─┴──┬─┘
                       │    │    │

Reduce side:
  1. Each reducer fetches its partition from ALL map tasks (HTTP pull)
  2. Merge-sort all fetched partitions (external merge if > memory)
  3. Feed sorted (key, [values]) groups to the reduce function

  Network transfer:
  ┌─────────┐    ┌─────────┐    ┌─────────┐
  │ Mapper 0│    │ Mapper 1│    │ Mapper 2│
  │   P0    │    │   P0    │    │   P0    │
  └────┬────┘    └────┬────┘    └────┬────┘
       │              │              │
       └──────────────┼──────────────┘
                      │  network transfer
                      ▼
                ┌───────────┐
                │ Reducer 0 │  receives P0 from ALL mappers
                │ merge-sort│  → sorted stream of (key, [values])
                │ → reduce()│
                └───────────┘

  THIS is why MapReduce is slow:
  • Every intermediate result is written to disk (map side spill)
  • All data for a reducer crosses the network (all-to-all shuffle)
  • Reduce cannot start until ALL mappers finish (barrier synchronization)
```

### Multi-Stage MapReduce

```
Real-world jobs require multiple MapReduce stages chained together:

Example: "Top 10 most-purchased products per region"

Stage 1: Parse + Filter
  Map:    raw_log → (region:product_id, 1)
  Reduce: (region:product_id, [1,1,...]) → (region:product_id, count)
  Output: → HDFS (intermediate)

Stage 2: Group by Region + Rank
  Map:    (region:product_id, count) → (region, (product_id, count))
  Reduce: (region, [(pid1,c1),(pid2,c2),...]) → top 10 by count
  Output: → HDFS (final)

Each stage: read from HDFS → map → shuffle → reduce → write to HDFS

  ┌──────┐   ┌────┐   ┌──────┐   ┌────┐   ┌──────┐   ┌────┐   ┌──────┐
  │ HDFS │──►│ M  │──►│ HDFS │──►│ M  │──►│ HDFS │──►│ M  │──►│ HDFS │
  │(input)│  │ R  │   │(tmp1)│   │ R  │   │(tmp2)│   │ R  │   │(out) │
  └──────┘   └────┘   └──────┘   └────┘   └──────┘   └────┘   └──────┘

  Problem: HDFS materialization between EVERY stage.
  A 3-stage pipeline writes intermediate data to disk 2 extra times.
  If input = 1 TB, intermediate can be 3-5 TB of disk I/O.

  This is the fundamental problem Spark solves.
```

### Hadoop MapReduce Architecture (YARN)

```
Hadoop 2.x / 3.x Architecture:
═══════════════════════════════

┌────────────────────────────────────────────────────────────────┐
│  ResourceManager (RM)                                          │
│  ┌────────────────────────┐  ┌──────────────────────────────┐  │
│  │ Scheduler              │  │ ApplicationManager           │  │
│  │ (Capacity / Fair)      │  │ (accepts jobs, launches AMs)  │  │
│  └────────────┬───────────┘  └──────────────┬───────────────┘  │
└───────────────┼──────────────────────────────┼─────────────────┘
                │                              │
     ┌──────────┼───────────────┐              │
     ▼          ▼               ▼              │
┌─────────┐ ┌─────────┐ ┌─────────┐           │
│NodeMgr 1│ │NodeMgr 2│ │NodeMgr 3│           │
│         │ │         │ │         │           │
│┌───────┐│ │┌───────┐│ │┌───────┐│           │
││Contnr ││ ││Contnr ││ ││Contnr ││ ◄─────────┘
││(AM)   ││ ││(Map)  ││ ││(Map)  ││   AM = ApplicationMaster
│└───────┘│ │└───────┘│ │└───────┘│   (one per job, manages tasks)
│┌───────┐│ │┌───────┐│ │┌───────┐│
││Contnr ││ ││Contnr ││ ││Contnr ││
││(Reduce)││ ││(Map)  ││ ││(Reduce)││
│└───────┘│ │└───────┘│ │└───────┘│
└─────────┘ └─────────┘ └─────────┘

  Data locality optimization:
  RM tries to schedule map tasks on the same node as the HDFS block.
  "Move computation to data, not data to computation."

  Block placement (HDFS default replication = 3):
  Block B1 → Node 1 (local), Node 3 (rack-local), Node 7 (off-rack)
  Map task for B1 → prefer Node 1, then Node 3, then any node.
```

---

## 3. MapReduce Limitations and the Road to Spark

### Why MapReduce Wasn't Enough

```
MapReduce Pain Points:
══════════════════════

1. Disk I/O between stages
   Every MR stage writes output to HDFS. A 5-stage pipeline reads/writes
   intermediate data 4 extra times. On spinning disks (HDD era): crushing.

2. Rigid two-phase model
   Many algorithms don't decompose into map + reduce cleanly.
   Iterative algorithms (ML training, PageRank) require N passes
   over the same data — each pass is a separate MR job with full HDFS I/O.

   PageRank iteration cost:
   ┌──────┐      ┌──────┐      ┌──────┐      ┌──────┐
   │ HDFS │─MR──►│ HDFS │─MR──►│ HDFS │─MR──►│ HDFS │  ...
   │iter 0│      │iter 1│      │iter 2│      │iter 3│
   └──────┘      └──────┘      └──────┘      └──────┘
   Each iteration: read entire graph + ranks, compute, write back.
   20 iterations × 100 GB graph = 4 TB of I/O for what should be in-memory.

3. No native support for interactive queries
   Each query is a new MR job: JVM startup, task scheduling, HDFS read.
   Simple COUNT(*) on a cached dataset: 30+ seconds.

4. Only Java (practically)
   Hadoop Streaming allowed Python/Ruby, but with serialization overhead.

5. High operational complexity
   HDFS NameNode = single point of failure (before HA federation).
   Tuning: 200+ XML configuration parameters.
   Map task count, reduce task count, sort buffer size, spill percentage,
   JVM heap, shuffle buffer — all manual.

6. Poor utilization
   Reduce slots sit idle until all mappers finish (barrier sync).
   Map slots sit idle during reduce phase.
   Static slot allocation (map slots vs reduce slots) wastes resources.
```

### The Spark Insight

```
Spark's key insight (Matei Zaharia, 2010):
══════════════════════════════════════════

Keep intermediate data IN MEMORY between stages.

MapReduce:
  Stage 1 output → write to HDFS → Stage 2 reads from HDFS

Spark:
  Stage 1 output → keep in memory → Stage 2 reads from memory

  Performance impact on iterative workloads:

  ┌───────────────────────────────────────────────────────────┐
  │  Logistic Regression (100 iterations, 100 GB input)       │
  │                                                           │
  │  Hadoop MapReduce:  ~110 minutes (read from HDFS × 100)   │
  │  Spark (cached):    ~5 minutes   (read from HDFS × 1,     │
  │                                   then in-memory × 99)    │
  │                                                           │
  │  Speedup: ~22x                                            │
  │                                                           │
  │  The 22x is NOT because Spark's code is faster.           │
  │  It's because Spark avoids 99 HDFS round-trips.           │
  └───────────────────────────────────────────────────────────┘

  The formal abstraction: Resilient Distributed Datasets (RDDs).
```

---

## 4. Apache Spark Architecture

### Cluster Architecture

```
Spark Cluster Architecture:
═══════════════════════════

┌───────────────────────────────────────────────────────────────┐
│  Driver Process                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  SparkContext                                            │  │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │  │
│  │  │ DAG Scheduler│  │ Task         │  │ Block        │  │  │
│  │  │ (stages,     │  │ Scheduler    │  │ Manager      │  │  │
│  │  │  RDD lineage)│  │ (tasks →     │  │ Master       │  │  │
│  │  │              │  │  executors)  │  │ (tracks      │  │  │
│  │  └──────┬───────┘  └──────┬───────┘  │  cached RDDs)│  │  │
│  │         │                 │          └──────────────┘  │  │
│  └─────────┼─────────────────┼───────────────────────────┘  │
└────────────┼─────────────────┼──────────────────────────────┘
             │                 │
             │  DAG of stages  │  task assignments
             │                 │
┌────────────▼─────────────────▼──────────────────────────────┐
│  Cluster Manager (YARN / Kubernetes / Standalone / Mesos)    │
│  Allocates executor containers on worker nodes               │
└────────────┬─────────────────┬──────────────────────────────┘
             │                 │
     ┌───────┴───────┐ ┌──────┴────────┐
     ▼               ▼ ▼               ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│  Executor 1 │ │  Executor 2 │ │  Executor 3 │
│  (Worker 1) │ │  (Worker 2) │ │  (Worker 3) │
│             │ │             │ │             │
│ ┌────┬────┐ │ │ ┌────┬────┐ │ │ ┌────┬────┐ │
│ │Task│Task│ │ │ │Task│Task│ │ │ │Task│Task│ │
│ │ 0  │ 1  │ │ │ │ 2  │ 3  │ │ │ │ 4  │ 5  │ │
│ └────┴────┘ │ │ └────┴────┘ │ │ └────┴────┘ │
│             │ │             │ │             │
│ ┌─────────┐ │ │ ┌─────────┐ │ │ ┌─────────┐ │
│ │ Block   │ │ │ │ Block   │ │ │ │ Block   │ │
│ │ Manager │ │ │ │ Manager │ │ │ │ Manager │ │
│ │ (cache) │ │ │ │ (cache) │ │ │ │ (cache) │ │
│ └─────────┘ │ │ └─────────┘ │ │ └─────────┘ │
└─────────────┘ └─────────────┘ └─────────────┘

Key roles:
  Driver:   the user's main() program. Creates SparkContext, builds
            the DAG of transformations, schedules stages, collects results.
  Executor: a JVM process on a worker node. Runs tasks and caches data.
  Task:     a unit of work applied to ONE partition of the data.
```

### Job, Stage, Task Hierarchy

```
Spark Execution Hierarchy:
══════════════════════════

  Application (SparkContext lifetime)
       │
       ├── Job 1 (triggered by an action: count(), save(), collect())
       │    │
       │    ├── Stage 0 (map-side: read + filter + map)
       │    │    ├── Task 0 (partition 0)
       │    │    ├── Task 1 (partition 1)
       │    │    └── Task 2 (partition 2)
       │    │
       │    └── Stage 1 (reduce-side: shuffle read + aggregate)
       │         ├── Task 0 (partition 0)
       │         ├── Task 1 (partition 1)
       │         └── Task 2 (partition 2)
       │
       └── Job 2 (triggered by next action)
            └── ...

  Stage boundaries are drawn at SHUFFLE points:
    narrow dependencies (map, filter, union) → same stage
    wide dependencies (groupBy, join, repartition) → new stage

  Why this matters:
    Within a stage: tasks run in parallel, pipelined, no data exchange.
    Between stages: shuffle (network I/O, disk spill) — the expensive part.
```

### The DAG Scheduler

```
DAG Scheduler — from logical plan to physical stages:
═════════════════════════════════════════════════════

User code:
  val orders = spark.read.parquet("s3://data/orders")
  val items  = spark.read.parquet("s3://data/items")
  val result = orders
    .filter($"status" === "completed")
    .join(items, "item_id")
    .groupBy("category")
    .agg(sum("amount"))
    .orderBy(desc("sum(amount)"))

Logical DAG (RDD lineage):
  orders ──► filter ──► join ──► groupBy ──► sort
  items  ──────────────────┘

Physical stages (DAG scheduler):
  ┌─ Stage 0: Scan orders + filter ─┐
  │  (narrow transforms, pipelined)  │
  │  Partitions: same as input files │──── shuffle (by item_id)
  └──────────────────────────────────┘                │
                                                      │
  ┌─ Stage 1: Scan items ───────────┐                 │
  │  Partitions: same as input files │──── shuffle ────┘
  └──────────────────────────────────┘     (by item_id)
                                                      │
  ┌─ Stage 2: Shuffle read + join ──────────────────┐ │
  │  + groupBy + partial aggregate                   │◄┘
  │  Partitions: spark.sql.shuffle.partitions (200)  │
  └──────────────────────────────────────────────────┘
                         │ shuffle (by category)
                         ▼
  ┌─ Stage 3: Shuffle read + final aggregate + sort ┐
  │  Partitions: spark.sql.shuffle.partitions (200)  │
  └──────────────────────────────────────────────────┘
                         │
                         ▼ action: collect() / save()

  Stage 0 and Stage 1 can run in PARALLEL (no dependency between them).
  Stage 2 waits for BOTH to complete (join requires both sides).
  Stage 3 waits for Stage 2.
```

---

## 5. The RDD Abstraction

### Resilient Distributed Datasets

```
RDD Properties:
════════════════

An RDD is an immutable, partitioned collection of records that can be
operated on in parallel. It tracks its lineage — how it was derived
from other RDDs — rather than materializing data.

  ┌────────────────────────────────────────────────────────────┐
  │  RDD[T]                                                    │
  │                                                            │
  │  Partitions:   [P0, P1, P2, ..., Pn]                      │
  │  Dependencies: parent RDDs + dependency type               │
  │  Compute:      function to compute partition from parents  │
  │  Partitioner:  (optional) how keys are distributed         │
  │  Preferred     (optional) data locality hints              │
  │  Locations:    (which nodes have this partition's data)     │
  └────────────────────────────────────────────────────────────┘

Two types of dependencies:
──────────────────────────

Narrow (pipelineable):             Wide (requires shuffle):
  Each parent partition maps to     Each parent partition maps to
  at most ONE child partition.      MULTIPLE child partitions.

  Parent:  [P0] [P1] [P2]          Parent:  [P0] [P1] [P2]
            │    │    │                      /│\  /│\  /│\
  Child:   [P0] [P1] [P2]          Child:  [P0] [P1] [P2]

  Examples: map, filter, flatMap    Examples: groupByKey, reduceByKey,
            union (of aligned)                join, repartition, distinct

  Pipelined within same stage.      Stage boundary — requires shuffle.
  Failure: recompute one partition.  Failure: recompute all parent partitions
                                     that fed this partition.
```

### Lazy Evaluation

```
Transformations vs Actions:
═══════════════════════════

Transformations (lazy — build the DAG, do NOT execute):
  map, filter, flatMap, mapPartitions, union, join, groupByKey,
  reduceByKey, sortByKey, repartition, coalesce, distinct

Actions (eager — trigger execution of the DAG):
  count, collect, take, first, saveAsTextFile, foreach,
  reduce, aggregate, countByKey

Why lazy evaluation matters:
  val rdd = sc.textFile("hdfs://logs/")     // nothing happens
    .filter(_.contains("ERROR"))            // nothing happens
    .map(_.split("\t")(2))                  // nothing happens
    .distinct()                             // nothing happens

  rdd.count()  // NOW the entire pipeline executes

  Benefits:
  1. Optimizer can fuse transformations (pipeline map + filter)
  2. Can eliminate dead branches (transformation chain never used)
  3. Can push predicates down (filter before map)
  4. Only materializes what's needed for the action
```

### Persistence and Caching

```
Storage levels for RDD persistence:
════════════════════════════════════

rdd.persist(StorageLevel.MEMORY_ONLY)      // default for .cache()
rdd.persist(StorageLevel.MEMORY_AND_DISK)
rdd.persist(StorageLevel.DISK_ONLY)
rdd.persist(StorageLevel.MEMORY_ONLY_SER)  // serialized, less memory
rdd.persist(StorageLevel.MEMORY_ONLY_2)    // replicated to 2 nodes

┌──────────────────────┬───────┬──────┬────────┬──────────────────┐
│ StorageLevel         │ Memory│ Disk │ Ser.   │ Trade-off        │
├──────────────────────┼───────┼──────┼────────┼──────────────────┤
│ MEMORY_ONLY          │ ✓     │ ✗    │ desrlzd│ Fastest, most RAM│
│ MEMORY_AND_DISK      │ ✓     │ ✓    │ desrlzd│ Spills to disk   │
│ MEMORY_ONLY_SER      │ ✓     │ ✗    │ srlzd  │ Less RAM, CPU    │
│ MEMORY_AND_DISK_SER  │ ✓     │ ✓    │ srlzd  │ Best general     │
│ DISK_ONLY            │ ✗     │ ✓    │ srlzd  │ Largest datasets │
└──────────────────────┴───────┴──────┴────────┴──────────────────┘

When to cache:
  ✓ RDD used in multiple actions (iterative ML, interactive queries)
  ✓ RDD is expensive to recompute (large join result)
  ✗ RDD used only once (caching wastes memory)
  ✗ RDD is small enough that recomputation is cheap

When cache is evicted (LRU per executor):
  Spark drops the least-recently-used partition.
  If persist(MEMORY_AND_DISK): evicted partition spills to disk.
  If persist(MEMORY_ONLY): evicted partition is recomputed from lineage.
```

---

## 6. Spark SQL and the DataFrame API

### From RDDs to DataFrames

```
Evolution of Spark APIs:
════════════════════════

RDD API (Spark 1.0, 2014):
  Low-level, untyped transformations on JVM objects.
  No optimization — user controls everything.
  rdd.filter(lambda x: x.age > 30).map(lambda x: (x.name, x.age))

DataFrame API (Spark 1.3, 2015):
  Structured data with named columns and types.
  Optimized by Catalyst query optimizer.
  df.filter(df.age > 30).select("name", "age")

Dataset API (Spark 1.6, 2016):
  Typed DataFrame (Scala/Java only, compile-time type safety).
  ds.filter(_.age > 30).map(p => (p.name, p.age))

Spark SQL (all versions):
  spark.sql("SELECT name, age FROM people WHERE age > 30")
  → Same Catalyst optimizer, same Tungsten execution.

  ┌────────────────────────────────────────────────────────┐
  │  All three APIs compile down to the SAME physical plan │
  │  through Catalyst. The choice is about ergonomics,     │
  │  not performance.                                      │
  └────────────────────────────────────────────────────────┘
```

### DataFrame Execution Path

```
From DataFrame to Execution:
═════════════════════════════

df = spark.read.parquet("s3://data/orders") \
    .filter(col("status") == "completed") \
    .groupBy("region") \
    .agg(sum("amount").alias("total"))

Step 1: Unresolved Logical Plan
  'Aggregate [region], [region, sum(amount) AS total]
    'Filter (status = "completed")
      'Relation [orders]  (unresolved — column names not yet verified)

Step 2: Analyzed Logical Plan (catalog lookup)
  Aggregate [region#5], [region#5, sum(amount#3) AS total#10]
    Filter (status#4 = "completed")
      Relation [orders] [id#1, item_id#2, amount#3, status#4, region#5]

Step 3: Optimized Logical Plan (Catalyst rules)
  Aggregate [region#5], [region#5, sum(amount#3) AS total#10]
    Project [amount#3, region#5]              ← column pruning
      Filter (status#4 = "completed")
        Relation [orders] [amount, status, region]  ← pushed down to scan

Step 4: Physical Plan (strategy selection)
  HashAggregate(keys=[region], functions=[sum(amount)])
    Exchange hashpartitioning(region, 200)    ← shuffle
      HashAggregate(keys=[region], functions=[partial_sum(amount)])
        Filter (status = "completed")
          FileScan parquet [amount, status, region]
            PushedFilters: [IsNotNull(status), EqualTo(status,completed)]
            ReadSchema: struct<amount:double,status:string,region:string>

Step 5: Code Generation (Tungsten)
  → Whole-stage code generation: fuse scan + filter + partial agg
    into a single tight loop with no virtual method calls
```

---

## 7. Catalyst Optimizer Deep Dive

### Rule-Based Optimization

```
Catalyst applies transformation rules in phases:
═════════════════════════════════════════════════

Phase 1: Analysis (resolve references)
  Resolve column names against catalog schema.
  Resolve function names (sum, count, etc.).
  Type coercion (int + double → double).

Phase 2: Logical Optimization (rule-based, 100+ rules)
  Key rules:
  ┌──────────────────────────┬──────────────────────────────────────┐
  │ Rule                     │ What it does                         │
  ├──────────────────────────┼──────────────────────────────────────┤
  │ Predicate Pushdown       │ Move filters before joins/aggregates │
  │ Column Pruning           │ Drop unused columns early            │
  │ Constant Folding         │ Evaluate constant expressions at     │
  │                          │ compile time: 1+2 → 3               │
  │ Boolean Simplification   │ x AND true → x; x OR false → x     │
  │ Combine Filters          │ filter(a).filter(b) → filter(a AND b)│
  │ Combine Limits           │ limit(10).limit(5) → limit(5)       │
  │ Combine Unions           │ Flatten nested UNIONs               │
  │ Replace Distinct with    │ DISTINCT → Aggregate without agg    │
  │  Aggregate               │ functions                            │
  │ Null Propagation         │ Eliminate branches where result is   │
  │                          │ always null                          │
  │ Subquery Elimination     │ Decorrelate subqueries into joins   │
  └──────────────────────────┴──────────────────────────────────────┘

Phase 3: Physical Planning (strategy selection)
  Choose implementations for logical operators:
    - Join: BroadcastHashJoin vs SortMergeJoin vs ShuffledHashJoin
    - Aggregate: HashAggregate vs SortAggregate
    - Scan: FileScan with predicate pushdown vs full scan

Phase 4: Code Generation (Tungsten)
  Generate Java bytecode for tight inner loops.
```

### Join Strategy Selection

```
Spark's join strategies (ordered by preference):
═════════════════════════════════════════════════

1. Broadcast Hash Join (BHJ)
   Condition: one side < spark.sql.autoBroadcastJoinThreshold (10 MB default)
   Mechanism: broadcast small table to all executors, build hash map, probe
   Cost: O(n) — no shuffle, no sort
   ┌──────────────┐
   │  Small table  │ ──broadcast──► all executors
   │  (< 10 MB)    │               build hash table
   └──────────────┘               probe with large table
                                  → NO SHUFFLE of large table

2. Sort-Merge Join (SMJ)
   Condition: both sides are large, equi-join
   Mechanism: shuffle both sides by join key, sort, merge
   Cost: O(n log n) shuffle + sort, but handles any size
   ┌──────────────┐     shuffle     ┌──────────────┐
   │  Left table  │ ───by key───►  │ Sorted left   │
   └──────────────┘                └──────┬────────┘
   ┌──────────────┐     shuffle           │ merge
   │  Right table │ ───by key───►  ┌──────┴────────┐
   └──────────────┘                │ Sorted right  │
                                   └───────────────┘

3. Shuffled Hash Join (SHJ)
   Condition: one side fits in memory after shuffle partitioning
   Mechanism: shuffle both sides, build hash table on smaller side per partition
   Cost: O(n) per partition, but requires memory for hash table

4. Broadcast Nested Loop Join (BNLJ)
   Condition: non-equi join (theta join), one side is small
   Cost: O(n × m) — last resort
   Used for: range joins, inequality joins

5. Cartesian Product
   Condition: no join condition at all
   Cost: O(n × m) — avoid at all costs
```

---

## 8. Tungsten Execution Engine

### The Motivation

```
JVM overhead that Tungsten eliminates:
══════════════════════════════════════

Standard Java objects:
  class Person { String name; int age; }

  Memory layout in JVM:
  ┌──────────────────────────────────────────┐
  │ Object header (16 bytes)                 │  ← class pointer, hash, GC flags
  ├──────────────────────────────────────────┤
  │ name: String reference (8 bytes)         │  ← points to another object
  │   └── String object:                     │
  │       ├── header (16 bytes)              │
  │       ├── char[] reference (8 bytes)     │
  │       │   └── char[] object:             │
  │       │       ├── header (16 bytes)      │
  │       │       ├── length (4 bytes)       │
  │       │       └── data: "Alice" (10 B)   │
  │       ├── hash (4 bytes)                 │
  │       └── padding (4 bytes)              │
  ├──────────────────────────────────────────┤
  │ age: int (4 bytes)                       │
  │ padding (4 bytes)                        │
  └──────────────────────────────────────────┘

  Total for one Person("Alice", 30):  ~120 bytes
  Actual useful data:                 ~9 bytes (5 chars + 4 byte int)
  Overhead: ~13x

Tungsten binary format:
  ┌──────────────────────────────────┐
  │ null bitmap: 0b00 (2 bits)       │  ← no nulls
  │ field 1 offset: 16 (4 bytes)     │
  │ field 2 value: 30 (4 bytes)      │  ← int stored inline
  │ field 1 length: 5 (4 bytes)      │
  │ field 1 data: "Alice" (5 bytes)  │
  │ padding (3 bytes)                │
  └──────────────────────────────────┘

  Total: ~24 bytes
  5x less memory, no GC pressure, cache-friendly layout.
```

### Whole-Stage Code Generation

```
Without codegen (Volcano model):
  Each operator is a virtual method call per row.

  while (filter.hasNext()) {
    Row row = filter.next();         // virtual call → Filter.next()
      Row inner = scan.next();       // virtual call → Scan.next()
      if (inner.get("status") == "completed")  // type dispatch
        return inner;
  }

  Per row: 2 virtual calls + boxing/unboxing + type dispatch
  At 1 billion rows: virtual call overhead dominates.

With whole-stage codegen (Tungsten):
  Spark generates ONE tight loop for the entire stage:

  // Generated Java code (simplified):
  while (scan.hasNextBatch()) {
    ColumnarBatch batch = scan.nextBatch();
    for (int i = 0; i < batch.numRows(); i++) {
      // Inline filter (no virtual call)
      if (batch.column(3).getUTF8String(i).equals("completed")) {
        // Inline projection (no virtual call)
        long amount = batch.column(2).getLong(i);
        // Inline partial aggregate (no virtual call)
        sum += amount;
        count++;
      }
    }
  }

  Eliminates: virtual dispatch, boxing, row-at-a-time overhead.
  Operates on columnar batches (1024 rows), SIMD-friendly.
  Compiled by JVM JIT → machine code with loop unrolling, vectorization.

  Speedup: 2-10x over interpreted Volcano for scan-heavy workloads.
```

---

## 9. Shuffle Internals

### Spark Shuffle Architecture

```
Shuffle is the most expensive operation in Spark:
══════════════════════════════════════════════════

Lifecycle of a shuffle:

Map side (shuffle write):
  1. Each task processes one partition of the input RDD
  2. For each record: compute target partition = hash(key) % numReducePartitions
  3. Buffer records in a PartitionedAppendOnlyMap (in-memory hash map)
  4. When buffer fills (spark.shuffle.spill.initialMemoryThreshold = 5 MB):
     → Sort by (partition, key) → spill to local disk as sorted run
  5. At end of task: merge all spills into one sorted shuffle file
     + write an index file (byte offset of each partition)

  Shuffle file layout:
  ┌────────────────────────────────────────────────────────┐
  │  shuffle_0_0_0.data                                    │
  │  ┌──────────┬──────────┬──────────┬──────────────────┐│
  │  │Partition │Partition │Partition │ ...              ││
  │  │   0      │   1      │   2      │ (sorted by key) ││
  │  └──────────┴──────────┴──────────┴──────────────────┘│
  │                                                        │
  │  shuffle_0_0_0.index                                   │
  │  [0, 15728640, 31457280, ...]  ← byte offset per part │
  └────────────────────────────────────────────────────────┘

Reduce side (shuffle read):
  1. Reducer asks MapOutputTracker: "where are my partitions?"
  2. Fetch partition data from all map tasks (via BlockTransferService):
     → local fetch (same executor): direct memory copy
     → remote fetch (different executor): netty TCP transfer
  3. Merge fetched blocks (external merge sort if too large for memory)
  4. Feed merged sorted stream to the reduce-side operator

Network pattern (all-to-all):
  M map tasks × R reduce partitions = M × R data transfers

  ┌──────┐ ┌──────┐ ┌──────┐
  │Map 0 │ │Map 1 │ │Map 2 │
  │┌─┬─┬─┐│┌─┬─┬─┐│┌─┬─┬─┐
  ││0│1│2│││0│1│2│││0│1│2│  ← partitions
  │└─┴─┴─┘│└─┴─┴─┘│└─┴─┴─┘
  └──┼─┼─┼┘└─┼──┼─┼┘└─┼─┼─┼┘
     │ │ │   │  │ │   │ │ │
     │ │ └───│──│─┘   │ │ │
     │ └──── │──┘     │ │ │
     │       │   ┌────┘ │ │
     │       │   │ ┌────┘ │
     ▼       ▼   ▼ ▼      ▼
  ┌──────┐ ┌──────┐ ┌──────┐
  │Red 0 │ │Red 1 │ │Red 2 │
  └──────┘ └──────┘ └──────┘

  3 mappers × 3 reducers = 9 network transfers.
  1000 mappers × 200 reducers = 200,000 transfers.
```

### Shuffle Implementations

```
Spark shuffle implementations:
══════════════════════════════

SortShuffleManager (default since Spark 1.2):
  Map output sorted by partition + key.
  One file per map task (consolidated).
  Good for large shuffles, external sort for spills.

BypassMergeSortShuffleWriter (optimization):
  When: no map-side combine AND numReducePartitions < 200 (configurable)
  Writes one file per reduce partition, then concatenates.
  Avoids sorting overhead — faster for small partition counts.

Tungsten UnsafeShuffleWriter:
  Uses Tungsten binary format (off-heap, no Java objects).
  8-byte key prefix for sorting (avoids deserializing full keys).
  2-3x faster for large shuffles with simple keys.

Push-Based Shuffle (Spark 3.2+, external shuffle service):
  Map tasks PUSH shuffle data to remote shuffle service.
  Shuffle service pre-merges data by partition.
  Reducers read pre-merged data → fewer fetches.
  Reduces the M × R problem to M + R transfers.
```

---

## 10. Memory Management and Spilling

### Unified Memory Management

```
Spark Unified Memory Manager (since Spark 1.6):
════════════════════════════════════════════════

Total executor memory: spark.executor.memory (e.g., 8 GB)

  ┌──────────────────────────────────────────────────────┐
  │                  JVM Heap (8 GB)                      │
  │                                                      │
  │  ┌────────────────────────────────────────────────┐  │
  │  │  Reserved Memory (300 MB) — Spark internals    │  │
  │  └────────────────────────────────────────────────┘  │
  │                                                      │
  │  ┌────────────────────────────────────────────────┐  │
  │  │  User Memory (40%)                             │  │
  │  │  Data structures, UDFs, RDD dependencies       │  │
  │  │  NOT managed by Spark                          │  │
  │  └────────────────────────────────────────────────┘  │
  │                                                      │
  │  ┌────────────────────────────────────────────────┐  │
  │  │  Unified Memory (60% = spark.memory.fraction)  │  │
  │  │                                                │  │
  │  │  ┌─────────────────┬─────────────────────────┐│  │
  │  │  │  Storage Memory │  Execution Memory       ││  │
  │  │  │  (50%)          │  (50%)                  ││  │
  │  │  │                 │                         ││  │
  │  │  │  Cached RDDs,   │  Shuffles, joins,       ││  │
  │  │  │  broadcasts,    │  sorts, aggregations    ││  │
  │  │  │  DataFrames     │                         ││  │
  │  │  │                 │                         ││  │
  │  │  │  ←─── can borrow from each other ───→     ││  │
  │  │  └─────────────────┴─────────────────────────┘│  │
  │  └────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────┘

  Key: execution can evict storage (cached data) when it needs memory,
  but storage cannot evict execution. This prevents OOM during shuffles.

Off-heap memory (spark.memory.offHeap.enabled):
  Tungsten manages memory outside the JVM heap.
  Benefits: no GC pauses, precise memory accounting.
  Used for: shuffle buffers, sort buffers, hash maps.
```

### Spilling

```
When memory runs out during a shuffle or sort:
═══════════════════════════════════════════════

1. Spark tries to acquire execution memory
2. If unified pool is full → evict cached RDD partitions (storage)
3. If still not enough → SPILL current in-memory data to disk

Spill mechanics:
  In-memory hash map for aggregation:
  ┌──────────────────────────────────────┐
  │  {key1: partial_agg, key2: ...}      │  ← 500 MB in memory
  └──────────────────────────────────────┘
                    │ memory pressure
                    ▼
  Sort by key → write sorted run to local disk
  ┌──────────────────────────────────────┐
  │  /tmp/spark-xxx/spill-0.data         │  ← sorted, compressed
  └──────────────────────────────────────┘
  Clear in-memory map → continue processing → fill again → spill again
  ┌──────────────────────────────────────┐
  │  /tmp/spark-xxx/spill-1.data         │
  └──────────────────────────────────────┘
                    │
                    ▼ at end of task
  External merge sort of all spill files + remaining in-memory data
  → final sorted output

  Spill is NOT a failure — it's by design. But it's 100x slower than
  in-memory processing, so tuning to minimize spills is critical.

  Key tuning knobs:
  • spark.executor.memory — more memory = fewer spills
  • spark.sql.shuffle.partitions — more partitions = less data per task
  • spark.memory.fraction — 0.6 default, increase if not caching much
```

---

## 11. Spark on Kubernetes and YARN

### Spark on YARN

```
YARN deployment (still the dominant mode in Hadoop shops):
══════════════════════════════════════════════════════════

Cluster mode (production):
  spark-submit --master yarn --deploy-mode cluster \
    --num-executors 50 \
    --executor-memory 8g \
    --executor-cores 4 \
    --driver-memory 4g \
    myapp.jar

  1. Client submits to YARN ResourceManager
  2. RM launches ApplicationMaster (AM) in a container ← this IS the driver
  3. AM requests executor containers from RM
  4. RM allocates containers on NodeManagers (data locality aware)
  5. AM launches executors in containers
  6. Executors register back with AM/driver
  7. Driver sends tasks to executors

  Dynamic allocation (spark.dynamicAllocation.enabled):
  → Executors scale up/down based on pending tasks.
  → Idle executors released after spark.dynamicAllocation.executorIdleTimeout.
  → New executors requested when tasks queue up.
```

### Spark on Kubernetes

```
Kubernetes deployment (growing rapidly, 2025+):
════════════════════════════════════════════════

spark-submit --master k8s://https://k8s-api:6443 \
  --deploy-mode cluster \
  --conf spark.kubernetes.container.image=myrepo/spark:3.5 \
  --conf spark.executor.instances=50 \
  --conf spark.executor.memory=8g \
  --conf spark.executor.cores=4 \
  myapp.jar

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  Kubernetes Cluster                                     │
  │                                                         │
  │  ┌────────────┐                                         │
  │  │  Driver Pod │ ← created by spark-submit              │
  │  │  (Spark AM) │                                        │
  │  └──────┬─────┘                                         │
  │         │ creates executor pods via K8s API              │
  │         │                                               │
  │  ┌──────▼──────┐ ┌──────────────┐ ┌──────────────┐     │
  │  │Executor Pod │ │Executor Pod  │ │Executor Pod  │     │
  │  │   0         │ │   1          │ │   2          │     │
  │  └─────────────┘ └──────────────┘ └──────────────┘     │
  │                                                         │
  │  Shuffle data: local PVC or external shuffle service    │
  └─────────────────────────────────────────────────────────┘

  Advantages over YARN:
  ✓ Unified infrastructure (same cluster for all workloads)
  ✓ Better resource isolation (pod-level cgroups)
  ✓ Container images instead of classpath management
  ✓ Native auto-scaling (HPA/VPA/Karpenter)
  ✓ Multi-tenant by namespace

  Challenges:
  ✗ No data locality (S3/GCS replaces HDFS — locality is irrelevant)
  ✗ Shuffle on local ephemeral storage (pod restarts lose shuffle data)
  ✗ Needs external shuffle service or remote shuffle for resilience
```

---

## 12. Fault Tolerance and Lineage

### RDD Lineage Recovery

```
Spark's fault tolerance model:
══════════════════════════════

MapReduce:  replicates data (HDFS 3x replication) for fault tolerance.
Spark:      replicates COMPUTATION (lineage) instead of data.

  If a partition is lost (executor crash), Spark recomputes it by
  replaying the lineage (the sequence of transformations) from the
  nearest materialized ancestor.

Example:
  rdd1 = sc.textFile("hdfs://input/")           ← source (on HDFS, durable)
  rdd2 = rdd1.flatMap(_.split(" "))              ← narrow dependency
  rdd3 = rdd2.map((_, 1))                        ← narrow dependency
  rdd4 = rdd3.reduceByKey(_ + _)                 ← wide dependency (shuffle)
  rdd5 = rdd4.filter(_._2 > 10)                  ← narrow dependency

  Lineage DAG:
  rdd1 ──► rdd2 ──► rdd3 ──► rdd4 ──► rdd5
                              ▲
                              │ shuffle barrier

  If Executor 2 crashes and loses partitions of rdd5:
  1. rdd5 has narrow dependency on rdd4 → need rdd4 partitions
  2. rdd4 is a shuffle output → check if shuffle files exist on disk
     a. If shuffle files survive (external shuffle service): recompute
        only rdd5 partitions from shuffle files
     b. If shuffle files lost: must recompute rdd3 → rdd4 (re-shuffle)
        → must recompute rdd2 → rdd3
        → must re-read rdd1 from HDFS

  Cost of failure WITHOUT checkpointing:
    → Recompute from source. For 10-stage pipeline: recompute ALL stages.

  Cost with checkpointing:
    rdd4.checkpoint()  ← materialize to HDFS before continuing
    → Failure in rdd5: recompute from rdd4 checkpoint (1 stage, not 4).
```

### Shuffle File Persistence

```
External Shuffle Service (ESS):
═══════════════════════════════

Problem: when an executor dies, its local shuffle files die with it.
Downstream stages must re-run the upstream stage to regenerate shuffle data.

Solution: External Shuffle Service — a long-running process on each node
that serves shuffle files independently of executor lifecycle.

  Without ESS:                        With ESS:
  ┌──────────┐                       ┌──────────┐
  │ Executor │ ← dies                │ Executor │ ← dies
  │ shuffle  │ ← lost!               │ shuffle  │ ← written to
  │ files    │                       │ files    │   local disk
  └──────────┘                       └────┬─────┘
                                          │ served by
                                     ┌────▼─────┐
                                     │ ESS proc │ ← survives executor death
                                     │ (node)   │
                                     └──────────┘

  On YARN: NodeManager shuffle auxiliary service.
  On K8s: separate DaemonSet process, or remote shuffle service
          (Apache Celeborn, LinkedIn Magnet, Uber Zeus).
```

---

## 13. Adaptive Query Execution (AQE)

### Runtime Query Re-Optimization

```
AQE (Spark 3.0+, default enabled in 3.2+):
═══════════════════════════════════════════

Traditional query optimization:
  Plan is fixed BEFORE execution based on estimated statistics.
  If stats are wrong (stale, missing, skewed) → bad plan → slow query.

AQE:
  Re-optimize the plan DURING execution, between shuffle stages,
  using ACTUAL runtime statistics from completed stages.

  ┌─ Stage 0 (scan + filter) ─┐
  │  Runs, produces shuffle    │
  │  output with REAL stats:   │
  │  • actual partition sizes  │
  │  • actual row counts       │
  │  • data distribution       │
  └────────────┬───────────────┘
               │ AQE checks stats
               ▼
  ┌─ Re-optimization ─────────────────────────────────────┐
  │  1. Coalesce small shuffle partitions (reduce tasks)   │
  │  2. Switch join strategy (SMJ → BHJ if one side small) │
  │  3. Handle skewed partitions (split large partitions)  │
  └─────────────────────────────────┬─────────────────────┘
                                    │
  ┌─ Stage 1 (re-optimized) ───────▼──┐
  │  Executes with better plan         │
  └────────────────────────────────────┘
```

### AQE Features

```
1. Coalescing Shuffle Partitions:
═════════════════════════════════

Before AQE:
  spark.sql.shuffle.partitions = 200 (static)
  After filter that removes 95% of data: 200 partitions, most nearly empty.
  190 tasks run in ~10ms each (overhead > useful work).

After AQE:
  AQE sees actual partition sizes after shuffle write.
  Merges adjacent small partitions until each ≥ advisoryPartitionSizeInBytes.
  200 partitions → 15 partitions. 185 fewer tasks.

  spark.sql.adaptive.advisoryPartitionSizeInBytes = 64MB (default)
  spark.sql.adaptive.coalescePartitions.minPartitionSize = 1MB

2. Converting Sort-Merge Join to Broadcast Hash Join:
═════════════════════════════════════════════════════

Before AQE:
  Table stats say: orders = 50 GB, regions = 500 MB
  Optimizer picks SortMergeJoin (both sides > 10 MB threshold).

At runtime:
  After filter on orders: only 8 MB survives.
  AQE: 8 MB < 10 MB broadcast threshold → switch to BroadcastHashJoin.
  → Eliminates one entire shuffle stage.

3. Skew Join Optimization:
══════════════════════════

Before AQE:
  Join on user_id. User "bot_account" has 10M rows.
  Other users: ~100 rows each.
  One reducer gets 10M rows → takes 10 minutes.
  All other reducers: finish in 5 seconds.

After AQE:
  AQE detects partition with 10M rows (> skewedPartitionThresholdInBytes).
  Splits the skewed partition into sub-partitions.
  Replicates the matching partition from the other side.
  → 10M rows processed by 10 tasks of 1M each → 10x faster.

  ┌──────────────────────────────────────────────────────────┐
  │  Without skew handling:                                  │
  │  ┌──────┐ ┌──────┐ ┌──────────────────────────────────┐ │
  │  │  1K  │ │  1K  │ │      10,000K (skewed!)            │ │
  │  │ 5 sec│ │ 5 sec│ │      10 minutes                   │ │
  │  └──────┘ └──────┘ └──────────────────────────────────┘ │
  │  Job time: 10 minutes (bottlenecked on one task)        │
  │                                                          │
  │  With AQE skew handling:                                 │
  │  ┌──────┐ ┌──────┐ ┌────┐ ┌────┐ ┌────┐ ... ┌────┐    │
  │  │  1K  │ │  1K  │ │1000K│ │1000K│ │1000K│    │1000K│    │
  │  │ 5 sec│ │ 5 sec│ │ 1m │ │ 1m │ │ 1m │    │ 1m │    │
  │  └──────┘ └──────┘ └────┘ └────┘ └────┘ ... └────┘    │
  │  Job time: 1 minute                                     │
  └──────────────────────────────────────────────────────────┘
```

---

## 14. Data Skew and Performance Tuning

### Identifying Skew

```
Symptoms of data skew:
══════════════════════

  Spark UI → Stages tab → one task takes 100x longer than others.

  Task Duration Summary:
  ┌───────────┬──────────┬──────────┬──────────┬──────────┐
  │ Metric    │ Min      │ 25th pct │ Median   │ Max      │
  ├───────────┼──────────┼──────────┼──────────┼──────────┤
  │ Duration  │ 2 sec    │ 5 sec    │ 8 sec    │ 45 min   │ ← MAX is the problem
  │ Input     │ 10 MB    │ 64 MB    │ 80 MB    │ 50 GB    │ ← one partition is huge
  │ Records   │ 100K     │ 500K     │ 800K    │ 200M     │
  └───────────┴──────────┴──────────┴──────────┴──────────┘

  Root cause: hot keys in groupBy/join.
  Common offenders:
  • null values → all nulls hash to same partition
  • bot/crawler traffic → one user_id has 90% of events
  • default values → category_id = 0, region = "UNKNOWN"
  • power-law distributions → Zipf (1% of keys = 50% of data)
```

### Skew Mitigation Strategies

```
1. AQE Skew Join (automatic, Spark 3.0+)
   spark.sql.adaptive.skewJoin.enabled = true (default)
   spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes = 256MB

2. Salting (manual, universal technique)
   Add random prefix to the hot key to distribute it across partitions.

   -- Before: all "bot_account" rows go to one partition
   SELECT user_id, COUNT(*) FROM events GROUP BY user_id

   -- After: "bot_account" spread across 10 partitions
   SELECT
     regexp_replace(salted_user_id, '^[0-9]+_', '') AS user_id,
     SUM(cnt) AS total
   FROM (
     SELECT
       CONCAT(FLOOR(RAND() * 10), '_', user_id) AS salted_user_id,
       COUNT(*) AS cnt
     FROM events
     GROUP BY CONCAT(FLOOR(RAND() * 10), '_', user_id)
   )
   GROUP BY regexp_replace(salted_user_id, '^[0-9]+_', '')

3. Isolate + Union (split hot and cold paths)
   val hotKeys = Set("bot_account", "null", "unknown")
   val coldDF = df.filter(!col("user_id").isin(hotKeys: _*))
   val hotDF  = df.filter(col("user_id").isin(hotKeys: _*))

   val coldResult = coldDF.groupBy("user_id").count()
   val hotResult  = hotDF.repartition(100).groupBy("user_id").count()
   val result     = coldResult.union(hotResult)

4. Broadcast join to avoid shuffle entirely
   If the skewed table is the large side of a join:
   spark.sql.autoBroadcastJoinThreshold = 100MB  // increase threshold
   Or: df.join(broadcast(smallDF), "key")

5. Filter nulls before join (nulls are a common skew source)
   df.filter(col("join_key").isNotNull).join(...)
```

### Key Tuning Parameters

```
Critical Spark configuration for production:
═════════════════════════════════════════════

┌─────────────────────────────────────────┬──────────────┬──────────────────┐
│ Parameter                               │ Default      │ Recommendation   │
├─────────────────────────────────────────┼──────────────┼──────────────────┤
│ spark.sql.shuffle.partitions            │ 200          │ 2-3x cores total │
│ spark.sql.adaptive.enabled              │ true (3.2+)  │ true             │
│ spark.executor.memory                   │ 1g           │ 4-16g            │
│ spark.executor.cores                    │ 1            │ 4-5 (YARN)       │
│ spark.executor.memoryOverhead           │ 10% or 384MB │ 10-20%           │
│ spark.memory.fraction                   │ 0.6          │ 0.6-0.8          │
│ spark.sql.files.maxPartitionBytes       │ 128MB        │ 128-256MB        │
│ spark.sql.autoBroadcastJoinThreshold    │ 10MB         │ 50-100MB         │
│ spark.serializer                        │ JavaSerializer│ KryoSerializer  │
│ spark.sql.parquet.compression.codec     │ snappy       │ zstd             │
│ spark.dynamicAllocation.enabled         │ false        │ true             │
│ spark.speculation                       │ false        │ true (batch only)│
└─────────────────────────────────────────┴──────────────┴──────────────────┘

Memory sizing formula:
  executor_memory = (node_memory - OS_reserved - NodeManager) / executors_per_node
  executor_cores = 4-5 (more → GC pressure, less → poor parallelism)
  executors_per_node = (node_cores - 1) / executor_cores

  Example: 64 GB RAM, 16 cores per node:
    executors_per_node = (16 - 1) / 4 = 3
    executor_memory = (64 - 4 - 2) / 3 ≈ 19g
    executor_memoryOverhead = 19g × 0.1 ≈ 2g
    Total per executor: ~21g (fits in 64 GB / 3)
```

---

## 15. Batch vs Stream: Lambda and Kappa Architectures

### Lambda Architecture

```
Lambda Architecture (Nathan Marz, 2011):
════════════════════════════════════════

Run BOTH batch and stream pipelines, merge results at query time.

                    ┌───────────────────────────────────────┐
                    │          Incoming Events              │
                    └───────────┬───────────┬───────────────┘
                                │           │
              ┌─────────────────▼─┐   ┌─────▼─────────────────┐
              │   Batch Layer     │   │   Speed Layer          │
              │   (Spark, MR)     │   │   (Flink, Storm)       │
              │                   │   │                        │
              │   ┌─────────┐     │   │   ┌──────────────┐     │
              │   │ Master  │     │   │   │ Real-time     │     │
              │   │ Dataset │     │   │   │ views         │     │
              │   │ (HDFS)  │     │   │   │ (approximate) │     │
              │   └────┬────┘     │   │   └──────┬───────┘     │
              │        │          │   │          │             │
              │   ┌────▼────┐     │   │          │             │
              │   │ Batch   │     │   │          │             │
              │   │ views   │     │   │          │             │
              │   │ (exact) │     │   │          │             │
              │   └────┬────┘     │   │          │             │
              └────────┼──────────┘   └──────────┼─────────────┘
                       │                         │
              ┌────────▼─────────────────────────▼─────────────┐
              │              Serving Layer                     │
              │   Merge batch views + speed views at query time│
              │   batch_result UNION speed_result              │
              └────────────────────────────────────────────────┘

  Batch layer: reprocesses ALL data periodically (hourly/daily).
  Speed layer: processes events in real time (low latency, approximate).
  Serving layer: merges both for queries.

  When batch view is updated, speed view for that window is discarded
  (batch is the source of truth).

  Problems:
  ✗ Two codebases for the same logic (batch + stream)
  ✗ Operational complexity (maintain two systems)
  ✗ Semantic gaps (batch and stream may produce different results)
  ✗ Batch reprocessing latency (hours behind real-time)
```

### Kappa Architecture

```
Kappa Architecture (Jay Kreps, 2014):
═════════════════════════════════════

ONE pipeline (streaming), no batch layer.
For reprocessing: replay the event log with a new version of the streaming job.

                    ┌───────────────────────────────────────┐
                    │          Incoming Events              │
                    └───────────────────┬───────────────────┘
                                        │
                    ┌───────────────────▼───────────────────┐
                    │        Event Log (Kafka)              │
                    │        (immutable, retained)          │
                    └──────────┬──────────┬────────────────┘
                               │          │
                        ┌──────▼──┐  ┌────▼────┐
                        │ Job v2  │  │ Job v1  │
                        │ (new    │  │ (current│
                        │  logic) │  │  logic) │
                        └────┬────┘  └────┬────┘
                             │            │
                        ┌────▼────┐  ┌────▼────┐
                        │ Output  │  │ Output  │
                        │ v2      │  │ v1      │
                        └─────────┘  └─────────┘
                                          │
                               swap when v2 catches up

  Reprocessing = deploy Job v2 reading from beginning of Kafka log.
  When v2 catches up to real-time, swap serving from v1 → v2.
  Delete v1 output.

  Advantages:
  ✓ Single codebase (streaming only)
  ✓ Reprocessing uses same code path as real-time
  ✓ Simpler operations (one system)

  When Lambda still wins:
  • Reprocessing 5 years of history is faster in batch (Spark) than
    replaying through a streaming job
  • Complex ML training requires batch (multiple passes over data)
  • Some computations are inherently bounded (month-end close)
```

### The Modern Reality (2025)

```
In practice, most production systems use a hybrid:
══════════════════════════════════════════════════

  ┌──────────────────────────────────────────────────────────┐
  │  Lakehouse Architecture (pragmatic middle ground)        │
  │                                                          │
  │  ┌──────────────┐    ┌──────────────┐                    │
  │  │ Streaming    │    │ Batch        │                    │
  │  │ Flink / Spark│    │ Spark / dbt  │                    │
  │  │ Streaming    │    │              │                    │
  │  └──────┬───────┘    └──────┬───────┘                    │
  │         │                   │                            │
  │         └────────┬──────────┘                            │
  │                  ▼                                       │
  │         ┌────────────────────┐                           │
  │         │  Iceberg / Delta   │  ← SINGLE table format   │
  │         │  (same tables)     │    ACID on both paths     │
  │         └────────────────────┘                           │
  │                  │                                       │
  │         ┌────────▼────────────┐                          │
  │         │  Query Engines      │                          │
  │         │  Trino / Spark SQL  │                          │
  │         └─────────────────────┘                          │
  │                                                          │
  │  Streaming writes micro-batches to Iceberg (1-5 min).    │
  │  Batch jobs do heavy transformations on same tables.     │
  │  One table format, one set of queries, ACID everywhere.  │
  │  No separate "batch views" and "speed views" to merge.   │
  └──────────────────────────────────────────────────────────┘

  The lakehouse made the Lambda/Kappa debate largely moot.
  Use streaming for low-latency ingestion.
  Use batch for heavy transformations and backfills.
  Both write to the same Iceberg/Delta tables with ACID.
```

---

## 16. Production Patterns and Anti-Patterns

### Patterns

```
1. Idempotent Writes
   ──────────────────
   Every batch job should produce the same output if run twice.
   Use: OVERWRITE partition mode (replace entire partition atomically).
   Avoid: APPEND mode without deduplication (re-run = duplicates).

   df.write.mode("overwrite") \
     .partitionBy("date") \
     .parquet("s3://output/")

   With Iceberg: MERGE INTO for upsert semantics.
   MERGE INTO target USING source ON target.id = source.id
   WHEN MATCHED THEN UPDATE ...
   WHEN NOT MATCHED THEN INSERT ...

2. Partition-Based Incremental Processing
   ──────────────────────────────────────
   Don't reprocess all data every run. Only process new/changed partitions.

   last_processed = read_watermark()  # e.g., "2024-01-14"
   new_data = spark.read.parquet("s3://raw/events/") \
     .filter(col("date") > last_processed)
   # ... process ...
   update_watermark("2024-01-15")

3. Exactly-Once with External Commit
   ──────────────────────────────────
   Spark guarantees at-least-once internally. For exactly-once output:
   • Use transactional sinks (Iceberg/Delta MERGE)
   • Or: write output + watermark in same transaction
   • Or: deduplicate in downstream consumer

4. Speculative Execution
   ─────────────────────
   spark.speculation = true
   If one task is 1.5x slower than median, Spark launches a copy on another
   executor. Whichever finishes first wins. Kills stragglers.
   Critical for batch SLAs — one slow task can delay the entire job.

5. Data Quality Gates
   ──────────────────
   After transformation, before writing output:
   assert result.count() > 0, "Empty output — aborting"
   assert result.filter(col("amount") < 0).count() == 0, "Negative amounts"
   assert result.select("id").distinct().count() == result.count(), "Dups"
   # Only write if all checks pass
```

### Anti-Patterns

```
1. collect() on Large DataFrames
   ─────────────────────────────
   BAD:  result = df.collect()  # pulls ALL data to driver → OOM
   GOOD: df.write.parquet(...)  # write distributed, read downstream

2. UDFs Instead of Built-in Functions
   ───────────────────────────────────
   BAD:  df.withColumn("upper_name", udf(lambda x: x.upper())(col("name")))
         → serialization per row, Python→JVM roundtrip, no Catalyst optimization
   GOOD: df.withColumn("upper_name", upper(col("name")))
         → Tungsten codegen, vectorized, 10-100x faster

3. Shuffling Without Reason
   ─────────────────────────
   BAD:  df.repartition(200).groupBy("key").count()
         → unnecessary shuffle BEFORE the groupBy shuffle
   GOOD: df.groupBy("key").count()
         → one shuffle, not two

4. Reading and Writing to the Same Path
   ─────────────────────────────────────
   BAD:  df = spark.read.parquet("s3://data/table")
         df.filter(...).write.mode("overwrite").parquet("s3://data/table")
         → May read partially overwritten data → corruption
   GOOD: Write to staging path, then atomically swap.
         Or use Iceberg/Delta (transactional overwrites).

5. Too Few or Too Many Partitions
   ────────────────────────────────
   Too few:  10 partitions on 100 cores → 90 cores idle
   Too many: 100,000 partitions → scheduling overhead dominates
   Rule:     2-3 partitions per available core, each 100-256 MB

6. Ignoring spark.sql.adaptive.enabled
   ────────────────────────────────────
   In Spark 3.2+, AQE is enabled by default.
   In Spark 3.0-3.1, it's opt-in. Enable it. Always.
   It fixes partition counts, join strategies, and skew automatically.

7. Not Monitoring Shuffle Spill
   ─────────────────────────────
   Spark UI → Stages → Task Metrics → "Shuffle Spill (Disk)"
   If this is nonzero and large: you need more memory or more partitions.
   Target: zero disk spill for best performance.
```

---

*Next: Chapter 24 covers distributed caching (TinyLFU, ARC) as referenced in the roadmap. The batch processing patterns described here integrate with the stream processing foundation from [Chapter 22](./22-stream-processing-flink-watermarks-eos.md) — together they cover the full Lambda/Kappa spectrum. For the lakehouse table formats (Iceberg, Delta Lake) that unify batch and streaming output, see `databases/22-data-lake-lakehouse.md`.*
