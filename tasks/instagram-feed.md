Design a **scalable, low-latency feed system** for an **Instagram-like social network** that serves **personalized feeds** to hundreds of millions of users worldwide.

The system must support **real-time post delivery**, **ranking**, and **media-heavy content**, while handling massive read and write traffic with **high availability and reliability**.

You are expected to design this as if it were going into **production at Instagram scale**.

---

### Functional Requirements

Your design must support:

1. **User Feed (Home Timeline)**

   * Show posts from:

     * Followed users
     * Sponsored / recommended content
   * Feed must be:

     * Personalized
     * Ranked
     * Paginated (infinite scroll)
2. **Post Creation**

   * Users can create posts with:

     * Images or videos
     * Captions
     * Hashtags
     * Location (optional)
   * New posts should appear in followers’ feeds quickly
3. **Feed Ranking**

   * Rank posts using:

     * Recency
     * Engagement signals (likes, comments)
     * User affinity
     * Optional ML-based ranking
4. **Interactions**

   * Support:

     * Likes
     * Comments
     * Saves
   * Engagement should affect feed ranking
5. **APIs**

   * Create post API
   * Fetch feed API
   * Like/comment APIs
6. **Media Delivery**

   * Efficient delivery of images/videos
   * Adaptive quality for different network conditions

---

### Non-Functional Requirements

Your system must meet the following constraints:

1. **Scale**

   * Hundreds of millions of users
   * Tens of billions of posts stored
   * Millions of feed reads per second globally
2. **Latency**

   * P99 latency ≤ **200 ms** for feed fetch
   * Media loading optimized via CDN
3. **Freshness**

   * New posts visible to followers within:

     * Seconds (best effort)
     * < 1 minute worst case
4. **Availability**

   * ≥ **99.99% uptime**
   * Resilient to data center and regional failures
5. **Consistency**

   * Eventual consistency acceptable for feeds
   * Strong consistency required for user actions (likes/comments)

---

### What You Should Deliver

Provide a **practical, production-oriented design** that includes:

1. **Requirement clarification & assumptions**
2. **High-level architecture**

   * Core services (Feed service, Post service, Media service)
   * Data flow (post creation → feed generation → feed read)
3. **Feed generation strategy**

   * Fan-out-on-write vs fan-out-on-read
   * Hybrid approaches
   * Handling celebrity users
4. **Data storage choices**

   * Feed storage
   * Post storage
   * Metadata vs media separation
   * Caching strategy
5. **Ranking architecture**

   * Online vs offline ranking
   * Feature computation
   * How ranking fits latency constraints
6. **Media handling**

   * Upload flow
   * Storage (object storage)
   * CDN integration
7. **Scalability strategy**

   * Horizontal scaling
   * Hot user mitigation
   * Feed cache invalidation
8. **Failure handling**

   * Backpressure on fan-out
   * Retry mechanisms
   * Graceful degradation (e.g., stale feed)
9. **Rough capacity estimates**

   * Posts/day
   * Feed reads/sec
   * Storage requirements
10. **Trade-offs**

    * What consistency is sacrificed and why
    * What is computed async vs sync
    * What is cached aggressively vs recomputed

---

### Expectations

* Be **concrete** (mention specific techniques like push vs pull feeds, Redis, object storage, CDN)
* Avoid unnecessary theory
* Clearly justify architectural decisions
* Optimize for **simplicity first**, then scale
* Assume this system will evolve for **10+ years**

---
