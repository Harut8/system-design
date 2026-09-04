# Interactive Brokers–Scale Trading Platform: Design Document

## 1. Requirements Clarification

### Assumptions & Constraints

| Dimension | Assumption |
|-----------|-----------|
| Users | 2M+ registered accounts, 500K concurrent during US market hours |
| Orders | ~30M orders/day, peaks at 100K+ orders/sec during open/close |
| Market Data | 50+ exchanges, 10M+ messages/sec ingestion, 500K+ symbols |
| Asset Classes | Equities, Options, Futures, Forex, Fixed Income, Crypto |
| Geographies | US, Europe, Asia — colocated at major exchange data centers |
| Regulatory | SEC, FINRA, MiFID II, CFTC — full audit trail, 7-year retention |
| Uptime | 99.999% during market hours (~5 min downtime/year) |

### Clarifying Questions Answered

- **Order routing**: Smart Order Routing (SOR) across multiple venues per regulation (Reg NMS / best execution)
- **Margin model**: Real-time portfolio margining (TIMS-style), not just Reg T
- **Settlement**: T+1 for equities (US), T+2 for international; overnight batch reconciliation
- **Client connectivity**: REST + WebSocket for retail; FIX 4.2/4.4 for institutional / API traders
- **Crypto**: 24/7 trading with separate risk and margin rules

---

## 2. Architecture by Scale (10K → 1M+ Accounts)

### Architecture Comparison Matrix

| Tier | Accounts | Orders/Day | Architecture Style | Key Change |
|------|----------|-----------|-------------------|------------|
| 1 | 10K | 50K | Monolith + single DB | Single process OMS |
| 2 | 100K | 500K | Modular monolith + read replicas | Separate market data service |
| 3 | 500K | 5M | Service-oriented + message bus | Dedicated risk engine, event sourcing |
| 4 | 1M+ | 30M+ | Distributed microservices + LMAX | Full SOR, multi-region, kernel bypass |

### 2.1 Tier 1: 10K Accounts (Early Brokerage)

**Architecture**: Single-process OMS with embedded risk checks.

```
┌─────────────────────────────────────────────────┐
│                   API Gateway                    │
│              (REST + WebSocket)                  │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────▼──────────────────────────────┐
│              Trading Monolith                    │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │   OMS    │ │   Risk   │ │  Market Data     │ │
│  │          │ │  Checks  │ │  (vendor feed)   │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Account  │ │ Position │ │  FIX Gateway     │ │
│  │ Service  │ │ Keeper   │ │  (1-2 exchanges) │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
└──────────────────┬──────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   PostgreSQL      │
         │   (single node)   │
         └───────────────────┘
```

**What works at this scale:**
- Single PostgreSQL database handles all orders, positions, accounts
- In-process risk checks — no network hop
- Vendor market data feed (e.g., polygon.io, IEX) instead of direct exchange connections
- FIX connection to 1-2 clearing brokers (not direct exchange access)

```python
# Simple order flow — everything in-process
class OrderService:
    def __init__(self, risk_engine, position_keeper, fix_gateway):
        self.risk = risk_engine
        self.positions = position_keeper
        self.gateway = fix_gateway

    def submit_order(self, order: Order) -> OrderAck:
        # 1. Validate
        self.validate(order)

        # 2. Pre-trade risk (in-process, ~microseconds)
        risk_result = self.risk.check(order, self.positions.get(order.account_id))
        if risk_result.rejected:
            return OrderAck(status="REJECTED", reason=risk_result.reason)

        # 3. Persist (write-ahead)
        order.status = OrderStatus.PENDING_NEW
        self.db.insert(order)

        # 4. Route to exchange via clearing broker
        self.gateway.send(order)

        return OrderAck(status="PENDING_NEW", order_id=order.id)
```

**Limitations that force Tier 2:**
- Single DB becomes bottleneck at ~5K orders/sec
- Vendor market data adds 50-200ms latency vs direct feeds
- No redundancy — single point of failure

### 2.2 Tier 2: 100K Accounts (Growing Brokerage)

**Key changes:**
- Separate market data service with direct exchange feeds
- Read replicas for account/portfolio queries
- Message queue for async order events
- Active-passive failover

```
┌─────────────┐     ┌──────────────┐     ┌────────────────┐
│  API Gateway │     │  WebSocket   │     │  FIX Gateway   │
│   (REST)     │     │  Gateway     │     │  (Institutional)│
└──────┬───────┘     └──────┬───────┘     └───────┬────────┘
       │                    │                     │
       └────────────┬───────┘─────────────────────┘
                    │
       ┌────────────▼──────────────┐
       │     Order Management      │
       │     Service (OMS)         │
       │  ┌────────┐ ┌──────────┐  │
       │  │ Router │ │  Risk    │  │
       │  │        │ │  Engine  │  │
       │  └────────┘ └──────────┘  │
       └────────────┬──────────────┘
                    │
    ┌───────────────┼───────────────┐
    │               │               │
┌───▼────┐   ┌─────▼──────┐   ┌────▼──────────┐
│ Primary│   │  Market    │   │  RabbitMQ     │
│ Pg DB  │   │  Data Svc  │   │  (order events│
│        │   │  (direct   │   │   fills, etc) │
│Replicas│   │   feeds)   │   └───────────────┘
└────────┘   └────────────┘
```

**Market data service** now connects directly to exchanges:
```python
class MarketDataService:
    def __init__(self):
        self.feed_handlers = {}  # exchange -> handler
        self.subscribers = defaultdict(set)  # symbol -> {client_ids}
        self.quote_cache = {}  # symbol -> latest quote

    def on_tick(self, exchange: str, symbol: str, tick: Tick):
        normalized = self.normalize(exchange, tick)
        self.quote_cache[symbol] = normalized

        # Fan-out to subscribers
        for client_id in self.subscribers[symbol]:
            self.push(client_id, normalized)

    def normalize(self, exchange: str, tick: Tick) -> NormalizedQuote:
        # Each exchange has different wire format
        handler = self.feed_handlers[exchange]
        return handler.parse(tick)
```

### 2.3 Tier 3: 500K Accounts (Scaling Brokerage)

**Key changes:**
- Event sourcing for order lifecycle (immutable audit log)
- Dedicated risk engine as separate service
- Kafka for event streaming
- Account-partitioned OMS instances
- Redis for real-time position cache

```
┌─────────────────────────────────────────────────────────────┐
│                      API Gateway Layer                       │
│         (REST + WebSocket + FIX Protocol Adapters)           │
└────────────────────────────┬────────────────────────────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
┌─────────▼──────┐ ┌────────▼───────┐ ┌────────▼───────┐
│   OMS Shard 1  │ │   OMS Shard 2  │ │   OMS Shard N  │
│  (accounts     │ │  (accounts     │ │  (accounts     │
│   A-F)         │ │   G-M)         │ │   N-Z)         │
└───────┬────────┘ └───────┬────────┘ └───────┬────────┘
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
┌───────▼────────┐ ┌───────▼────────┐ ┌───────▼───────┐
│  Risk Engine   │ │ Smart Order    │ │ Kafka Cluster │
│  (real-time    │ │ Router (SOR)   │ │ (order events │
│   margin)      │ │                │ │  audit log)   │
└────────────────┘ └───────┬────────┘ └───────────────┘
                           │
                  ┌────────▼────────┐
                  │ Exchange Gateway │
                  │ (FIX sessions)   │
                  └─────────────────┘
```

**Event sourcing for orders** — every state change is an immutable event:
```python
# Order events — append-only, never mutated
@dataclass
class OrderEvent:
    event_id: UUID
    order_id: UUID
    account_id: str
    event_type: str  # NEW, ACKNOWLEDGED, PARTIAL_FILL, FILLED, CANCELLED, REJECTED
    timestamp: datetime  # nanosecond precision
    payload: dict  # type-specific data
    sequence_num: int  # per-account monotonic sequence

# Event types
class OrderNew(OrderEvent):
    symbol: str
    side: str  # BUY / SELL
    quantity: Decimal
    order_type: str
    price: Optional[Decimal]
    tif: str  # DAY, GTC, IOC, FOK

class OrderFill(OrderEvent):
    fill_qty: Decimal
    fill_price: Decimal
    exchange: str
    liquidity_flag: str  # ADDED / REMOVED
    commission: Decimal

# Rebuild current state from events
def rebuild_order(events: List[OrderEvent]) -> OrderState:
    state = OrderState()
    for event in events:
        state = state.apply(event)
    return state
```

### 2.4 Tier 4: 1M+ Accounts (IBKR Scale)

This is the full production architecture described in the remaining sections. Key additions:

- **LMAX Disruptor pattern** for deterministic, single-threaded order processing
- **Kernel bypass networking** (DPDK / Solarflare OpenOnload) for exchange connectivity
- **Multi-region deployment** with exchange colocation
- **Hardware timestamping** for regulatory compliance
- **Real-time risk engine** with portfolio margining (TIMS)

---

## 3. Capacity Estimates (IBKR Scale)

### Traffic Analysis

| Metric | Value | Derivation |
|--------|-------|-----------|
| Registered accounts | 2M | Given |
| Peak concurrent users | 500K | ~25% during US market open |
| Orders/day | 30M | ~15 orders/user/day for active traders |
| Peak orders/sec | 100K | 60% of daily volume in first/last 30 min |
| Market data messages/sec (ingest) | 10M | 50+ exchanges, all asset classes |
| Market data subscriptions | 1M | avg 2 symbols/user × 500K concurrent |
| API calls/sec (account queries) | 200K | portfolio, P&L, positions |
| FIX sessions (institutional) | 10K | API/institutional traders |

### Storage Estimates

| Data Type | Daily Volume | Retention | Total Storage |
|-----------|-------------|-----------|--------------|
| Order events | 30M × 500B = 15 GB/day | 7 years | ~40 TB |
| Trade records | 20M × 1KB = 20 GB/day | 7 years | ~50 TB |
| Market data ticks | 10M/sec × 100B × 23400s = 23 TB/day | 90 days hot, 7 years cold | 2 PB hot + cold archive |
| OHLCV bars (1s) | 500K symbols × 86400s × 80B = 3.4 TB/day | Forever | 1+ PB |
| Account snapshots | 2M × 5KB = 10 GB/day | 7 years | ~25 TB |
| Audit logs | 100M events × 200B = 20 GB/day | 7 years | ~50 TB |

### Network Bandwidth

