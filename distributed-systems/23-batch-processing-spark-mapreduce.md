# Chapter 23: Batch Processing — Spark Architecture and the MapReduce Concept

A production-grade deep dive into batch processing for senior engineers who need to design large-scale data pipelines, ETL systems, and distributed computation frameworks. Covers MapReduce from first principles, Apache Spark's architecture and internals (DAG scheduler, Catalyst optimizer, Tungsten execution engine, shuffle mechanics), and the practical patterns that power petabyte-scale data processing in production.

Prerequisites: Distributed filesystems (HDFS/S3) from `14-distributed-filesystems-gfs-hdfs-ceph-juicefs.md` (roadmap), stream processing from `22-stream-processing-flink-watermarks-eos.md`, and sharding concepts from `10-sharding-and-consistent-hashing.md`. This chapter provides the batch processing foundation that complements the streaming chapter — together they cover the full spectrum of distributed data processing.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
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
17. [Interview Questions](#17-interview-questions)
18. [Real-world cases — incidents with numbers](#18-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** Some jobs have to read terabytes of data that are already sitting in storage: yesterday's orders, a year of logs, all customer profiles. One machine would take days. So we split the data into thousands of pieces and let hundreds of machines work at once. The hard parts are moving data between machines (the "shuffle"), keeping every machine equally busy, surviving machines that die halfway, and not running out of memory. MapReduce was the first popular framework for this. Spark is the one most teams use today.

**A real-world example.** An e-commerce company runs a nightly job: "revenue per product per region for yesterday." Numbers are illustrative but consistent.

- Input: 2 TB of order events in Parquet on S3. Also a 5 MB `regions` table and a 40 GB `products` table.
- Cluster: 20 nodes, each 16 cores and 64 GB RAM. 3 executors per node × 5 cores = 15 cores per node, so 300 tasks run at once. Each executor gets 17 GB heap + 2 GB overhead (§14).
- **Reading (§4, §6):** Spark cuts 2 TB into 128 MB splits → 16,384 read tasks → about 55 "waves" of 300 tasks. It reads only the 4 columns the query uses, not all 40.
- **Joins (§7):** `regions` is 5 MB, under the 10 MB broadcast threshold, so Spark copies it to every executor. The 2 TB side never crosses the network for that join. `products` is 40 GB, so that join needs a shuffle.
- **Shuffle sizing (§9, §13):** about 400 GB is shuffled. With the default 200 shuffle partitions, each task gets ~2 GB, which does not fit in its share of memory, so tasks spill to disk and crawl. Setting 3,200 partitions gives ~128 MB each (about 11 waves), and AQE merges any that turn out tiny.
- **Skew (§14):** 15% of orders have `product_id = 0` ("unknown"). That is 60 GB landing in one partition. 3,199 tasks finish in ~1 minute; one runs for over an hour. Fix: handle the `0` rows separately (they match no product anyway), or let AQE skew join split that partition into ~64 MB pieces (60 GB / 64 MB = 960 tasks).
- **Stragglers (§16):** one node has a failing disk and runs tasks 5× slower than the median. Speculative execution starts a copy of those tasks elsewhere; the first copy to finish wins.
- **Failures (§12):** an executor dies at 80%. Its shuffle files are served by the external shuffle service, so Spark reruns only that executor's unfinished tasks, not the whole job.
- **Re-runs (§16):** the job overwrites the `date=…` partition, so running it twice never doubles revenue.

Before these fixes the job took about 7 hours and missed the 06:00 deadline. After: about 1.5 hours (illustrative).

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| Batch job | process a complete, fixed pile of data, then stop | doing the monthly accounts once all receipts are in |
| Partition | one slice of the dataset, processed by one task | one stack of exam papers given to one grader |
| Map | work on each record alone, tag it with a key | each polling station counting its own ballots |
| Shuffle | move all records with the same key to the same machine | sorting mail into bags by destination city |
| Reduce | combine all records of one key | the city post office delivering its bag |
| Narrow dependency | output piece needs only one input piece; no data moves | each cook finishing their own dish |
| Wide dependency | output piece needs data from many input pieces; needs a shuffle | regrouping all dishes by table before serving |
| Stage | a chain of narrow steps between two shuffles | one leg of a relay race |
| Driver / executor | the planner process / the worker processes | head chef / line cooks |
| Lineage | the recipe of how each piece was made, used to rebuild lost pieces | keeping the recipe instead of a spare cake |
| Broadcast join | send a copy of the small table to every worker | giving every cashier a printed price list |
| Data skew | a few keys have far more data than the rest | one checkout lane with a 40-item cart queue |
| Salting | split a hot key into N sub-keys, then combine | opening 10 lanes just for the huge queue |
| Spill | writing partial results to local disk when memory is full | putting papers on the floor when the desk is full |
| AQE | Spark re-plans mid-job using real data sizes | a GPS rerouting after seeing actual traffic |
| Speculative execution | run a backup copy of a slow task | sending a second courier when the first is stuck |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| `M` | number of map tasks (input splits) | 100 – 100,000 | 2 TB / 128 MB = 16,384 |
| `R` | number of reduce tasks / shuffle partitions | 200 – 10,000 | 3,200 for a 400 GB shuffle |
| `M × R` | number of shuffle blocks to fetch | up to millions | 1,000 × 200 = 200,000 |
| `hash(key) % R` | which reduce partition a key goes to | — | `hash("user42") % 200 = 17` |
| `spark.sql.shuffle.partitions` | shuffle partitions for DataFrame/SQL | 200 (default) | 400 GB / 200 = 2 GB per task (too big) |
| `spark.sql.files.maxPartitionBytes` | max bytes per input split when reading files | 128 MB | 2 TB → 16,384 splits |
| `spark.sql.adaptive.enabled` | turn AQE on | true since Spark 3.2 | merges 200 tiny partitions into 16 |
| `advisoryPartitionSizeInBytes` | AQE's target size per shuffle partition | 64 MB | 1 GB shuffle → ~16 partitions |
| `skewedPartitionFactor` / `...ThresholdInBytes` | AQE calls a join partition skewed if > factor × median AND > threshold | 5 / 256 MB | 60 GB partition vs 128 MB median → skewed |
| `spark.sql.autoBroadcastJoinThreshold` | max table size for automatic broadcast join | 10 MB | 5 MB `regions` table is broadcast |
| `spark.executor.memory` | JVM heap per executor | 1 GB default; 4–16+ GB in practice | 17 GB |
| `spark.executor.memoryOverhead` | extra off-heap memory per container | max(10% of heap, 384 MB) | 17 GB heap → ~2 GB |
| `spark.executor.cores` | tasks one executor runs at once | 4 – 5 | 3 executors × 5 cores = 15 per node |
| Reserved memory | fixed memory Spark keeps for itself | 300 MB | 8 GB heap → 7,892 MB usable |
| `spark.memory.fraction` | share of usable heap for Spark's execution + storage | 0.6 | 0.6 × 7,892 MB ≈ 4.7 GB |
| `spark.memory.storageFraction` | part of that pool protected for cached data | 0.5 | ≈ 2.4 GB of 4.7 GB |
| Waves | rounds of tasks needed = tasks / task slots | 1 – 100 | 16,384 / 300 ≈ 55 |
| `spark.speculation`, multiplier, quantile | backup copies for slow tasks; "slow" = multiplier × median after quantile of tasks finish | off; 1.5 and 0.75 (≤ 3.5), 3 and 0.9 (4.0) | task at 5× median gets a copy |
| `mapreduce.task.io.sort.mb` | MapReduce map-side sort buffer | 100 MB | spills to disk when full |
| `spark.shuffle.sort.bypassMergeThreshold` | at or below this many partitions, skip sorting in shuffle write | 200 | 150 partitions → bypass writer |
| HDFS replication | copies of each block | 3 | 1 on writer's node, 2 on another rack |
| Salt count `N` | sub-keys a hot key is split into | 10 – 100 | `bot_account` → `0_bot…` … `9_bot…` |
| Shuffle spill (disk) | bytes written to disk because memory ran out (Spark UI) | 0 is ideal | 30 GB spill → add memory or partitions |
| Task duration max / median | how uneven tasks are; a sign of skew | < 3× is healthy | 45 min / 8 s → severe skew |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. Batch Processing Fundamentals

> **In plain words.** Batch processing means "take a pile of data that is already complete, crunch all of it, write the result." Nobody waits for an answer in real time, so the goal is to finish the whole pile as cheaply and reliably as possible, not to answer in milliseconds.
>
> **Real-world example.** An e-commerce site runs a job at 02:00 that reads yesterday's 2 TB of order events and writes revenue per product per region. If it finishes by 06:00, the finance dashboard is fresh for the morning. A streaming system would cost more and give the same number.

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
   For workloads that tolerate hourly latency, batch is often several
   times cheaper (exact ratio depends on cluster size and duty cycle).
```

---

## 2. MapReduce: The Original Distributed Batch Framework

> **In plain words.** MapReduce splits a huge job into many small pieces. "Map" looks at each record on its own and tags it with a key. The framework then moves all records with the same key to the same machine (the "shuffle"). "Reduce" combines each key's records into one answer.
>
> **Real-world example.** Counting votes in a national election: each polling station (map) counts its own ballots per candidate. The counts are sent to one regional office per candidate (shuffle). Each office adds up its candidate's numbers (reduce). 10,000 stations work in parallel; only small totals travel.

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
  • Reduce cannot start until ALL mappers finish (barrier synchronization;
    reducers may start COPYING early, but reduce() waits for the last map)
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

  Block placement (HDFS default replication = 3, default policy):
  Block B1 → Node 1 (writer's node, rack A), Node 7 and Node 8 (both on rack B)
  Map task for B1 → prefer a node holding B1 (node-local: 1, 7 or 8),
  then another node on rack A or B (rack-local), then any node (off-rack).
```

---

## 3. MapReduce Limitations and the Road to Spark

> **In plain words.** MapReduce writes every intermediate result to disk and reads it back for the next step. For jobs with many steps, or jobs that loop over the same data many times, most of the time is spent on disk I/O. Spark's fix: keep intermediate data in memory.
>
> **Real-world example.** A fraud model trained with 20 passes over a 100 GB dataset. MapReduce reads and writes 100 GB on every pass: 20 × 200 GB = 4 TB of disk traffic. Spark reads 100 GB once, caches it in cluster memory, and does the other 19 passes from RAM.

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
   20 iterations × (100 GB read + 100 GB write) = 4 TB of I/O for what
   should be in-memory.

3. No native support for interactive queries
   Each query is a new MR job: JVM startup, task scheduling, HDFS read.
   Even a simple COUNT(*) takes tens of seconds (there is no in-memory cache).

4. Only Java (practically)
   Hadoop Streaming allowed Python/Ruby, but with serialization overhead.

5. High operational complexity
   HDFS NameNode = single point of failure (before NameNode HA in Hadoop 2.x).
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
  │  (illustrative numbers, in line with the Spark papers)    │
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

> **In plain words.** One "driver" program plans the work and hands out tasks. Many "executors" (worker processes) do the tasks, one data partition per task. Work is cut into "stages" at every point where data must be moved between machines (a shuffle).
>
> **Real-world example.** A kitchen: the head chef (driver) reads the order and splits it into steps. 50 cooks (executors) chop vegetables in parallel, each with their own bowl (partition). When dishes must be regrouped by table (shuffle), everyone stops, passes plates, then the next step starts. Fewer regrouping points = faster service.

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
│  Cluster Manager (YARN / Kubernetes / Standalone; Mesos was  │
│  deprecated in Spark 3.2)                                    │
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

  (The global orderBy adds one more range-partitioning shuffle before the
   final sort; it is folded into Stage 3 here for brevity. If `items` were
   under 10 MB, Spark would broadcast it and Stage 1's shuffle would vanish.)

  Stage 0 and Stage 1 can run in PARALLEL (no dependency between them).
  Stage 2 waits for BOTH to complete (join requires both sides).
  Stage 3 waits for Stage 2.
```

---

## 5. The RDD Abstraction

> **In plain words.** An RDD is a big dataset split into pieces, plus the recipe for how each piece was made. Spark doesn't run the recipe until you ask for a result. If a machine dies and a piece is lost, Spark re-runs the recipe for just that piece instead of keeping backup copies.
>
> **Real-world example.** A 1 TB clickstream split into 8,000 partitions. `filter` and `map` are narrow: partition 17 only needs input partition 17, so if it is lost Spark redoes 1 of 8,000 pieces. `groupByKey` is wide: every output partition needs data from all 8,000 inputs, so that step costs a full shuffle.

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

  Note: an operation is narrow if the parent is ALREADY partitioned the
  right way, e.g. reduceByKey or join on RDDs that share the same
  partitioner needs no new shuffle.

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
  1. Spark can fuse transformations (pipeline map + filter in one task)
  2. Branches that no action uses are never computed
  3. With DataFrames/SQL, Catalyst can also reorder work (push filters
     down, prune columns). The RDD API does NOT reorder your lambdas.
  4. Only materializes what's needed for the action
```

### Persistence and Caching

```
Storage levels for RDD persistence:
════════════════════════════════════

rdd.persist(StorageLevel.MEMORY_ONLY)      // default for rdd.cache()
                                           // (DataFrame.cache() defaults
                                           //  to MEMORY_AND_DISK)
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

> **In plain words.** DataFrames are tables with named, typed columns. Because Spark can see which columns and filters you use, it can plan the work much better than with raw RDD code, like a database does with SQL.
>
> **Real-world example.** `orders.filter(status == 'completed').groupBy('region').sum('amount')` on a Parquet table with 40 columns: Spark reads only the 3 columns it needs and skips row groups that can't contain 'completed'. If the columns are similar in size, reading 3 of 40 cuts the bytes read by about 92%.

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
  │  DataFrame, Dataset and SQL all go through Catalyst;   │
  │  DataFrame code and the same SQL give the SAME plan.   │
  │  Caveat: typed Dataset lambdas (ds.map(p => ...)) are  │
  │  opaque to Catalyst, and the RDD API skips it entirely,│
  │  so those can be slower.                               │
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

> **In plain words.** Catalyst is Spark's query planner. It rewrites your query into an equivalent but cheaper one (filter earlier, drop unused columns) and then picks how to run each step, especially which join method to use.
>
> **Real-world example.** Joining 2 TB of orders with a 5 MB country table: Catalyst sees the small side is under 10 MB and sends a copy of it to every executor (broadcast join). The 2 TB side never moves over the network. Without that, both sides would be shuffled.

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

> **In plain words.** Tungsten makes each machine faster. It stores rows as compact bytes instead of Java objects (less memory, less garbage collection) and generates one tight loop for a whole stage instead of calling many small functions per row.
>
> **Real-world example.** A Person("Alice", 30) takes about 96 bytes as Java objects but 32 bytes as a Tungsten row. On 1 billion rows that is roughly 96 GB vs 32 GB of memory, which decides whether the data fits in cache or spills to disk.

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

  Total for one Person("Alice", 30):  ~96 bytes (32 + 32 + 32, 8-byte
                                      aligned, no compressed oops)
  Actual useful data:                 ~9 bytes (5 chars + 4 byte int)
  Overhead: ~11x

Tungsten binary format (UnsafeRow):
  ┌─────────────────────────────────────────────┐
  │ null bitset (8 bytes, 1 bit per field)      │  ← no nulls
  │ field 1 slot: offset + length of "Alice"    │  (8 bytes)
  │ field 2 slot: 30, int stored inline         │  (8 bytes)
  │ variable-length area: "Alice" (5 B + 3 pad) │  (8 bytes)
  └─────────────────────────────────────────────┘

  Total: 32 bytes, one contiguous block
  ~3x less memory, no per-object headers, no GC pressure, cache-friendly.
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
  Reads columnar batches from Parquet/ORC (4096 rows by default,
  spark.sql.parquet.columnarReaderBatchSize).
  Compiled by JVM JIT → machine code with loop unrolling, vectorization.

  Speedup: often several-fold over the Volcano model for CPU-bound
  scan/filter/aggregate stages (varies a lot by query).
```

---

## 9. Shuffle Internals

> **In plain words.** A shuffle regroups data by key across the cluster: every map task writes one file split by destination, and every reduce task pulls its slice from every map task. It is usually the slowest and most failure-prone part of a job.
>
> **Real-world example.** 1,000 map tasks and 200 reduce partitions means 1,000 × 200 = 200,000 small blocks to fetch. If 400 GB is shuffled, each reduce partition gets about 2 GB, which often does not fit in memory and spills to disk.

### Spark Shuffle Architecture

```
Shuffle is the most expensive operation in Spark:
══════════════════════════════════════════════════

Lifecycle of a shuffle:

Map side (shuffle write):
  1. Each task processes one partition of the input RDD
  2. For each record: compute target partition = hash(key) % numReducePartitions
  3. Buffer records in memory: a PartitionedAppendOnlyMap (hash map) if
     there is a map-side combine, otherwise a PartitionedPairBuffer
  4. The buffer starts tracking its size at 5 MB
     (spark.shuffle.spill.initialMemoryThreshold) and asks for more
     execution memory as it grows. When it cannot get more:
     → Sort by partition ID (and by key if aggregation/ordering is
       needed) → spill to local disk as a sorted run
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
  When: no map-side combine AND numReducePartitions ≤ 200
        (spark.shuffle.sort.bypassMergeThreshold)
  Writes one file per reduce partition, then concatenates.
  Avoids sorting overhead — faster for small partition counts.

Tungsten UnsafeShuffleWriter:
  Works on serialized Tungsten binary records (no Java objects).
  Sorts compact 8-byte entries (partition ID + record pointer) instead
  of deserialized records. Used when there is no map-side aggregation
  and the serializer supports relocating records (e.g. Kryo, or
  Spark SQL's UnsafeRow serializer).

Push-Based Shuffle (Spark 3.2+, YARN external shuffle service; based
  on LinkedIn's Magnet):
  Map tasks PUSH shuffle blocks to shuffle services on other nodes.
  Each service pre-merges blocks of the same reduce partition.
  Reducers read a few large merged files instead of M small blocks
  → far fewer small random disk reads and fetch requests.
```

---

## 10. Memory Management and Spilling

> **In plain words.** Each executor splits its memory into a part Spark manages (for caching and for sorts/joins) and a part your own code uses. When Spark-managed memory runs out during a sort or join, it writes partial results to local disk ("spill") and merges them later.
>
> **Real-world example.** An 8 GB executor: 300 MB reserved, about 4.7 GB for Spark (caching + execution), about 3.2 GB for user code. A task aggregating a 2 GB shuffle partition with 4 tasks sharing the executor gets roughly 1/4 of the ~4.7 GB pool, so it spills to disk at least once.

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
  │  │  │  (50% = storage-│  (50%)                  ││  │
  │  │   Fraction)     │                         ││  │
  │  │  │                 │                         ││  │
  │  │  │  Cached RDDs,   │  Shuffles, joins,       ││  │
  │  │  │  broadcasts,    │  sorts, aggregations    ││  │
  │  │  │  DataFrames     │                         ││  │
  │  │  │                 │                         ││  │
  │  │  │  ←─── can borrow from each other ───→     ││  │
  │  │  └─────────────────┴─────────────────────────┘│  │
  │  └────────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────────┘

  Worked numbers for an 8 GB heap: (8192 − 300) MB = 7,892 MB usable.
  Unified = 0.6 × 7,892 ≈ 4,735 MB (≈ 2,368 storage + 2,368 execution
  at the start). User memory = 0.4 × 7,892 ≈ 3,157 MB.

  Key: execution can evict cached blocks when it needs memory, but only
  until storage shrinks back to its protected share
  (spark.memory.storageFraction = 0.5). Storage can never evict
  execution. This makes OOM during shuffles less likely (not impossible).

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

  Spill is NOT a failure — it's by design. But it adds serialization
  and disk I/O and can make a task many times slower, so large spills
  are worth tuning away.

  Key tuning knobs:
  • spark.executor.memory — more memory = fewer spills
  • spark.sql.shuffle.partitions — more partitions = less data per task
  • spark.memory.fraction — 0.6 default, increase if not caching much
```

---

## 11. Spark on Kubernetes and YARN

> **In plain words.** Spark needs a cluster manager to hand it machines. YARN is the classic Hadoop option; Kubernetes is the newer option that runs Spark as containers next to your other services.
>
> **Real-world example.** A company already running its web apps on Kubernetes runs a nightly Spark job as 1 driver pod + 50 executor pods (4 cores, 8 GB each = 200 cores, 400 GB heap). The pods disappear when the job ends, so the cluster can shrink overnight.

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
  │  │  (driver)   │                                        │
  │  └──────┬─────┘                                         │
  │         │ creates executor pods via K8s API              │
  │         │                                               │
  │  ┌──────▼──────┐ ┌──────────────┐ ┌──────────────┐     │
  │  │Executor Pod │ │Executor Pod  │ │Executor Pod  │     │
  │  │   0         │ │   1          │ │   2          │     │
  │  └─────────────┘ └──────────────┘ └──────────────┘     │
  │                                                         │
  │  Shuffle data: executor local disk (emptyDir/PVC) or a  │
  │  remote shuffle service (e.g. Apache Celeborn)          │
  └─────────────────────────────────────────────────────────┘

  Advantages over YARN:
  ✓ Unified infrastructure (same cluster for all workloads)
  ✓ Better resource isolation (pod-level cgroups)
  ✓ Container images instead of classpath management
  ✓ Node autoscaling (Cluster Autoscaler/Karpenter) + Spark dynamic
    allocation with shuffle tracking
  ✓ Multi-tenant by namespace

  Challenges:
  ✗ No data locality (S3/GCS replaces HDFS — locality is irrelevant)
  ✗ Shuffle on local ephemeral storage (pod restarts lose shuffle data)
  ✗ No built-in external shuffle service on K8s: use shuffle tracking +
    executor decommissioning, or a remote shuffle service
```

---

## 12. Fault Tolerance and Lineage

> **In plain words.** Spark survives machine failures by remembering how each piece of data was computed, not by copying it. When a piece is lost, Spark recomputes only that piece, going back as far as the last saved copy (shuffle files, cache or checkpoint).
>
> **Real-world example.** A 10-stage job loses one executor at stage 8. If stage 7's shuffle files are still readable, Spark reruns only the lost stage-8 tasks (maybe 2 minutes). If they are gone, it must also rerun the stage-7 tasks that wrote them, and possibly earlier stages.

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
    sc.setCheckpointDir("hdfs://ckpt/")
    rdd4.persist(); rdd4.checkpoint()  ← materialize to HDFS (persist
                                         first, or rdd4 is computed twice)
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
  On K8s: no built-in ESS. Options: shuffle tracking + graceful
          decommissioning (migrates shuffle blocks off a dying executor),
          or a remote shuffle service (Apache Celeborn, Apache Uniffle).
```

---

## 13. Adaptive Query Execution (AQE)

> **In plain words.** AQE lets Spark change its plan halfway through a job, after it sees the real size of the data. It merges tiny partitions, switches to a cheaper join when one side turns out small, and splits oversized partitions in joins.
>
> **Real-world example.** A query planned with 200 shuffle partitions filters out 95% of the data. AQE sees 200 partitions totalling about 1 GB and merges them into ~16 partitions of ~64 MB, so 16 tasks run instead of 200 mostly-empty ones.

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
  → The map-side shuffle write already happened, but the sort and the
    all-to-all network fetch of the big side are skipped (AQE reads the
    shuffle files locally instead).

3. Skew Join Optimization:
══════════════════════════

Before AQE:
  Join on user_id. User "bot_account" has 10M rows.
  Other users: ~100 rows each.
  One reducer gets 10M rows → takes 10 minutes.
  All other reducers: finish in 5 seconds.

After AQE:
  AQE marks a partition skewed if it is > 5 × the median partition size
  (skewedPartitionFactor) AND > 256 MB (skewedPartitionThresholdInBytes).
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

> **In plain words.** Skew means a few keys hold far more data than the rest, so one task gets a giant share and the whole job waits for it. Tuning is about sizing partitions, memory and cores so every task gets a fair, fitting share of work.
>
> **Real-world example.** A ride-hailing job groups trips by driver_id. 12% of rows have driver_id = NULL (cancelled before assignment). All NULLs hash to one partition: 199 tasks finish in 1 minute, one runs for 40 minutes. Filtering NULLs first (or salting) brings the stage back to about 1 minute.

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
   spark.sql.adaptive.skewJoin.skewedPartitionFactor = 5 (default)
   spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes = 256MB
   Covers sort-merge (and, in newer versions, shuffled hash) JOINS only.
   It does NOT fix a skewed groupBy — use salting for that.

2. Salting (manual, universal technique)
   Add random prefix to the hot key to distribute it across partitions.

   -- Before: all "bot_account" rows go to one partition
   SELECT user_id, COUNT(*) FROM events GROUP BY user_id

   -- After: "bot_account" spread across 10 partitions
   -- (compute the salt ONCE in a subquery; two separate RAND() calls
   --  in SELECT and GROUP BY would not match)
   SELECT user_id, SUM(cnt) AS total
   FROM (
     SELECT user_id, salt, COUNT(*) AS cnt
     FROM (SELECT user_id, FLOOR(RAND() * 10) AS salt FROM events) e
     GROUP BY user_id, salt
   ) s
   GROUP BY user_id

   Note: Spark already does map-side partial aggregation for COUNT/SUM,
   which softens skew for these. Salting matters most for joins and for
   aggregations that can't be pre-combined (collect_list, percentiles,
   exact COUNT DISTINCT).

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
│ spark.executor.cores                    │ 1 on YARN    │ 4-5 (YARN)       │
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

  The container = heap + memoryOverhead, so divide the node memory
  first, then take the overhead out of each share:
    container_per_executor = (node_memory - OS_reserved - NodeManager)
                             / executors_per_node
    executor_memory ≈ container_per_executor / 1.1

  Example: 64 GB RAM, 16 cores per node:
    executors_per_node = floor((16 - 1) / 4) = 3   (uses 12 of 15 cores;
                         5 cores per executor would use all 15)
    container_per_executor = (64 - 4 - 2) / 3 ≈ 19.3g
    executor_memory = 19.3 / 1.1 ≈ 17g
    executor_memoryOverhead = max(384 MB, 0.1 × 17g) ≈ 2g
    Total per node: 3 × (17 + 2) = 57g ≤ 58g available
```

---

## 15. Batch vs Stream: Lambda and Kappa Architectures

> **In plain words.** Lambda runs a batch pipeline (exact but late) and a streaming pipeline (fast but approximate) side by side and merges them. Kappa keeps only the streaming pipeline and replays history through it when logic changes. Today many teams write both batch and streaming output into the same lakehouse tables.
>
> **Real-world example.** A video platform counts views. The stream gives a live counter within seconds. A nightly batch job recomputes yesterday's exact counts after removing bot views and overwrites that day's partition. Viewers see live numbers; creator payouts use the batch numbers.

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

> **In plain words.** Most batch incidents come from a few habits: jobs that create duplicates when re-run, pulling huge results onto one machine, unnecessary shuffles, and bad partition counts. Make every job safe to re-run and check its output before publishing it.
>
> **Real-world example.** A bank's nightly ledger rollup fails at 90% and is retried. With `append` mode the retry writes 1.8 days of rows for one day. With `overwrite` of the `date=2026-09-22` partition, the retry just replaces it and totals stay correct.

### Patterns

```
1. Idempotent Writes
   ──────────────────
   Every batch job should produce the same output if run twice.
   Use: OVERWRITE partition mode (replace entire partition atomically).
   Avoid: APPEND mode without deduplication (re-run = duplicates).

   df.write.mode("overwrite") \
     .option("partitionOverwriteMode", "dynamic") \
     .partitionBy("date") \
     .parquet("s3://output/")

   Careful: the default spark.sql.sources.partitionOverwriteMode is
   "static", which deletes EVERY existing partition under the path, not
   just the dates present in df. "dynamic" replaces only those dates.

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
   Once a fraction of a stage's tasks has finished
   (spark.speculation.quantile: 0.75 up to Spark 3.5, 0.9 in 4.0), any
   task running longer than multiplier × median (1.5 up to 3.5, 3 in 4.0)
   gets a copy on another executor. Whichever finishes first wins.
   Critical for batch SLAs — one slow task can delay the entire job.
   Only safe when task output is committed through Spark's output
   committer or is otherwise idempotent (no side effects like sending
   emails or calling a payment API inside a task).

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
