Design a **scalable, ultra-low-latency trading platform** for an **Interactive Brokers–scale online brokerage** that supports **multi-asset electronic trading**, **real-time market data**, **risk management**, and **order lifecycle management** across global exchanges.

The system must handle **millions of concurrent users**, **real-time price feeds**, **deterministic order execution**, and **strict regulatory compliance** while maintaining **sub-millisecond internal latency** and **five-nines availability**.

You are expected to design this as if it were going into **production at Interactive Brokers scale**.

---

### Functional Requirements

Your design must support:

1. **Order Management**

   * Order types:

     * Market, Limit, Stop, Stop-Limit, Trailing Stop
     * Bracket orders (parent + take-profit + stop-loss)
     * Algorithmic orders (TWAP, VWAP, Iceberg)
   * Order lifecycle:

     * Submit → Validate → Route → Acknowledge → Fill/Partial Fill → Complete/Cancel
   * Modifications and cancellations in-flight
   * Support for multi-leg options orders

2. **Market Data Distribution**

   * Real-time streaming quotes (Level 1 and Level 2 / depth-of-book)
   * Tick-by-tick trade data
   * Historical OHLCV bars (1s to 1M granularity)
   * Multi-exchange consolidated feed
   * Subscription-based delivery (users subscribe to specific symbols)

3. **Multi-Asset Support**

   * Equities, Options, Futures, Forex, Fixed Income, Crypto
   * Cross-asset margin and risk aggregation
   * Exchange-specific protocol adapters (FIX, ITCH, proprietary)

4. **Account & Portfolio Management**

   * Real-time P&L, margin, and buying power
   * Position tracking across asset classes
   * Multi-currency support with real-time FX conversion
   * Account hierarchies (advisor → sub-accounts)

5. **Risk & Compliance**

   * Pre-trade risk checks (margin, position limits, concentration)
   * Real-time margin computation (TIMS / portfolio margining)
   * Regulatory reporting (SEC, FINRA, MiFID II)
   * Trade surveillance and anomaly detection

6. **APIs**

   * Order submission / modification / cancellation API
   * Market data streaming API (WebSocket + REST fallback)
   * Account / portfolio / positions API
   * Historical data API

---

### Non-Functional Requirements

Your system must meet the following constraints:

1. **Scale**

   * Millions of registered accounts, hundreds of thousands concurrent during market hours
   * Tens of millions of orders per day
   * Billions of market data messages per day across all exchanges

2. **Latency**

   * Order-to-exchange (internal path): P99 ≤ **1 ms**
   * Market data tick-to-client: P99 ≤ **5 ms**
   * API response for account queries: P99 ≤ **100 ms**

3. **Throughput**

   * Market data ingestion: **10M+ messages/sec** sustained
   * Order processing: **100K+ orders/sec** peak
   * Market data fan-out: **1M+ subscriptions** served concurrently

4. **Availability**

   * ≥ **99.999% uptime** during market hours
   * Zero data loss for order and trade records
   * Graceful degradation during exchange outages

5. **Consistency**

   * **Strict ordering** for order lifecycle events per account
   * **Exactly-once** semantics for order execution
   * Eventual consistency acceptable for P&L display (< 1s lag)

6. **Durability & Auditability**

   * Full audit trail for every order event (immutable log)
   * 7-year retention for regulatory compliance
   * Point-in-time recovery for positions and balances

---

### What You Should Deliver

Provide a **practical, production-oriented design** that includes:

1. **Requirement clarification & assumptions**
2. **High-level architecture**

   * Core services (Gateway, Order Management, Matching/Routing, Market Data, Risk, Settlement)
   * Data flow (order submission → risk check → routing → exchange → fill → settlement)

3. **Order management system**

   * Order state machine
   * Smart order routing (SOR)
   * Deterministic processing guarantees
   * In-memory vs persistent state

4. **Market data architecture**

   * Feed handlers and normalization
   * Fan-out strategy to clients
   * Conflation and throttling for slow consumers
   * Historical data storage and retrieval

5. **Risk engine design**

   * Pre-trade vs real-time vs end-of-day risk
   * Margin computation approach
   * Position aggregation across asset classes

6. **Data storage choices**

   * Hot path (in-memory / LMAX-style)
   * Warm path (time-series for market data)
   * Cold path (archive for compliance)
   * Event sourcing and audit log

7. **Scalability strategy**

   * Partitioning by account / symbol / exchange
   * Horizontal scaling of stateless services
   * Market data multicast and shared-memory architectures

8. **Failure handling**

   * Exchange connectivity failover
   * Order state recovery after crash
   * Split-brain prevention
   * Circuit breakers and backpressure

9. **Rough capacity estimates**

   * Orders/day, market data messages/sec
   * Storage for tick data and audit logs
   * Network bandwidth requirements

10. **Trade-offs**

    * Latency vs consistency decisions
    * In-memory vs durable state boundaries
    * Build vs buy for matching/routing
    * What is sacrificed for regulatory compliance

---

### Expectations

* Be **concrete** (mention specific patterns: event sourcing, LMAX Disruptor, FIX protocol, kernel bypass, multicast)
* Design for **determinism** — trading systems cannot tolerate non-deterministic behavior
* Address **regulatory requirements** as first-class concerns, not afterthoughts
* Prefer **simple, battle-tested designs** over clever optimizations
* Assume this system handles **real money** — correctness trumps performance
* Assume this system will be maintained by hundreds of engineers for **20+ years**

---