| Path | Bandwidth |
|------|-----------|
| Exchange feeds (ingest) | 10M msg/sec × 100B = ~10 Gbps |
| Client market data (egress) | 1M subs × 10 updates/sec × 200B = ~16 Gbps |
| Order flow (both directions) | 100K/sec × 500B = ~400 Mbps |
| Internal replication | ~5 Gbps |

---

## 4. High-Level Architecture (IBKR Scale)

```
                            ┌─────────────────────────────────┐
                            │         Client Layer            │
                            │  TWS  │  Web  │  Mobile │  API  │
                            └───────────────┬─────────────────┘
                                            │
                           ┌────────────────▼────────────────┐
                           │         Edge / Gateway          │
                           │  ┌─────────┐  ┌──────────────┐  │
                           │  │  REST   │  │  WebSocket   │  │
                           │  │  GW     │  │  GW          │  │
                           │  └─────────┘  └──────────────┘  │
                           │  ┌─────────┐  ┌──────────────┐  │
                           │  │  FIX    │  │  Rate Limiter│  │
                           │  │  GW     │  │  + Auth      │  │
                           │  └─────────┘  └──────────────┘  │
                           └────────────────┬────────────────┘
                                            │
               ┌────────────────────────────┼────────────────────────────┐
               │                            │                            │
  ┌────────────▼─────────────┐ ┌────────────▼─────────────┐ ┌───────────▼──────────────┐
  │   Order Management       │ │   Market Data            │ │   Account & Portfolio    │
  │   System (OMS)           │ │   Platform               │ │   Service                │
  │                          │ │                          │ │                          │
  │  ┌─────────────────────┐ │ │  ┌─────────────────────┐ │ │  ┌─────────────────────┐ │
  │  │ Order Validator     │ │ │  │ Feed Handlers       │ │ │  │ Position Keeper     │ │
  │  │ & Normalizer        │ │ │  │ (FIX,ITCH,prop.)   │ │ │  │                     │ │
  │  └─────────┬───────────┘ │ │  └─────────┬───────────┘ │ │  └─────────────────────┘ │
  │  ┌─────────▼───────────┐ │ │  ┌─────────▼───────────┐ │ │  ┌─────────────────────┐ │
  │  │ Risk Gateway        │ │ │  │ Normalizer &        │ │ │  │ P&L Engine          │ │
  │  │ (pre-trade checks)  │ │ │  │ Conflation Engine   │ │ │  │                     │ │
  │  └─────────┬───────────┘ │ │  └─────────┬───────────┘ │ │  └─────────────────────┘ │
  │  ┌─────────▼───────────┐ │ │  ┌─────────▼───────────┐ │ │  ┌─────────────────────┐ │
  │  │ LMAX Sequencer      │ │ │  │ Pub/Sub Fan-out     │ │ │  │ Margin Calculator   │ │
  │  │ (single-writer)     │ │ │  │ Engine              │ │ │  │                     │ │
  │  └─────────┬───────────┘ │ │  └─────────┬───────────┘ │ │  └─────────────────────┘ │
  │  ┌─────────▼───────────┐ │ │  ┌─────────▼───────────┐ │ │  ┌─────────────────────┐ │
  │  │ Smart Order Router  │ │ │  │ Tick Store           │ │ │  │ FX Rate Service     │ │
  │  │ (SOR)               │ │ │  │ (time-series DB)    │ │ │  │                     │ │
  │  └─────────┬───────────┘ │ │  └─────────────────────┘ │ │  └─────────────────────┘ │
  └────────────┼─────────────┘ └──────────────────────────┘ └──────────────────────────┘
               │
  ┌────────────▼─────────────┐
  │   Exchange Gateway       │
  │                          │
  │  ┌────┐ ┌────┐ ┌────┐   │
  │  │NYSE│ │NASD│ │CME │   │
  │  └────┘ └────┘ └────┘   │
  │  ┌────┐ ┌────┐ ┌────┐   │
  │  │CBOE│ │LSE │ │TSE │   │
  │  └────┘ └────┘ └────┘   │
  └──────────────────────────┘

  ┌──────────────────────────────────────────────────────────────────┐
  │                     Cross-Cutting Services                       │
  │                                                                  │
  │  ┌───────────────┐ ┌───────────────┐ ┌────────────────────────┐  │
  │  │ Risk Engine   │ │ Compliance &  │ │ Settlement & Clearing  │  │
  │  │ (real-time +  │ │ Surveillance  │ │ (T+1 batch)            │  │
  │  │  batch)       │ │               │ │                        │  │
  │  └───────────────┘ └───────────────┘ └────────────────────────┘  │
  │                                                                  │
  │  ┌───────────────┐ ┌───────────────┐ ┌────────────────────────┐  │
  │  │ Event Store   │ │ Audit Trail   │ │ Monitoring &           │  │
  │  │ (Kafka)       │ │ (immutable)   │ │ Alerting               │  │
  │  └───────────────┘ └───────────────┘ └────────────────────────┘  │
  └──────────────────────────────────────────────────────────────────┘
```

### Core Data Flow: Order Submission → Fill

```
Client                    Gateway    OMS         Risk       SOR        Exchange
  │                          │        │           │          │            │
  │─── Submit Order ────────▶│        │           │          │            │
  │                          │──▶ Validate        │          │            │
  │                          │   & Normalize      │          │            │
  │                          │        │           │          │            │
  │                          │        │──▶ Pre-trade Risk    │            │
  │                          │        │   Check   │          │            │
  │                          │        │◀── Pass ──│          │            │
  │                          │        │           │          │            │
  │                          │        │──▶ Sequence (LMAX)   │            │
  │                          │        │   assign seq_num     │            │
  │                          │        │           │          │            │
  │                          │        │──▶ Event: ORDER_NEW  │            │
  │                          │        │   (to Kafka)         │            │
  │                          │        │           │          │            │
  │                          │        │──────────────▶ Route │            │
  │                          │        │           │   (SOR)  │            │
  │                          │        │           │          │──▶ FIX New │
  │                          │        │           │          │   Order    │
  │                          │        │           │          │◀── Ack ───│
  │◀── Ack (PENDING) ───────│◀───────│           │          │            │
  │                          │        │           │          │            │
  │                          │        │           │          │◀── Fill ──│
  │                          │        │◀──────────│──── Fill │            │
  │                          │        │──▶ Update Position   │            │
  │                          │        │──▶ Event: ORDER_FILL │            │
  │◀── Fill Notification ───│◀───────│           │          │            │
```

---

## 5. Order Management System (OMS)

### 5.1 Order State Machine

Every order follows a deterministic state machine. Transitions are driven by events, and invalid transitions are rejected.

```
                              ┌─────────────┐
                              │  CREATED    │
                              └──────┬──────┘
                                     │ validate
                              ┌──────▼──────┐
                     ┌────────│  PENDING_NEW │────────┐
                     │        └──────┬──────┘        │
                     │ reject        │ ack           │ cancel
                     │               │               │
              ┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼───────┐
              │  REJECTED   │ │    NEW      │ │  PENDING    │
              └─────────────┘ └──────┬──────┘ │  _CANCEL    │
                                     │        └──────┬──────┘
                          ┌──────────┼───────┐       │
                          │          │       │       │
                   ┌──────▼───┐ ┌────▼────┐  │ ┌────▼──────┐
                   │ PARTIAL  │ │  FILLED │  │ │ CANCELLED │
                   │ _FILL    │ └─────────┘  │ └───────────┘
                   └──────┬───┘              │
                          │                  │
                   ┌──────▼──────┐    ┌──────▼──────┐
                   │   FILLED    │    │  EXPIRED    │
                   └─────────────┘    └─────────────┘
```

```python
class OrderStateMachine:
    TRANSITIONS = {
        "CREATED":        {"VALIDATE": "PENDING_NEW"},
        "PENDING_NEW":    {"ACK": "NEW", "REJECT": "REJECTED", "CANCEL": "PENDING_CANCEL"},
        "NEW":            {"PARTIAL_FILL": "PARTIAL_FILL", "FILL": "FILLED",
                           "CANCEL": "PENDING_CANCEL", "EXPIRE": "EXPIRED",
                           "REPLACE": "PENDING_REPLACE"},
        "PARTIAL_FILL":   {"PARTIAL_FILL": "PARTIAL_FILL", "FILL": "FILLED",
                           "CANCEL": "PENDING_CANCEL"},
        "PENDING_CANCEL": {"CANCELLED": "CANCELLED", "FILL": "FILLED",
                           "REJECT_CANCEL": "NEW"},
        "PENDING_REPLACE":{"REPLACED": "NEW", "REJECT_REPLACE": "NEW"},
    }
    # FILLED, REJECTED, CANCELLED, EXPIRED are terminal states

    def transition(self, current: str, event: str) -> str:
        next_state = self.TRANSITIONS.get(current, {}).get(event)
        if next_state is None:
            raise InvalidTransitionError(f"{current} + {event} is not a valid transition")
        return next_state
```

### 5.2 LMAX Disruptor Pattern

The OMS core uses a single-writer pattern inspired by the LMAX Disruptor for deterministic, lock-free processing.

```
                    ┌─────────────────────────────────────────────┐
                    │              Ring Buffer                     │
                    │  (pre-allocated, cache-line padded)          │
                    │                                             │
                    │  ┌───┬───┬───┬───┬───┬───┬───┬───┐         │
                    │  │ 0 │ 1 │ 2 │ 3 │ 4 │ 5 │ 6 │ 7 │  ...   │
                    │  └───┴───┴───┴───┴───┴───┴───┴───┘         │
                    │    ▲                       ▲                 │
                    │    │                       │                 │
                    │  Consumer                Publisher           │
                    │  Sequence               Sequence             │
                    └─────────────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
  ┌───────▼────────┐ ┌───────▼────────┐ ┌────────▼───────┐
  │ Risk Handler   │ │ Journal Handler│ │ Router Handler │
  │ (pre-trade     │ │ (persist to    │ │ (send to       │
  │  checks)       │ │  event store)  │ │  exchange)     │
  └────────────────┘ └────────────────┘ └────────────────┘
```

**Why single-writer?**
- No locks → no lock contention → deterministic latency
- Mechanical sympathy: sequential memory access, CPU cache-friendly
- Single thread can process 1M+ events/sec on modern hardware
- Replay capability: feed the same events → get the same state

