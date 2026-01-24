## System Design Task: Instagram-Scale Distributed Like Counter

### Problem Statement

Design a **highly scalable, fault-tolerant distributed counter system** to support **Instagram-scale “Like” counts** on posts, reels, comments, and stories.

The system must handle **massive write throughput**, provide **fast reads**, and maintain **acceptable accuracy** under extreme concurrency, network partitions, and regional failures.

This counter system will be used across multiple products and surfaces, so it must be **generic, reusable, and production-ready**.

---

### Functional Requirements

Your system must support:

1. **Like / Unlike Operations**

   * Users can like and unlike:

     * Posts
     * Reels
     * Comments
   * Each user can like an item **at most once**
   * Repeated likes/unlikes should be **idempotent**

2. **Counter Retrieval**

   * Fetch like counts for:

     * Single item
     * Bulk items (e.g., feed with 50 posts)
   * Reads must be **low-latency**

3. **Real-Time Feedback**

   * UI should update immediately after a like/unlike
   * Backend counts may be **eventually consistent**

4. **APIs**

   * Like API
   * Unlike API
   * GetCount API (single & batch)
   * Optional: HasUserLiked API

5. **Scope & Extensibility**

   * System should support:

     * Other counters (views, shares, saves)
     * Future aggregation (daily likes, per-region likes)

---

### Non-Functional Requirements

Your design must satisfy:

1. **Scale**

   * Tens of billions of items
   * Hundreds of millions of active users
   * Peak write throughput: **millions of likes/unlikes per second**
   * Peak read throughput: **tens of millions of reads per second**

2. **Latency**

   * Like/Unlike P99 ≤ **50 ms**
   * Read P99 ≤ **20 ms**

3. **Availability**

   * ≥ **99.99% uptime**
   * Must survive:

     * Data center failures
     * Network partitions

4. **Consistency**

   * Eventual consistency is acceptable
   * Temporary over/under-counting is acceptable
   * No permanent count corruption

5. **Durability**

   * Likes must not be permanently lost
   * Counters must be recoverable after crashes

---

### What You Should Deliver

Provide a **production-grade system design** that includes:

1. **Requirement clarification & assumptions**

   * Accuracy guarantees
   * Acceptable error bounds
   * Read-after-write expectations

2. **High-Level Architecture**

   * Core services
   * Data flow:

     * Like → write path
     * Read → aggregation path

3. **Data Modeling**

   * How likes are stored
   * User-like relationships
   * Counter representation

4. **Counter Update Strategy**

   * Synchronous vs asynchronous updates
   * Handling extreme write contention
   * Idempotency and deduplication

5. **Storage Choices**

   * Hot counters vs cold storage
   * In-memory vs persistent stores
   * Sharding and partitioning strategy

6. **Caching Strategy**

   * What is cached
   * Cache invalidation/update model
   * Handling hot keys (viral posts)

7. **Scalability & Performance**

   * Horizontal scaling approach
   * Write amplification control
   * Hot partition mitigation techniques

8. **Failure Handling**

   * Retry strategy
   * Data recovery
   * Handling partial writes and duplicates

9. **Approximation & Optimization (If Any)**

   * Probabilistic counters (if used)
   * Batching and aggregation windows
   * Trade-offs between accuracy and performance

10. **Capacity Estimates**

    * Likes per second (average & peak)
    * Storage growth
    * Memory usage for hot counters

11. **Trade-offs & Design Decisions**

    * What accuracy guarantees are relaxed
    * Why certain technologies are chosen
    * What is explicitly *not* optimized

---

### Expectations

* Be **concrete and realistic**
* Name **specific data structures and system components**

  * (e.g., Redis, log-based ingestion, sharded counters, write-behind cache)
* Avoid academic-only solutions
* Focus on **operability, debuggability, and long-term maintenance**
* Assume this system will be used by **multiple teams and products**

---
