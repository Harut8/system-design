---
## System Design Task: Twitter-Scale Real-Time Search

### Problem Statement

Design a **scalable, low-latency system** to **index and search billions of tweets in near real time** for a global user base.
The system must support **search**, **trending topics**, and **autocomplete**, while handling massive write and read traffic with high availability.

You are expected to design this as if it were going into **production at Twitter scale**.

---

### Functional Requirements

Your design must support:

1. **Tweet Search**

   * Search by:

     * Keywords and phrases
     * Hashtags
     * User IDs
     * Filters: time range, geo/location, language
2. **Trending Topics**

   * Identify and rank trending hashtags and phrases:

     * Globally
     * Per region
   * Trends must reflect **near real-time activity**
3. **Autocomplete**

   * Provide autocomplete suggestions while users type
   * Suggestions should be based on:

     * Popular queries
     * Recent searches
     * Trending terms
4. **APIs**

   * Search API
   * Trending topics API
   * Autocomplete API
5. **Ranking**

   * Rank search results using:

     * Recency
     * Popularity (engagement)
     * Optional personalization signals

---

### Non-Functional Requirements

Your system must meet the following constraints:

1. **Scale**

   * Billions of tweets stored
   * Thousands to tens of thousands of queries per second globally
2. **Latency**

   * P99 latency ≤ **200 ms** for search and autocomplete
3. **Freshness**

   * Trending topics freshness < **1 minute**
   * Newly created tweets should appear in search within **seconds**
4. **Availability**

   * ≥ **99.99% uptime**
   * Tolerant to regional and data-center failures
5. **Consistency**

   * Eventual consistency is acceptable across shards and regions

---

### What You Should Deliver

Provide a **practical, production-oriented design** that includes:

1. **Requirement clarification & assumptions**
2. **High-level architecture**

   * Core services
   * Data flow (tweet ingestion → indexing → search)
3. **Data storage choices**

   * Indexing strategy
   * Sharding and partitioning
4. **Search architecture**

   * How queries are executed and ranked
   * How latency targets are met
5. **Trending topics computation**

   * Windowing strategy
   * Real-time vs batch trade-offs
6. **Autocomplete design**

   * Data sources
   * Update frequency
7. **Scalability strategy**

   * Horizontal scaling
   * Hot partition mitigation
8. **Failure handling**

   * Regional failover
   * Backpressure, retries
9. **Rough capacity estimates**

   * Tweets/day
   * Index size
   * QPS assumptions
10. **Trade-offs**

    * Explicitly explain what you choose *not* to optimize and why

---

### Expectations

* Be **concrete** (name technologies or categories: inverted index, stream processor, cache, etc.)
* Avoid academic theory unless it directly impacts production behavior
* Prefer **simple, reliable designs** over clever ones
* Assume this system will be maintained by hundreds of engineers over many years

---