```java
// Simplified LMAX-style sequencer (Java — the canonical implementation)
public class OrderSequencer {
    private final RingBuffer<OrderEvent> ringBuffer;
    private final EventHandler<OrderEvent>[] handlers;

    // Single publisher thread — all orders for a partition
    // flow through this one thread
    public void onOrder(IncomingOrder order) {
        long sequence = ringBuffer.next();
        try {
            OrderEvent event = ringBuffer.get(sequence);
            event.copyFrom(order);
            event.setSequenceNum(sequence);
            event.setTimestamp(System.nanoTime());
        } finally {
            ringBuffer.publish(sequence);
        }
    }

    // Handlers consume in dependency order:
    // Risk → Journal → Router
    // Each runs on its own thread but processes
    // events in strict sequence order
}
```

**Partitioning**: One sequencer per account partition. Orders for the same account always go to the same partition → strict per-account ordering without cross-partition coordination.

### 5.3 Smart Order Router (SOR)

SOR decides which exchange(s) to route an order to, considering:
- **Best execution** (Reg NMS / MiFID II best execution obligation)
- **Venue liquidity** (visible + hidden)
- **Fees** (maker/taker, routing fees)
- **Latency** (to each venue)

```python
class SmartOrderRouter:
    def __init__(self, venues: List[Venue], market_data: MarketDataService):
        self.venues = venues
        self.md = market_data

    def route(self, order: Order) -> List[ChildOrder]:
        if order.type == OrderType.MARKET:
            return self.route_market(order)
        elif order.type == OrderType.LIMIT:
            return self.route_limit(order)
        elif order.type in (OrderType.TWAP, OrderType.VWAP):
            return self.route_algo(order)

    def route_market(self, order: Order) -> List[ChildOrder]:
        # Get consolidated book across all venues
        book = self.md.get_consolidated_book(order.symbol)

        # Sweep the book — fill at best prices across venues
        children = []
        remaining = order.quantity

        for level in book.levels(order.side):
            if remaining <= 0:
                break

            fill_qty = min(remaining, level.size)
            children.append(ChildOrder(
                parent_id=order.id,
                venue=level.venue,
                symbol=order.symbol,
                side=order.side,
                quantity=fill_qty,
                price=level.price,
                type=OrderType.LIMIT,  # IOC limit at that price
                tif=TimeInForce.IOC
            ))
            remaining -= fill_qty

        return children

    def route_limit(self, order: Order) -> List[ChildOrder]:
        # Check NBBO — is our limit price marketable?
        nbbo = self.md.get_nbbo(order.symbol)

        if order.is_marketable(nbbo):
            return self.route_market(order)  # sweep

        # Non-marketable: post to venue with best rebate
        best_venue = self.select_posting_venue(order)
        return [ChildOrder(
            parent_id=order.id,
            venue=best_venue,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
            type=OrderType.LIMIT,
            tif=order.tif
        )]

    def select_posting_venue(self, order: Order) -> Venue:
        # Rank venues by: maker rebate, fill rate, latency
        scored = []
        for venue in self.venues:
            if not venue.supports(order.symbol):
                continue
            score = (
                venue.maker_rebate * 0.4 +
                venue.historical_fill_rate(order.symbol) * 0.4 -
                venue.latency_ms * 0.2
            )
            scored.append((score, venue))
        scored.sort(reverse=True)
        return scored[0][1]
```

### 5.4 Algorithmic Orders

```python
class TWAPAlgo:
    """Time-Weighted Average Price — slice large order into
    equal-sized child orders spread over a time window."""

    def __init__(self, parent: Order, duration_sec: int, num_slices: int):
        self.parent = parent
        self.slice_qty = parent.quantity // num_slices
        self.interval = duration_sec / num_slices
        self.remaining = parent.quantity
        self.children_sent = 0

    def on_timer(self):
        if self.remaining <= 0:
            return

        qty = min(self.slice_qty, self.remaining)
        child = ChildOrder(
            parent_id=self.parent.id,
            symbol=self.parent.symbol,
            side=self.parent.side,
            quantity=qty,
            type=OrderType.LIMIT,
            price=self.get_limit_price(),
            tif=TimeInForce.IOC
        )
        self.router.route(child)
        self.remaining -= qty
        self.children_sent += 1

    def get_limit_price(self):
        nbbo = self.md.get_nbbo(self.parent.symbol)
        # Add small aggression to improve fill rate
        if self.parent.side == "BUY":
            return nbbo.ask + Decimal("0.01")
        return nbbo.bid - Decimal("0.01")


class IcebergOrder:
    """Show only a portion of the total order to the market."""

    def __init__(self, parent: Order, display_qty: int):
        self.parent = parent
        self.display_qty = display_qty
        self.remaining = parent.quantity

    def on_fill(self, fill: Fill):
        self.remaining -= fill.quantity
        if self.remaining > 0:
            # Replenish the displayed slice
            self.send_slice()

    def send_slice(self):
        qty = min(self.display_qty, self.remaining)
        child = ChildOrder(
            parent_id=self.parent.id,
            symbol=self.parent.symbol,
            side=self.parent.side,
            quantity=qty,
            price=self.parent.price,
            type=OrderType.LIMIT,
            tif=TimeInForce.DAY
        )
        self.router.route(child)
```

---

## 6. Market Data Architecture

### 6.1 Feed Handler Layer

Each exchange speaks a different protocol. Feed handlers translate to a normalized internal format.

```
  NYSE (Pillar)     NASDAQ (ITCH 5.0)    CME (MDP 3.0)     LSE (MIT)
       │                   │                   │                │
  ┌────▼────┐         ┌────▼────┐         ┌────▼────┐     ┌────▼────┐
  │  Feed   │         │  Feed   │         │  Feed   │     │  Feed   │
  │ Handler │         │ Handler │         │ Handler │     │ Handler │
  │ (Pillar │         │ (ITCH   │         │ (MDP    │     │ (MIT    │
  │  codec) │         │  codec) │         │  codec) │     │  codec) │
  └────┬────┘         └────┬────┘         └────┬────┘     └────┬────┘
       │                   │                   │                │
       └───────────────────┼───────────────────┘────────────────┘
                           │
                  ┌────────▼────────┐
                  │   Normalizer    │
                  │  (unified tick  │
                  │   format)       │
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────▼──────┐ ┌───▼──────┐ ┌───▼──────────┐
     │ Conflation    │ │ Tick     │ │ NBBO         │
     │ Engine        │ │ Store    │ │ Calculator   │
     │ (per-client   │ │ (append  │ │ (consolidated│
     │  throttle)    │ │  to TS)  │ │  best bid/   │
     └───────┬───────┘ └──────────┘ │  ask)        │
             │                      └──────────────┘
     ┌───────▼───────┐
     │  WebSocket    │
     │  Fan-out      │
     │  (per-symbol  │
     │   topics)     │
     └───────────────┘
```

```python
@dataclass
class NormalizedTick:
    symbol: str           # Unified symbol (e.g., "AAPL")
    exchange: str         # Source exchange
    timestamp: int        # Exchange timestamp (nanoseconds since epoch)
    recv_timestamp: int   # Our receive timestamp (nanoseconds)
    tick_type: str        # TRADE, QUOTE, DEPTH
    bid: Decimal
    bid_size: int
    ask: Decimal
    ask_size: int
    last: Decimal
    last_size: int
    sequence: int         # Per-symbol monotonic sequence


class FeedHandler:
    """Base class for exchange-specific feed handlers."""

    def __init__(self, exchange: str, multicast_groups: List[str]):
        self.exchange = exchange
        self.multicast_groups = multicast_groups
        self.sequence_tracker = {}  # symbol -> last_seq (gap detection)

    def on_packet(self, raw: bytes):
        messages = self.decode(raw)  # Exchange-specific decoding
        for msg in messages:
            # Gap detection
            expected_seq = self.sequence_tracker.get(msg.symbol, 0) + 1
            if msg.sequence != expected_seq and expected_seq > 1:
                self.request_retransmit(msg.symbol, expected_seq, msg.sequence)
                continue
            self.sequence_tracker[msg.symbol] = msg.sequence

            normalized = self.normalize(msg)
            self.publish(normalized)

    def decode(self, raw: bytes) -> List:
        raise NotImplementedError

    def normalize(self, msg) -> NormalizedTick:
        raise NotImplementedError
```

### 6.2 Conflation Engine

Not all clients can consume 10M messages/sec. The conflation engine throttles per-client based on their subscription tier and connection speed.

```python
class ConflationEngine:
    """Reduces tick rate for slow consumers by keeping only the latest
    state per symbol and flushing at the client's max rate."""

    def __init__(self):
        self.buckets = {}  # client_id -> {symbol -> latest_tick}
        self.client_rates = {}  # client_id -> max_ticks_per_sec

    def on_tick(self, tick: NormalizedTick):
        # Update latest state for all subscribers of this symbol
        for client_id in self.subscriptions[tick.symbol]:
            if client_id not in self.buckets:
                self.buckets[client_id] = {}
            # Overwrite — only latest matters
            self.buckets[client_id][tick.symbol] = tick

    def flush(self, client_id: str):
        """Called on timer at client's configured rate."""
        bucket = self.buckets.pop(client_id, {})
        for symbol, tick in bucket.items():
            self.send_to_client(client_id, tick)


class SubscriptionTier(Enum):
    SNAPSHOT = 1        # Poll-based, 1 update/sec
    STREAMING_BASIC = 2  # 5 updates/sec per symbol (retail)
    STREAMING_PRO = 3    # 20 updates/sec per symbol (active trader)
    FULL_TICK = 4        # Every tick, no conflation (institutional)
```

### 6.3 Fan-out Architecture

```
                    ┌──────────────────────────────┐
                    │    Market Data Bus            │
                    │    (shared memory /           │
                    │     kernel bypass)            │
                    └──────────────┬───────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼─────────┐ ┌───────▼────────┐ ┌────────▼─────────┐
     │ Fan-out Process 1│ │ Fan-out Proc 2 │ │ Fan-out Proc N   │
     │ (symbols A-F)    │ │ (symbols G-M)  │ │ (symbols N-Z)    │
     │                  │ │                │ │                  │
     │ Subscription     │ │ Subscription   │ │ Subscription     │
     │ Registry         │ │ Registry       │ │ Registry         │
     └────────┬─────────┘ └───────┬────────┘ └────────┬─────────┘
              │                   │                    │
              └───────────────────┼────────────────────┘
                                  │
                     ┌────────────▼────────────┐
                     │   WebSocket Cluster     │
                     │   (sticky sessions by   │
                     │    client_id)            │
                     └─────────────────────────┘
```

**Shared memory for internal fan-out**: Between the feed handler and fan-out processes, we use shared memory (or DPDK-style ring buffers) instead of network hops. This keeps inter-process latency under 1 microsecond.

### 6.4 Tick Store (Time-Series Database)

```python
# Schema for tick storage (QuestDB / TimescaleDB / InfluxDB)
# QuestDB chosen for: column-oriented, append-only, SQL interface,
# ingestion rate >1M rows/sec on commodity hardware

CREATE TABLE ticks (
    symbol       SYMBOL,
    exchange     SYMBOL,
    timestamp    TIMESTAMP,       -- exchange timestamp (nanosecond)
    recv_ts      TIMESTAMP,       -- our receive timestamp
    tick_type    SYMBOL,          -- TRADE, QUOTE
    bid          DOUBLE,
    bid_size     INT,
    ask          DOUBLE,
    ask_size     INT,
    last         DOUBLE,
    last_size    INT,
    volume       LONG,
    sequence     LONG
) TIMESTAMP(timestamp) PARTITION BY DAY
  DEDUP KEYS(symbol, exchange, sequence);

-- OHLCV bars materialized via continuous aggregation
CREATE TABLE ohlcv_1s AS (
    SELECT
        symbol,
        timestamp AS ts,
        first(last) AS open,
        max(last) AS high,
        min(last) AS low,
        last(last) AS close,
        sum(last_size) AS volume
    FROM ticks
    WHERE tick_type = 'TRADE'
    SAMPLE BY 1s
    ALIGN TO CALENDAR
);
```

**Storage tiering:**
- **Hot (0-7 days)**: NVMe SSDs, full tick data, sub-ms query
- **Warm (7-90 days)**: SSD, compressed, 1s bar minimum
- **Cold (90 days - 7 years)**: Object storage (S3), Parquet format, for compliance

### 6.5 Historical Data API

```python
class HistoricalDataService:
    """Serves OHLCV bars and raw ticks for charting and backtesting."""

    def get_bars(self, symbol: str, interval: str,
                 start: datetime, end: datetime) -> List[OHLCVBar]:
        # Route to appropriate storage tier
        age = datetime.utcnow() - start

        if age < timedelta(days=7):
            return self.hot_store.query_bars(symbol, interval, start, end)
        elif age < timedelta(days=90):
            return self.warm_store.query_bars(symbol, interval, start, end)
        else:
            return self.cold_store.query_bars(symbol, interval, start, end)

    def get_ticks(self, symbol: str, start: datetime,
                  end: datetime) -> Iterator[NormalizedTick]:
        # Only available from hot store (raw ticks not kept long-term)
        if (datetime.utcnow() - start) > timedelta(days=7):
            raise TickDataExpiredError("Raw ticks only available for last 7 days")
        return self.hot_store.stream_ticks(symbol, start, end)
```

---

## 7. Risk Engine

### 7.1 Three-Stage Risk Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Risk Engine Architecture                   │
│                                                              │
│  Stage 1: PRE-TRADE (synchronous, on order path)             │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ • Buying power check                                    │ │
│  │ • Position limit check                                  │ │
│  │ • Concentration check (single-name %)                   │ │
│  │ • Fat finger check (price deviation, size)              │ │
│  │ • Restricted list check                                 │ │
│  │ Latency budget: < 50 μs                                 │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  Stage 2: REAL-TIME (async, continuously updated)            │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ • Portfolio margin (TIMS model)                         │ │
│  │ • Greeks aggregation (delta, gamma, vega, theta)        │ │
│  │ • Stress scenarios (what-if analysis)                   │ │
│  │ • Margin call detection                                 │ │
│  │ Update frequency: on every fill + market data tick       │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  Stage 3: END-OF-DAY (batch, overnight)                      │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ • Full portfolio re-margining                           │ │
│  │ • Reg T margin check                                   │ │
│  │ • Concentration risk report                             │ │
│  │ • VaR / Expected Shortfall calculation                  │ │
│  │ • Regulatory risk reports (FOCUS, SSOI)                 │ │
│  └─────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 Pre-Trade Risk Checks

```python
class PreTradeRiskEngine:
    """Synchronous checks on the critical order path.
    Must complete in < 50 μs — all data pre-loaded in memory."""

    def __init__(self):
        self.position_cache = {}    # account -> {symbol -> position}
        self.margin_cache = {}      # account -> margin_state
        self.restricted_list = set()  # symbols blocked from trading

    def check(self, order: Order) -> RiskResult:
        checks = [
            self.check_restricted,
            self.check_fat_finger,
            self.check_buying_power,
            self.check_position_limits,
            self.check_concentration,
            self.check_order_rate,
        ]

        for check_fn in checks:
            result = check_fn(order)
            if result.rejected:
                return result

        return RiskResult(passed=True)

    def check_fat_finger(self, order: Order) -> RiskResult:
        # Price reasonability
        nbbo = self.market_data.get_nbbo(order.symbol)
        if order.price:
            mid = (nbbo.bid + nbbo.ask) / 2
            deviation = abs(order.price - mid) / mid
            if deviation > Decimal("0.10"):  # 10% away from mid
                return RiskResult(rejected=True,
                    reason=f"Price {order.price} deviates {deviation:.1%} from mid {mid}")

        # Size reasonability
        adv = self.get_avg_daily_volume(order.symbol)
        if order.quantity > adv * Decimal("0.05"):  # > 5% of ADV
            return RiskResult(rejected=True,
                reason=f"Size {order.quantity} exceeds 5% of ADV ({adv})")

        return RiskResult(passed=True)

    def check_buying_power(self, order: Order) -> RiskResult:
        margin = self.margin_cache[order.account_id]
        required = self.calculate_margin_impact(order)

        if required > margin.available_buying_power:
            return RiskResult(rejected=True,
                reason=f"Insufficient buying power: need {required}, have {margin.available_buying_power}")

        return RiskResult(passed=True)
```

### 7.3 Real-Time Margin (TIMS Model)

IBKR uses a portfolio margining approach based on OCC's TIMS (Theoretical Intermarket Margining System).

```python
class TIMSMarginEngine:
    """Portfolio margin calculation using risk arrays.
    Evaluates portfolio value under multiple stress scenarios."""

    # TIMS defines scenarios as combinations of:
    # - Underlying price move: -15% to +15% in steps
    # - Volatility move: -15% to +15%
    # - Time decay: 1 day
    SCENARIOS = [
        {"price_move": p, "vol_move": v}
        for p in [-0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15]
        for v in [-0.15, 0.0, 0.15]
    ]  # 21 scenarios per underlying

    def calculate_margin(self, account_id: str) -> MarginResult:
        positions = self.get_positions(account_id)

        # Group positions by underlying
        groups = self.group_by_underlying(positions)

        total_margin = Decimal("0")
        for underlying, group_positions in groups.items():
            worst_loss = Decimal("0")

            for scenario in self.SCENARIOS:
                pnl = self.evaluate_scenario(group_positions, scenario)
                worst_loss = min(worst_loss, pnl)

            # Margin = worst-case loss across all scenarios
            total_margin += abs(worst_loss)

        # Cross-margining: offsetting positions across related products
        offset = self.calculate_cross_margin_offset(groups)
        total_margin -= offset

        return MarginResult(
            margin_requirement=total_margin,
            available_equity=self.get_equity(account_id),
            excess_margin=self.get_equity(account_id) - total_margin,
            margin_utilization=total_margin / self.get_equity(account_id)
        )

    def evaluate_scenario(self, positions: List[Position],
                          scenario: dict) -> Decimal:
        total_pnl = Decimal("0")
        for pos in positions:
            if pos.asset_type == "OPTION":
                pnl = self.reprice_option(pos, scenario)
            elif pos.asset_type == "FUTURE":
                pnl = pos.quantity * pos.multiplier * (
                    pos.current_price * Decimal(str(scenario["price_move"]))
                )
            else:  # equity
                pnl = pos.quantity * pos.current_price * Decimal(str(scenario["price_move"]))
            total_pnl += pnl
        return total_pnl

    def reprice_option(self, pos: Position, scenario: dict) -> Decimal:
        # Black-Scholes repricing under scenario
        new_underlying = pos.underlying_price * (1 + Decimal(str(scenario["price_move"])))
        new_vol = pos.implied_vol * (1 + Decimal(str(scenario["vol_move"])))
        new_price = black_scholes(
            S=new_underlying,
            K=pos.strike,
            T=pos.time_to_expiry - Decimal("1")/365,  # 1-day decay
            r=self.risk_free_rate,
            sigma=new_vol,
            option_type=pos.option_type
        )
        return pos.quantity * pos.multiplier * (new_price - pos.current_price)
```

### 7.4 Position Tracking

```python
class PositionKeeper:
    """Real-time position tracking across all asset classes.
    Positions are derived from fills, never directly mutated."""

    def __init__(self):
        # In-memory position state, rebuilt from event log on startup
        self.positions = {}  # (account_id, symbol) -> Position

    def on_fill(self, fill: Fill):
        key = (fill.account_id, fill.symbol)
        pos = self.positions.get(key, Position.empty(fill.account_id, fill.symbol))

        if fill.side == "BUY":
            new_qty = pos.quantity + fill.quantity
            if pos.quantity >= 0:
                # Adding to long — update avg cost
                new_cost = (pos.avg_cost * pos.quantity + fill.price * fill.quantity) / new_qty
            else:
                # Covering short
                new_cost = pos.avg_cost if new_qty < 0 else fill.price
        else:  # SELL
            new_qty = pos.quantity - fill.quantity
            if pos.quantity <= 0:
                new_cost = (abs(pos.avg_cost * pos.quantity) + fill.price * fill.quantity) / abs(new_qty)
            else:
                new_cost = pos.avg_cost if new_qty > 0 else fill.price

        pos.quantity = new_qty
        pos.avg_cost = new_cost
        pos.realized_pnl += self.calc_realized_pnl(pos, fill)
        pos.last_updated = fill.timestamp

        self.positions[key] = pos

        # Publish position update event
        self.publish(PositionUpdate(
            account_id=fill.account_id,
            symbol=fill.symbol,
            position=pos
        ))

    def get_portfolio(self, account_id: str) -> Portfolio:
        positions = {k[1]: v for k, v in self.positions.items()
                     if k[0] == account_id and v.quantity != 0}

        unrealized_pnl = sum(
            p.quantity * (self.market_data.get_last(p.symbol) - p.avg_cost)
            for p in positions.values()
        )

        return Portfolio(
            positions=positions,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=sum(p.realized_pnl for p in positions.values()),
            net_liquidation=self.get_cash(account_id) + sum(
                p.quantity * self.market_data.get_last(p.symbol)
                for p in positions.values()
            )
        )
```

---

## 8. Data Storage Design

### 8.1 Storage Strategy by Data Type

| Data | Store | Why |
|------|-------|-----|
| Order events (audit log) | Kafka + S3 (Parquet) | Append-only, immutable, high throughput, long retention |
| Current order state | In-memory (OMS) + PostgreSQL | Hot state in RAM, durable in DB |
| Positions (real-time) | In-memory (PositionKeeper) + Redis | Sub-μs reads, Redis for cross-service sharing |
| Positions (durable) | PostgreSQL | Nightly snapshot, point-in-time recovery |
| Market data (real-time) | Shared memory ring buffer | Zero-copy, sub-μs |
| Market data (ticks) | QuestDB | Append-only time-series, 1M+ inserts/sec |
| Market data (bars) | QuestDB + S3 (Parquet) | Continuous aggregation, cold archive |
| Account balances | PostgreSQL | ACID for money, strong consistency |
| Reference data (symbols) | PostgreSQL + Redis | Rarely changes, cached aggressively |
| Compliance/audit | Kafka → S3 (Parquet) → Athena | Immutable, queryable archive |

### 8.2 Event Store (Kafka)

```
┌────────────────────────────────────────────────────────┐
│                    Kafka Cluster                        │
│                                                        │
│  Topic: orders.events                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│  │Partition 0│ │Partition 1│ │Partition N│              │
│  │(accts A-C)│ │(accts D-F)│ │(accts ..)│              │
│  └──────────┘ └──────────┘ └──────────┘               │
│                                                        │
│  Topic: market-data.ticks                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│  │Partition 0│ │Partition 1│ │Partition N│              │
│  │(symbols  )│ │(symbols  )│ │(symbols  )│              │
│  │ A-F      │ │ G-M      │ │ N-Z       │              │
│  └──────────┘ └──────────┘ └──────────┘               │
│                                                        │
│  Topic: positions.updates                              │
│  Topic: risk.margin-calls                              │
│  Topic: compliance.audit                               │
│                                                        │
│  Retention: 7 days in Kafka, then S3 tiered storage    │
│  Replication: 3x (min.insync.replicas=2)               │
└────────────────────────────────────────────────────────┘
```

**Key Kafka configuration for trading:**
```properties
# Zero tolerance for data loss on order events
acks=all
min.insync.replicas=2
unclean.leader.election.enable=false

# Ordering guarantee per account
# Partition key = account_id (for orders) or symbol (for market data)

# Throughput tuning for market data topic
batch.size=65536
linger.ms=1
compression.type=lz4  # low CPU overhead
```

### 8.3 PostgreSQL Schema (Account & Order State)

```sql
-- Accounts
CREATE TABLE accounts (
    account_id       VARCHAR(20) PRIMARY KEY,
    account_type     VARCHAR(20) NOT NULL,  -- INDIVIDUAL, ADVISOR, IRA, MARGIN
    parent_account   VARCHAR(20) REFERENCES accounts(account_id),
    base_currency    VARCHAR(3) NOT NULL DEFAULT 'USD',
    margin_type      VARCHAR(20) NOT NULL,  -- REG_T, PORTFOLIO
    status           VARCHAR(20) NOT NULL,  -- ACTIVE, RESTRICTED, CLOSED
    created_at       TIMESTAMPTZ NOT NULL
);

-- Cash balances (multi-currency)
CREATE TABLE cash_balances (
    account_id   VARCHAR(20) REFERENCES accounts(account_id),
    currency     VARCHAR(3),
    balance      NUMERIC(18, 4) NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (account_id, currency)
);

-- Orders (current state — event-sourced, this is a projection)
CREATE TABLE orders (
    order_id         UUID PRIMARY KEY,
    account_id       VARCHAR(20) NOT NULL,
    symbol           VARCHAR(30) NOT NULL,
    side             VARCHAR(4) NOT NULL,     -- BUY, SELL
    order_type       VARCHAR(20) NOT NULL,    -- MARKET, LIMIT, STOP, etc.
    quantity         NUMERIC(18, 4) NOT NULL,
    price            NUMERIC(18, 8),
    stop_price       NUMERIC(18, 8),
    tif              VARCHAR(10) NOT NULL,    -- DAY, GTC, IOC, FOK
    status           VARCHAR(20) NOT NULL,
    filled_qty       NUMERIC(18, 4) NOT NULL DEFAULT 0,
    avg_fill_price   NUMERIC(18, 8),
    parent_order_id  UUID,                    -- for bracket/algo children
    exchange         VARCHAR(20),
    submitted_at     TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    sequence_num     BIGINT NOT NULL          -- LMAX sequence for replay
);

CREATE INDEX idx_orders_account_status ON orders(account_id, status);
CREATE INDEX idx_orders_symbol ON orders(symbol) WHERE status IN ('NEW', 'PARTIAL_FILL');

-- Trades (fills)
CREATE TABLE trades (
    trade_id         UUID PRIMARY KEY,
    order_id         UUID NOT NULL REFERENCES orders(order_id),
    account_id       VARCHAR(20) NOT NULL,
    symbol           VARCHAR(30) NOT NULL,
    side             VARCHAR(4) NOT NULL,
    quantity         NUMERIC(18, 4) NOT NULL,
    price            NUMERIC(18, 8) NOT NULL,
    commission       NUMERIC(12, 4) NOT NULL,
    exchange         VARCHAR(20) NOT NULL,
    liquidity_flag   VARCHAR(10),             -- ADDED, REMOVED
    executed_at      TIMESTAMPTZ NOT NULL,
    settled_at       TIMESTAMPTZ,
    settlement_date  DATE NOT NULL
);

CREATE INDEX idx_trades_account ON trades(account_id, executed_at DESC);
CREATE INDEX idx_trades_settlement ON trades(settlement_date) WHERE settled_at IS NULL;

-- Positions (nightly snapshot + real-time cache in Redis)
CREATE TABLE positions (
    account_id       VARCHAR(20),
    symbol           VARCHAR(30),
    quantity         NUMERIC(18, 4) NOT NULL,
    avg_cost         NUMERIC(18, 8) NOT NULL,
    realized_pnl     NUMERIC(18, 4) NOT NULL DEFAULT 0,
    snapshot_date    DATE NOT NULL,
    PRIMARY KEY (account_id, symbol, snapshot_date)
);
```

### 8.4 In-Memory Architecture

The hot path (order processing, risk checks, position updates) runs entirely in memory. Persistence is async.

```
┌─────────────────────────────────────────────────────┐
│                 In-Memory Data                       │
│                                                     │
│  ┌─────────────────┐  ┌──────────────────────────┐  │
│  │ Order Book       │  │ Position Cache            │  │
│  │ (per-account     │  │ (per-account              │  │
│  │  HashMap)        │  │  HashMap of positions)    │  │
│  │                  │  │                          │  │
│  │ ~500K active     │  │ ~2M accounts ×            │  │
│  │ orders = ~2 GB   │  │ avg 10 positions = ~1 GB  │  │
│  └─────────────────┘  └──────────────────────────┘  │
│                                                     │
│  ┌─────────────────┐  ┌──────────────────────────┐  │
│  │ Quote Cache      │  │ Margin State              │  │
│  │ (NBBO per        │  │ (per-account buying       │  │
│  │  symbol)         │  │  power, margin req)       │  │
│  │                  │  │                          │  │
│  │ 500K symbols ×   │  │ ~500K active accounts    │  │
│  │ 200B = ~100 MB   │  │ × 500B = ~250 MB         │  │
│  └─────────────────┘  └──────────────────────────┘  │
│                                                     │
│  Total: ~4 GB — fits in a single server's L3 cache  │
│  + RAM with room to spare                           │
└─────────────────────────────────────────────────────┘
         │
         │ Async persist (every event)
         ▼
┌─────────────────┐  ┌────────────┐  ┌──────────────┐
│ Kafka (durable  │  │ PostgreSQL │  │ Redis        │
│  event log)     │  │ (snapshots)│  │ (shared      │
│                 │  │            │  │  state)      │
└─────────────────┘  └────────────┘  └──────────────┘
```

---

## 9. Exchange Connectivity

### 9.1 FIX Protocol Gateway

```python
class FIXGateway:
    """Manages FIX sessions to exchanges and clearing brokers.
    Each venue has its own session with venue-specific quirks."""

    def __init__(self):
        self.sessions = {}  # venue -> FIXSession

    def send_new_order(self, order: ChildOrder):
        session = self.sessions[order.venue]

        fix_msg = FixMessage()
        fix_msg.set_field(MsgType, "D")  # New Order Single
        fix_msg.set_field(ClOrdID, str(order.id))
        fix_msg.set_field(Symbol, order.symbol)
        fix_msg.set_field(Side, "1" if order.side == "BUY" else "2")
        fix_msg.set_field(OrderQty, order.quantity)
        fix_msg.set_field(OrdType, self.map_order_type(order.type))
        fix_msg.set_field(Price, order.price)
        fix_msg.set_field(TimeInForce, self.map_tif(order.tif))
        fix_msg.set_field(TransactTime, datetime.utcnow())

        session.send(fix_msg)

    def on_execution_report(self, session: FIXSession, msg: FixMessage):
        exec_type = msg.get_field(ExecType)

        if exec_type == "0":    # New (acknowledged)
            self.publish(OrderAck(order_id=msg.get_field(ClOrdID)))
        elif exec_type == "1":  # Partial fill
            self.publish(Fill(
                order_id=msg.get_field(ClOrdID),
                quantity=msg.get_field(LastQty),
                price=msg.get_field(LastPx),
                exchange=session.venue
            ))
        elif exec_type == "2":  # Full fill
            self.publish(Fill(
                order_id=msg.get_field(ClOrdID),
                quantity=msg.get_field(LastQty),
                price=msg.get_field(LastPx),
                exchange=session.venue,
                is_final=True
            ))
        elif exec_type == "8":  # Rejected
            self.publish(OrderReject(
                order_id=msg.get_field(ClOrdID),
                reason=msg.get_field(Text)
            ))
```

### 9.2 Kernel Bypass Networking

For exchange colocation, we use kernel bypass to eliminate OS overhead on the network path.

```
┌─────────────────────────────────────────────────────────┐
│              Kernel Bypass Stack                         │
│                                                         │
│  Traditional:                                           │
│  NIC → Kernel → Socket → User Space                    │
│  Latency: ~10-50 μs per hop                            │
│                                                         │
│  Kernel Bypass (DPDK / Solarflare OpenOnload):          │
│  NIC → User Space (direct memory-mapped I/O)            │
│  Latency: ~1-3 μs per hop                              │
│                                                         │
│  ┌──────────┐                                           │
│  │   NIC    │◄── Hardware timestamping                  │
│  │(Solarflare│    (nanosecond precision for MiFID II)   │
│  │ X2522)   │                                           │
│  └────┬─────┘                                           │
│       │ DMA (no kernel copy)                            │
│  ┌────▼──────────────────────────────────────────────┐  │
│  │  User-space network stack                          │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌─────────────┐  │  │
│  │  │ TCP/UDP  │  │ FIX Engine   │  │ ITCH/MDP    │  │  │
│  │  │ Stack    │  │ (order flow) │  │ Decoder     │  │  │
│  │  └──────────┘  └──────────────┘  └─────────────┘  │  │
│  └────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 9.3 Exchange Adapter Pattern

```python
class ExchangeAdapter:
    """Each exchange has unique quirks. The adapter normalizes them."""

    def __init__(self, venue_config: VenueConfig):
        self.config = venue_config
        self.supported_order_types = venue_config.order_types
        self.tick_sizes = {}  # symbol -> Decimal
        self.lot_sizes = {}   # symbol -> int
        self.trading_hours = venue_config.hours

    def validate_order(self, order: ChildOrder) -> ValidationResult:
        # Check tick size compliance
        tick = self.tick_sizes.get(order.symbol)
        if tick and order.price % tick != 0:
            return ValidationResult(
                valid=False,
                reason=f"Price must be multiple of tick size {tick}"
            )

        # Check lot size
        lot = self.lot_sizes.get(order.symbol, 1)
        if order.quantity % lot != 0:
            return ValidationResult(
                valid=False,
                reason=f"Quantity must be multiple of lot size {lot}"
            )

        # Check trading hours
        if not self.trading_hours.is_open():
            if not self.config.accepts_after_hours:
                return ValidationResult(valid=False, reason="Market closed")

        return ValidationResult(valid=True)
```

---

## 10. Account & Portfolio Service

### 10.1 Multi-Currency Support

```python
class MultiCurrencyEngine:
    """All P&L and margin calculations happen in the account's base currency.
    FX rates are streamed in real-time from the market data service."""

    def __init__(self, fx_service: FXRateService):
        self.fx = fx_service

    def get_net_liquidation(self, account: Account) -> Decimal:
        nlv = Decimal("0")

        # Cash balances in all currencies
        for currency, balance in account.cash_balances.items():
            nlv += self.fx.convert(balance, currency, account.base_currency)

        # Position values
        for pos in account.positions:
            market_value = pos.quantity * self.get_price(pos.symbol)
            pos_currency = self.get_currency(pos.symbol)
            nlv += self.fx.convert(market_value, pos_currency, account.base_currency)

        return nlv

    def get_unrealized_pnl(self, position: Position,
                           base_currency: str) -> Decimal:
        current_price = self.get_price(position.symbol)
        pnl_local = position.quantity * (current_price - position.avg_cost)
        pos_currency = self.get_currency(position.symbol)
        return self.fx.convert(pnl_local, pos_currency, base_currency)


class FXRateService:
    """Real-time FX rates from market data."""

    def __init__(self):
        self.rates = {}  # (from, to) -> rate
        # Cross rates derived via USD triangulation
        self.base = "USD"

    def convert(self, amount: Decimal, from_ccy: str, to_ccy: str) -> Decimal:
        if from_ccy == to_ccy:
            return amount
        rate = self.get_rate(from_ccy, to_ccy)
        return amount * rate

    def get_rate(self, from_ccy: str, to_ccy: str) -> Decimal:
        direct = self.rates.get((from_ccy, to_ccy))
        if direct:
            return direct

        # Triangulate via USD
        from_usd = self.rates.get((from_ccy, "USD"), Decimal("1") / self.rates.get(("USD", from_ccy), Decimal("1")))
        usd_to = self.rates.get(("USD", to_ccy), Decimal("1") / self.rates.get((to_ccy, "USD"), Decimal("1")))
        return from_usd * usd_to
```

### 10.2 Advisor Account Hierarchies

```
Advisor Account (FA)
├── Sub-Account 1 (Client A — Individual)
│   ├── Cash: $500K
│   └── Positions: AAPL, GOOGL, SPY
├── Sub-Account 2 (Client B — IRA)
│   ├── Cash: $200K
│   └── Positions: VTI, BND
├── Sub-Account 3 (Client C — Margin)
│   ├── Cash: $1M
│   └── Positions: TSLA, NVDA, options
└── Model Portfolio: "Growth"
    └── Allocation: 60% equities, 30% options, 10% cash
        → Applied to Sub-Accounts 1, 3
```

```python
class AdvisorService:
    """Manages advisor → sub-account relationships and allocation."""

    def submit_allocation_order(self, advisor_id: str, order: Order,
                                allocation: AllocationMethod):
        sub_accounts = self.get_sub_accounts(advisor_id)

        if allocation == AllocationMethod.EQUAL:
            per_account_qty = order.quantity // len(sub_accounts)
            for sub in sub_accounts:
                self.oms.submit(order.copy(account_id=sub.id, quantity=per_account_qty))

        elif allocation == AllocationMethod.BY_EQUITY:
            total_equity = sum(self.get_equity(sub.id) for sub in sub_accounts)
            for sub in sub_accounts:
                ratio = self.get_equity(sub.id) / total_equity
                qty = int(order.quantity * ratio)
                self.oms.submit(order.copy(account_id=sub.id, quantity=qty))

        elif allocation == AllocationMethod.BY_MODEL:
            # Rebalance each sub-account toward target model
            for sub in sub_accounts:
                target_qty = self.calculate_model_target(sub, order.symbol)
                current_qty = self.get_position(sub.id, order.symbol)
                delta = target_qty - current_qty
                if delta != 0:
                    side = "BUY" if delta > 0 else "SELL"
                    self.oms.submit(order.copy(
                        account_id=sub.id, quantity=abs(delta), side=side
                    ))
```

---

## 11. Scalability Strategy

### 11.1 Partitioning Strategy

```
┌─────────────────────────────────────────────────────────┐
│               Partitioning by Domain                     │
│                                                         │
│  OMS: partition by account_id                           │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐      │
│  │Shard 0  │ │Shard 1  │ │Shard 2  │ │Shard N  │      │
│  │accts 0-X│ │accts X-Y│ │accts Y-Z│ │overflow │      │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘      │
│  Key: consistent hashing on account_id                  │
│  Guarantee: all orders for one account on one shard    │
│                                                         │
│  Market Data: partition by symbol                       │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐                  │
│  │Shard 0  │ │Shard 1  │ │Shard 2  │                  │
│  │A-F syms │ │G-M syms │ │N-Z syms │                  │
│  └─────────┘ └─────────┘ └─────────┘                  │
│                                                         │
│  Risk Engine: partition by account_id (same as OMS)     │
│  Exchange Gateway: partition by exchange/venue           │
└─────────────────────────────────────────────────────────┘
```

### 11.2 Horizontal Scaling

| Service | Scaling Strategy | State |
|---------|-----------------|-------|
| API Gateway | Stateless, add instances behind LB | None |
| WebSocket Gateway | Sticky sessions by client_id | Connection state only |
| OMS | Partition by account_id, one sequencer per partition | In-memory + journal |
| Risk Engine | Partition mirrors OMS partitioning | In-memory + cache |
| Market Data (ingest) | One handler per exchange | Stateless (pass-through) |
| Market Data (fan-out) | Partition by symbol range | Subscription registry |
| Position Keeper | Partition by account_id | In-memory + PostgreSQL |
| Exchange Gateway | One per venue | FIX session state |

### 11.3 Hot Symbol Mitigation

Some symbols (AAPL, SPY, TSLA) have disproportionate activity.

```python
class HotSymbolHandler:
    """Dedicated processing for symbols with extreme activity.
    Top 100 symbols get their own fan-out instances."""

    HOT_THRESHOLD = 100_000  # ticks/sec

    def __init__(self):
        self.symbol_rates = Counter()
        self.dedicated_handlers = {}

    def monitor_and_split(self):
        for symbol, rate in self.symbol_rates.most_common(100):
            if rate > self.HOT_THRESHOLD and symbol not in self.dedicated_handlers:
                # Spin up dedicated fan-out for this symbol
                handler = DedicatedFanOutHandler(symbol)
                self.dedicated_handlers[symbol] = handler
                self.rebalance_subscriptions(symbol, handler)

    def rebalance_subscriptions(self, symbol: str, handler):
        # Move all subscriptions for this symbol to dedicated handler
        for client_id in self.subscription_registry.get_subscribers(symbol):
            self.subscription_registry.reassign(client_id, symbol, handler)
```

---

## 12. Failure Handling

### 12.1 Order State Recovery

After a crash, the OMS must recover to the exact pre-crash state without duplicate or lost orders.

```python
class OrderStateRecovery:
    """Rebuild OMS state from the Kafka event log."""

    def recover(self, partition_id: int):
        # 1. Load latest snapshot (taken every N minutes)
        snapshot = self.load_snapshot(partition_id)
        snapshot_offset = snapshot.kafka_offset if snapshot else 0

        # 2. Replay events from snapshot offset
        consumer = KafkaConsumer(
            topic="orders.events",
            partition=partition_id,
            offset=snapshot_offset
        )

        state = snapshot.state if snapshot else {}

        for event in consumer:
            order_id = event.order_id
            if order_id not in state:
                state[order_id] = OrderState()
            state[order_id] = state[order_id].apply(event)

        # 3. Reconcile with exchange
        # For any order in PENDING_NEW or NEW state,
        # send OrderStatusRequest to the exchange
        for order_id, order_state in state.items():
            if order_state.status in ("PENDING_NEW", "NEW", "PARTIAL_FILL"):
                self.exchange_gateway.request_status(order_id)

        return state

    def take_snapshot(self, partition_id: int, state: dict, kafka_offset: int):
        snapshot = Snapshot(
            partition_id=partition_id,
            state=state,
            kafka_offset=kafka_offset,
            timestamp=datetime.utcnow()
        )
        self.snapshot_store.save(snapshot)
```

### 12.2 Exchange Connectivity Failover

```
Primary Exchange Connection          Backup Exchange Connection
┌──────────────────────┐            ┌──────────────────────┐
│  FIX Session A       │            │  FIX Session B       │
│  (Colo DC 1)         │            │  (Colo DC 2)         │
│                      │            │                      │
│  Heartbeat: 30s      │            │  Heartbeat: 30s      │
│  Status: ACTIVE      │            │  Status: STANDBY     │
└──────────┬───────────┘            └──────────┬───────────┘
           │                                   │
           └──────────────┬────────────────────┘
                          │
                 ┌────────▼────────┐
                 │  Connection     │
                 │  Manager        │
                 │                 │
                 │  • Monitors     │
                 │    heartbeats   │
                 │  • Detects      │
                 │    session loss │
                 │  • Promotes     │
                 │    standby      │
                 │  • Resends      │
                 │    in-flight    │
                 │    orders       │
                 └─────────────────┘
```

```python
class ExchangeConnectionManager:
    def __init__(self, primary: FIXSession, backup: FIXSession):
        self.primary = primary
        self.backup = backup
        self.active = primary
        self.in_flight_orders = {}  # order_id -> FIX message

    def on_heartbeat_timeout(self, session: FIXSession):
        if session == self.active:
            self.failover()

    def failover(self):
        old_active = self.active
        self.active = self.backup if self.active == self.primary else self.primary

        # Re-login on new session
        self.active.logon()

        # Reconcile: request status of all in-flight orders
        for order_id, msg in self.in_flight_orders.items():
            self.active.send_order_status_request(order_id)

        # Don't blindly resend — wait for status response
        # to avoid duplicate fills
```

### 12.3 Split-Brain Prevention

```python
class FencingTokenManager:
    """Prevent split-brain in OMS partitions.
    Each partition has a fencing token (epoch number).
    Only the holder of the current token can write."""

    def __init__(self, zk: ZookeeperClient):
        self.zk = zk

    def acquire_leadership(self, partition_id: int) -> int:
        # Atomic increment of epoch in ZooKeeper
        path = f"/oms/partition/{partition_id}/epoch"
        epoch = self.zk.increment(path)

        # All writes to Kafka/DB must include this epoch
        # Consumers reject events with stale epochs
        return epoch

    def validate_epoch(self, partition_id: int, claimed_epoch: int) -> bool:
        current = self.zk.get(f"/oms/partition/{partition_id}/epoch")
        return claimed_epoch == current
```

### 12.4 Circuit Breakers and Backpressure

```python
class OrderRateCircuitBreaker:
    """Protect the system from runaway algo orders or bugs."""

    def __init__(self, config: CircuitBreakerConfig):
        self.config = config
        self.account_counters = defaultdict(lambda: SlidingWindowCounter(60))
        self.global_counter = SlidingWindowCounter(60)
        self.state = CircuitState.CLOSED

    def allow(self, order: Order) -> bool:
        # Per-account rate limit
        account_rate = self.account_counters[order.account_id].count()
        if account_rate > self.config.max_orders_per_account_per_minute:
            return False

        # Global rate limit
        global_rate = self.global_counter.count()
        if global_rate > self.config.max_global_orders_per_second:
            if self.state == CircuitState.CLOSED:
                self.state = CircuitState.OPEN
                self.alert("Circuit breaker OPEN: global order rate exceeded")
            return False

        self.account_counters[order.account_id].increment()
        self.global_counter.increment()
        return True


class BackpressureController:
    """Apply backpressure when downstream systems are slow."""

    def __init__(self):
        self.queue_depths = {}  # service -> depth

    def check_and_shed(self, service: str, request) -> bool:
        depth = self.queue_depths.get(service, 0)
        max_depth = self.config.max_queue_depth[service]

        if depth > max_depth * 0.9:
            # Shed lowest priority traffic
            if request.priority == Priority.LOW:
                return False  # reject
            # Degrade: disable non-essential features
            request.skip_analytics = True
            request.skip_audit_enrichment = True

        if depth > max_depth:
            # Critical: reject all new requests
            return False

        return True
```

---

## 13. Settlement & Clearing

### 13.1 Settlement Lifecycle

```
Trade Day (T)           T+1 (US Equities)
   │                        │
   ├── Trade Execution      │
   ├── Trade Capture        │
   ├── Position Update      │
   ├── Margin Calculation   │
   │                        │
   │  Overnight Batch:      │
   ├── Trade Matching       │
   │   (our records vs      │
   │    clearing firm)      │
   ├── Break Resolution     │
   │                        │
   │                   ┌────▼────────────────┐
   │                   │ Settlement          │
   │                   │ • Cash movement     │
   │                   │ • Security delivery │
   │                   │ • DTCC/CCP          │
   │                   └─────────────────────┘
```

```python
class SettlementEngine:
    """Overnight batch process for trade settlement."""

    def run_nightly(self, trade_date: date):
        # 1. Gather all trades for the day
        trades = self.trade_store.get_by_date(trade_date)

        # 2. Net trades per account per symbol (reduces settlement volume)
        netted = self.net_trades(trades)

        # 3. Match against clearing firm's records
        clearing_trades = self.clearing_firm.get_trades(trade_date)
        matched, breaks = self.match(netted, clearing_trades)

        # 4. Handle breaks
        for brk in breaks:
            self.alert_ops(brk)
            self.break_queue.enqueue(brk)

        # 5. Calculate settlement obligations
        for trade in matched:
            settlement_date = self.get_settlement_date(trade)
            self.create_settlement_instruction(trade, settlement_date)

        # 6. Submit to DTCC/CCP
        self.dtcc_client.submit_instructions(self.pending_settlements)

    def net_trades(self, trades: List[Trade]) -> List[NettedTrade]:
        # Netting: if account bought 1000 AAPL and sold 500 AAPL,
        # settle only the net 500 buy
        groups = defaultdict(list)
        for t in trades:
            groups[(t.account_id, t.symbol, t.settlement_date)].append(t)

        netted = []
        for key, group in groups.items():
            net_qty = sum(t.quantity if t.side == "BUY" else -t.quantity for t in group)
            if net_qty != 0:
                netted.append(NettedTrade(
                    account_id=key[0], symbol=key[1],
                    settlement_date=key[2],
                    side="BUY" if net_qty > 0 else "SELL",
                    quantity=abs(net_qty),
                    avg_price=self.weighted_avg_price(group)
                ))
        return netted
```

---

## 14. Compliance & Surveillance

### 14.1 Regulatory Reporting

```python
class ComplianceEngine:
    """Generates regulatory reports and monitors for violations."""

    def generate_cat_report(self, trade_date: date):
        """Consolidated Audit Trail (CAT) — SEC requirement.
        Every order event must be reported with timestamps."""
        events = self.event_store.get_order_events(trade_date)

        for event in events:
            cat_record = CATRecord(
                event_id=event.id,
                reporter_id=self.firm_id,  # IBKR's CAT ID
                event_type=self.map_to_cat_type(event),
                timestamp=event.timestamp,  # nanosecond precision
                symbol=event.symbol,
                side=event.side,
                quantity=event.quantity,
                price=event.price,
                order_id=event.order_id,
                account_type=self.get_account_type(event.account_id),
            )
            self.cat_reporter.submit(cat_record)

    def check_wash_sale(self, fill: Fill):
        """IRS wash sale rule: if you sell at a loss and buy the same
        security within 30 days, the loss is disallowed."""
        recent_sells = self.trade_store.get_sells(
            account_id=fill.account_id,
            symbol=fill.symbol,
            start=fill.timestamp - timedelta(days=30),
            end=fill.timestamp
        )

        for sell in recent_sells:
            if sell.realized_pnl < 0:
                self.flag_wash_sale(fill, sell)


class TradeSurveillance:
    """Real-time monitoring for suspicious trading patterns."""

    PATTERNS = [
        "spoofing",      # Large orders placed and cancelled rapidly
        "layering",      # Multiple orders at different prices to create false depth
        "front_running",  # Trading ahead of large client orders
        "insider",       # Unusual activity before material announcements
    ]

    def on_order_event(self, event: OrderEvent):
        for detector in self.detectors:
            alert = detector.check(event)
            if alert:
                self.alert_compliance_team(alert)

    class SpoofingDetector:
        def check(self, event: OrderEvent) -> Optional[Alert]:
            if event.type != "CANCELLED":
                return None

            # Check if order was cancelled within 1 second of placement
            order = self.order_store.get(event.order_id)
            lifetime = event.timestamp - order.submitted_at

            if lifetime < timedelta(seconds=1) and order.quantity > self.large_order_threshold:
                cancel_rate = self.get_cancel_rate(
                    order.account_id, order.symbol, window=timedelta(minutes=5)
                )
                if cancel_rate > 0.9:  # >90% of large orders cancelled quickly
                    return Alert(
                        type="SPOOFING",
                        account_id=order.account_id,
                        symbol=order.symbol,
                        evidence=f"Cancel rate {cancel_rate:.0%} for large orders"
                    )
```

---

## 15. Multi-Region Architecture

### 15.1 Global Topology

```
                     ┌─────────────────────────┐
                     │     DNS / Global LB      │
                     │   (latency-based routing) │
                     └────────────┬──────────────┘
                                  │
          ┌───────────────────────┼───────────────────────┐
          │                       │                       │
┌─────────▼──────────┐  ┌────────▼──────────┐  ┌─────────▼──────────┐
│   US East (NY4)    │  │   Europe (LD4)    │  │   Asia (TY3)       │
│                    │  │                    │  │                    │
│ • OMS (US accts)   │  │ • OMS (EU accts)  │  │ • OMS (Asia accts)│
│ • NYSE/NASDAQ      │  │ • LSE/Eurex       │  │ • TSE/HKEX        │
│   colocation       │  │   colocation      │  │   colocation       │
│ • Full market data │  │ • Full market data│  │ • Full market data │
│ • Risk engine      │  │ • Risk engine     │  │ • Risk engine      │
│                    │  │                    │  │                    │
│ ◄──── Cross-region replication (Kafka MirrorMaker) ────►       │
└────────────────────┘  └────────────────────┘  └────────────────────┘
```

**Routing rules:**
- Orders for US exchanges → US East OMS
- Orders for European exchanges → Europe OMS
- Orders for Asian exchanges → Asia OMS
- Client API requests → nearest region (DNS latency-based)
- Cross-region orders (US client trading on TSE) → routed to Asia OMS

### 15.2 Data Replication

```python
class CrossRegionReplication:
    """Replicate critical data across regions for disaster recovery."""

    REPLICATION_POLICY = {
        "orders.events": {
            "mode": "async",           # async to avoid cross-region latency on order path
            "max_lag": timedelta(seconds=5),
            "alert_lag": timedelta(seconds=2),
        },
        "positions.snapshots": {
            "mode": "async",
            "frequency": timedelta(minutes=1),
        },
        "account.balances": {
            "mode": "sync",  # critical financial data — sync replication
            "quorum": 2,     # at least 2 regions must confirm
        },
    }
```

---

## 16. API Design

### 16.1 Order API

```python
# POST /api/v1/orders
{
    "account_id": "U1234567",
    "symbol": "AAPL",
    "side": "BUY",
    "quantity": 100,
    "order_type": "LIMIT",
    "price": 150.25,
    "tif": "DAY",
    "exchange": "SMART"  # SOR decides venue
}

# Response (immediate — order accepted, not filled)
{
    "order_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "status": "PENDING_NEW",
    "submitted_at": "2024-01-15T14:30:00.123456789Z"
}

# WebSocket notification (when fill arrives)
{
    "type": "execution_report",
    "order_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    "status": "FILLED",
    "filled_qty": 100,
    "avg_fill_price": 150.24,
    "commission": 1.00,
    "exchange": "NYSE",
    "executed_at": "2024-01-15T14:30:00.234567890Z"
}
```

### 16.2 Market Data API

```python
# WebSocket: subscribe to real-time quotes
# → Send
{"action": "subscribe", "symbols": ["AAPL", "GOOGL"], "level": "L2"}

# ← Receive (Level 2 depth)
{
    "type": "depth",
    "symbol": "AAPL",
    "timestamp": "2024-01-15T14:30:00.123456789Z",
    "bids": [
        {"price": 150.24, "size": 500, "count": 3},
        {"price": 150.23, "size": 1200, "count": 7},
        {"price": 150.22, "size": 800, "count": 4}
    ],
    "asks": [
        {"price": 150.25, "size": 300, "count": 2},
        {"price": 150.26, "size": 900, "count": 5},
        {"price": 150.27, "size": 1500, "count": 8}
    ]
}

# REST: historical bars
# GET /api/v1/market-data/bars?symbol=AAPL&interval=1m&start=2024-01-15T09:30:00Z&end=2024-01-15T16:00:00Z
{
    "symbol": "AAPL",
    "interval": "1m",
    "bars": [
        {"t": "2024-01-15T09:30:00Z", "o": 150.10, "h": 150.50, "l": 150.05, "c": 150.30, "v": 125000},
        {"t": "2024-01-15T09:31:00Z", "o": 150.30, "h": 150.45, "l": 150.20, "c": 150.35, "v": 98000}
    ]
}
```

### 16.3 Account & Portfolio API

```python
# GET /api/v1/accounts/{account_id}/portfolio
{
    "account_id": "U1234567",
    "base_currency": "USD",
    "net_liquidation": 1250000.00,
    "cash": 250000.00,
    "margin_used": 400000.00,
    "available_funds": 850000.00,
    "buying_power": 3400000.00,  # 4:1 for day trading
    "unrealized_pnl": 45230.50,
    "realized_pnl_today": 1250.00,
    "positions": [
        {
            "symbol": "AAPL",
            "asset_type": "EQUITY",
            "quantity": 500,
            "avg_cost": 145.30,
            "market_price": 150.25,
            "market_value": 75125.00,
            "unrealized_pnl": 2475.00,
            "currency": "USD"
        },
        {
            "symbol": "AAPL 240119C00155000",
            "asset_type": "OPTION",
            "quantity": 10,
            "avg_cost": 5.20,
            "market_price": 6.50,
            "market_value": 6500.00,
            "unrealized_pnl": 1300.00,
            "currency": "USD",
            "option_details": {
                "underlying": "AAPL",
                "strike": 155.00,
                "expiry": "2024-01-19",
                "type": "CALL",
                "multiplier": 100
            }
        }
    ]
}
```

---

## 17. Monitoring & Observability

### 17.1 Key Metrics

| Category | Metric | SLO |
|----------|--------|-----|
| **Order Latency** | Order-to-ack (internal) | P99 < 1ms |
| | Order-to-exchange | P99 < 2ms |
| | Order-to-fill (marketable) | P99 < 10ms |
| **Market Data** | Tick-to-client | P99 < 5ms |
| | Feed handler gap rate | < 0.001% |
| **Risk** | Pre-trade check latency | P99 < 50μs |
| | Margin calculation freshness | < 1s |
| **Availability** | OMS uptime (market hours) | 99.999% |
| | Market data uptime | 99.99% |
| | API uptime | 99.99% |
| **Correctness** | Order state divergence rate | 0% |
| | Position reconciliation breaks | < 0.01% |
| | Settlement breaks | < 0.1% |

### 17.2 Distributed Tracing

```python
# Every order carries a trace through the entire system
@dataclass
class OrderTrace:
    trace_id: UUID
    spans: List[Span]

# Span examples for a single order:
# 1. api_gateway.receive         t=0.000ms   dur=0.02ms
# 2. oms.validate                t=0.020ms   dur=0.05ms
# 3. risk.pretrade_check         t=0.070ms   dur=0.04ms
# 4. oms.sequence                t=0.110ms   dur=0.01ms
# 5. journal.persist             t=0.120ms   dur=0.10ms  (async)
# 6. sor.route                   t=0.130ms   dur=0.08ms
# 7. fix_gateway.send            t=0.210ms   dur=0.05ms
# 8. exchange.ack                t=0.800ms   dur=—
# 9. exchange.fill               t=15.000ms  dur=—
# 10. position_keeper.update     t=15.050ms  dur=0.02ms
# 11. client.notification        t=15.100ms  dur=0.03ms
```

### 17.3 Alerting Rules

```yaml
alerts:
  - name: order_latency_degraded
    condition: p99_order_to_ack > 5ms for 1m
    severity: warning
    action: page_oncall

  - name: order_latency_critical
    condition: p99_order_to_ack > 50ms for 30s
    severity: critical
    action: page_oncall + halt_algo_orders

  - name: market_data_gap
    condition: feed_gap_count > 10 in 1m
    severity: critical
    action: page_oncall + switch_to_backup_feed

  - name: position_divergence
    condition: position_recon_breaks > 0
    severity: critical
    action: page_oncall + halt_new_orders_for_account

  - name: margin_breach
    condition: account_margin_utilization > 1.0
    severity: critical
    action: auto_liquidation_warning + page_risk_team

  - name: circuit_breaker_open
    condition: any_circuit_breaker_state == OPEN
    severity: warning
    action: page_oncall
```

---

## 18. Trade-offs & Decisions

### 18.1 Key Decisions

| Decision | Chosen | Alternative | Why |
|----------|--------|------------|-----|
| Order processing | Single-writer (LMAX) | Multi-threaded with locks | Determinism > raw throughput; replays are exact |
| Event store | Kafka | Custom WAL | Kafka is battle-tested, ecosystem (connect, streams) |
| Market data transport | Shared memory + multicast | TCP pub/sub | Latency; internal network is controlled |
| Risk checks | Synchronous pre-trade | Async post-trade | Regulatory requirement; must reject before execution |
| Position state | In-memory + async persist | DB-first | Latency; positions derived from fills (event sourced) |
| Historical ticks | QuestDB | TimescaleDB / InfluxDB | Ingestion rate; SQL compatibility; column-oriented |
| Cross-region | Async replication | Sync replication | Can't afford cross-region latency on order path |

### 18.2 What We're Sacrificing

| Sacrifice | Impact | Mitigation |
|-----------|--------|-----------|
| Cross-region sync consistency | Regional failure may lose up to 5s of events | Reconciliation on recovery; regulatory dual-write |
| Real-time P&L precision | Up to 1s stale during high volatility | Acceptable for display; risk engine uses real-time |
| Full tick retention | Raw ticks only kept 7 days | OHLCV bars kept indefinitely; regulatory ticks archived |
| Algo order flexibility | Only TWAP/VWAP/Iceberg built-in | Extensible framework for custom algos |
| Mobile-first design | Desktop/API optimized over mobile | Mobile gets conflated data, acceptable for retail |

### 18.3 Build vs Buy

| Component | Decision | Rationale |
|-----------|----------|-----------|
| OMS core | Build | Core IP; no off-the-shelf matches IBKR's multi-asset complexity |
| FIX engine | Build (thin layer over QuickFIX) | Need low-level control for kernel bypass |
| Market data platform | Build | Latency requirements rule out most vendors |
| Risk engine | Build | TIMS-style margining is too complex for generic solutions |
| Database (orders/accounts) | Buy (PostgreSQL) | Proven, understood, sufficient for warm/cold path |
| Time-series DB | Buy (QuestDB) | Purpose-built; building one is a multi-year effort |
| Message bus | Buy (Kafka) | Industry standard; not a differentiator |
| Monitoring | Buy (Prometheus + Grafana) | Standard; custom dashboards on top |

---

## 19. Summary

### System at a Glance

| Component | Technology | Scale |
|-----------|-----------|-------|
| **API Gateway** | Custom (REST + WebSocket + FIX) | 500K concurrent |
| **OMS** | LMAX Disruptor pattern (Java/C++) | 100K orders/sec |
| **Smart Order Router** | Custom (venue-aware, Reg NMS) | Sub-ms routing |
| **Market Data** | Kernel bypass + shared memory | 10M msgs/sec ingest |
| **Risk Engine** | In-memory TIMS model | 50μs pre-trade check |
| **Position Keeper** | Event-sourced, in-memory | Real-time, every fill |
| **Order Store** | Kafka (event log) + PostgreSQL (projection) | 30M events/day |
| **Tick Store** | QuestDB | 10M inserts/sec |
| **Account DB** | PostgreSQL (multi-region) | 2M accounts |
| **Cache** | Redis Cluster | Position/margin state |
| **Settlement** | Overnight batch + DTCC integration | T+1 |
| **Compliance** | Kafka Streams + batch reporting | CAT, FOCUS, SSOI |
| **Regions** | 3 (NY4, LD4, TY3) | Exchange colocation |

### End-to-End Order Flow Summary

1. Client submits order via REST/WebSocket/FIX
2. API Gateway authenticates, rate-limits, routes to correct OMS partition
3. OMS validates order format and account permissions
4. Risk Engine runs pre-trade checks (buying power, limits, fat finger) — **< 50μs**
5. LMAX Sequencer assigns monotonic sequence number — deterministic
6. Event `ORDER_NEW` published to Kafka (async persist)
7. Smart Order Router selects optimal venue(s) based on NBBO, fees, fill rate
8. Exchange Gateway sends FIX NewOrderSingle to exchange — **total internal: < 1ms**
9. Exchange acknowledges → `ORDER_ACK` event → client notified
10. Exchange fills → `ORDER_FILL` event → Position Keeper updates → Margin recalculated
11. Client receives fill notification via WebSocket — **tick-to-client: < 5ms**
12. End-of-day: Settlement engine nets trades, matches with clearing firm, settles T+1
13. Compliance: CAT report generated, surveillance scans for anomalies
