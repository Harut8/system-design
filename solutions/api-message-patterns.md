# API Message Exchange Patterns

Advanced patterns for message-based integration from "Patterns for API Design" and industry best practices.

---

## Table of Contents

1. [Message Exchange Fundamentals](#1-message-exchange-fundamentals)
2. [Request-Response Patterns](#2-request-response-patterns)
3. [Asynchronous Patterns](#3-asynchronous-patterns)
4. [Event-Driven Patterns](#4-event-driven-patterns)
5. [Integration Patterns](#5-integration-patterns)
6. [Resilience Patterns](#6-resilience-patterns)

---

## 1. Message Exchange Fundamentals

### 1.1 Message Types

```
┌─────────────────────────────────────────────────────────────┐
│                    Message Categories                        │
├─────────────────┬───────────────────┬───────────────────────┤
│    Command      │      Query        │       Event           │
│   (Request)     │    (Request)      │    (Notification)     │
├─────────────────┼───────────────────┼───────────────────────┤
│ "Do something"  │ "Tell me about"   │ "Something happened"  │
│ Changes state   │ No side effects   │ Fact about past       │
│ Expects result  │ Returns data      │ No response expected  │
└─────────────────┴───────────────────┴───────────────────────┘
```

### 1.2 Message Structure

**Command Message:**

```json
{
  "message_id": "msg_abc123",
  "message_type": "command",
  "command_name": "CreateOrder",
  "correlation_id": "corr_xyz789",
  "timestamp": "2023-06-15T10:30:00Z",
  "payload": {
    "customer_id": "cust_456",
    "items": [
      {"product_id": "prod_789", "quantity": 2}
    ]
  },
  "metadata": {
    "source": "checkout-service",
    "version": "1.0",
    "trace_id": "trace_123"
  }
}
```

**Query Message:**

```json
{
  "message_id": "msg_def456",
  "message_type": "query",
  "query_name": "GetOrderStatus",
  "correlation_id": "corr_abc123",
  "timestamp": "2023-06-15T10:31:00Z",
  "parameters": {
    "order_id": "ord_123"
  }
}
```

**Event Message:**

```json
{
  "event_id": "evt_ghi789",
  "event_type": "OrderCreated",
  "aggregate_id": "ord_123",
  "aggregate_type": "Order",
  "timestamp": "2023-06-15T10:30:05Z",
  "version": 1,
  "payload": {
    "customer_id": "cust_456",
    "total_amount": 5999,
    "currency": "USD"
  },
  "metadata": {
    "correlation_id": "corr_xyz789",
    "causation_id": "msg_abc123",
    "source": "order-service"
  }
}
```

---

## 2. Request-Response Patterns

### 2.1 Synchronous Request-Response

**Pattern:** Client blocks until response received.

```
┌────────┐         ┌────────┐
│ Client │         │ Server │
└───┬────┘         └───┬────┘
    │   POST /orders   │
    │─────────────────>│
    │                  │ Process
    │   201 Created    │
    │<─────────────────│
    │                  │
```

**Implementation:**

```python
# Server
@app.post("/api/v1/orders")
async def create_order(order: OrderCreate) -> OrderResponse:
    # Synchronous processing
    result = order_service.create(order)
    return OrderResponse(
        id=result.id,
        status="created",
        created_at=result.created_at
    )

# Client
response = await client.post("/api/v1/orders", json=order_data)
order = response.json()  # Blocks until complete
```

### 2.2 Request-Acknowledge Pattern

**Pattern:** Immediate acknowledgment, async processing.

```
┌────────┐         ┌────────┐         ┌─────────┐
│ Client │         │ Server │         │  Queue  │
└───┬────┘         └───┬────┘         └────┬────┘
    │  POST /orders    │                   │
    │─────────────────>│                   │
    │                  │───Queue task─────>│
    │  202 Accepted    │                   │
    │<─────────────────│                   │
    │                  │                   │
    │ GET /orders/123  │                   │
    │─────────────────>│                   │
    │  200 (pending)   │                   │
    │<─────────────────│                   │
```

**Implementation:**

```python
# Server
@app.post("/api/v1/orders", status_code=202)
async def create_order(order: OrderCreate) -> AcceptedResponse:
    # Generate ID immediately
    order_id = generate_id()

    # Queue for async processing
    await queue.enqueue("process_order", {
        "order_id": order_id,
        "data": order.dict()
    })

    return AcceptedResponse(
        id=order_id,
        status="accepted",
        status_url=f"/api/v1/orders/{order_id}",
        retry_after=5
    )

# Response
{
  "id": "ord_abc123",
  "status": "accepted",
  "message": "Order accepted for processing",
  "_links": {
    "status": "/api/v1/orders/ord_abc123",
    "cancel": "/api/v1/orders/ord_abc123/cancel"
  },
  "retry_after": 5
}
```

### 2.3 Polling Pattern

**Pattern:** Client repeatedly checks for completion.

```
┌────────┐              ┌────────┐
│ Client │              │ Server │
└───┬────┘              └───┬────┘
    │                       │
    │ GET /jobs/123/status  │
    │──────────────────────>│
    │ 200 {status:pending}  │
    │<──────────────────────│
    │                       │
    │     (wait 5 sec)      │
    │                       │
    │ GET /jobs/123/status  │
    │──────────────────────>│
    │ 200 {status:complete} │
    │<──────────────────────│
```

**With Exponential Backoff:**

```python
async def poll_for_result(job_id: str, max_attempts: int = 10):
    base_delay = 1  # seconds

    for attempt in range(max_attempts):
        response = await client.get(f"/api/v1/jobs/{job_id}")
        result = response.json()

        if result["status"] == "completed":
            return result["data"]

        if result["status"] == "failed":
            raise JobFailedError(result["error"])

        # Exponential backoff with jitter
        delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
        delay = min(delay, 60)  # Cap at 60 seconds

        await asyncio.sleep(delay)

    raise TimeoutError("Job did not complete in time")
```

### 2.4 Callback Pattern (Webhooks)

**Pattern:** Server notifies client when complete.

```
┌────────┐         ┌────────┐         ┌──────────┐
│ Client │         │ Server │         │ Callback │
└───┬────┘         └───┬────┘         └────┬─────┘
    │ POST /orders     │                   │
    │ callback_url:... │                   │
    │─────────────────>│                   │
    │ 202 Accepted     │                   │
    │<─────────────────│                   │
    │                  │                   │
    │                  │    (process)      │
    │                  │                   │
    │                  │ POST callback_url │
    │                  │──────────────────>│
    │                  │      200 OK       │
    │                  │<──────────────────│
```

**Request with Callback:**

```json
{
  "order": {
    "customer_id": "cust_456",
    "items": [...]
  },
  "webhook": {
    "url": "https://client.example.com/webhooks/orders",
    "secret": "whsec_abc123",
    "events": ["order.completed", "order.failed"]
  }
}
```

**Webhook Payload (Stripe-style):**

```json
{
  "id": "evt_abc123",
  "type": "order.completed",
  "created": 1623456789,
  "data": {
    "object": {
      "id": "ord_xyz789",
      "status": "completed",
      "total": 5999
    }
  },
  "api_version": "2023-06-15"
}
```

**Webhook Security:**

```python
import hmac
import hashlib

def verify_webhook(payload: bytes, signature: str, secret: str) -> bool:
    """Verify webhook signature (Stripe-style)."""
    timestamp, sig = parse_signature_header(signature)

    # Check timestamp to prevent replay attacks
    if abs(time.time() - timestamp) > 300:  # 5 minute tolerance
        return False

    # Compute expected signature
    signed_payload = f"{timestamp}.{payload.decode()}"
    expected = hmac.new(
        secret.encode(),
        signed_payload.encode(),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(sig, expected)
```

---

## 3. Asynchronous Patterns

### 3.1 Message Queue Pattern

**Pattern:** Decouple producers and consumers via queue.

```
┌──────────┐      ┌─────────┐      ┌──────────┐
│ Producer │─────>│  Queue  │─────>│ Consumer │
└──────────┘      └─────────┘      └──────────┘
     │                                   │
     │            ┌─────────┐            │
     └───────────>│  Queue  │───────────>│
                  └─────────┘
```

**AWS SQS Example:**

```python
import boto3

sqs = boto3.client('sqs')

# Producer
def send_order_message(order: dict):
    response = sqs.send_message(
        QueueUrl='https://sqs.us-east-1.amazonaws.com/123/orders',
        MessageBody=json.dumps(order),
        MessageAttributes={
            'OrderType': {
                'DataType': 'String',
                'StringValue': order['type']
            }
        },
        MessageDeduplicationId=order['id'],  # For FIFO queues
        MessageGroupId=order['customer_id']   # For FIFO queues
    )
    return response['MessageId']

# Consumer
def process_messages():
    while True:
        response = sqs.receive_message(
            QueueUrl='https://sqs.us-east-1.amazonaws.com/123/orders',
            MaxNumberOfMessages=10,
            WaitTimeSeconds=20,  # Long polling
            VisibilityTimeout=300
        )

        for message in response.get('Messages', []):
            try:
                order = json.loads(message['Body'])
                process_order(order)

                # Delete after successful processing
                sqs.delete_message(
                    QueueUrl=queue_url,
                    ReceiptHandle=message['ReceiptHandle']
                )
            except Exception as e:
                # Message will be retried after visibility timeout
                log.error(f"Failed to process: {e}")
```

### 3.2 Publish-Subscribe Pattern

**Pattern:** One-to-many message distribution.

```
                    ┌─────────────┐
              ┌────>│ Subscriber1 │
              │     └─────────────┘
┌───────────┐ │     ┌─────────────┐
│ Publisher │─┼────>│ Subscriber2 │
└───────────┘ │     └─────────────┘
              │     ┌─────────────┐
              └────>│ Subscriber3 │
                    └─────────────┘
```

**Kafka Example:**

```python
from kafka import KafkaProducer, KafkaConsumer

# Publisher
producer = KafkaProducer(
    bootstrap_servers=['localhost:9092'],
    value_serializer=lambda v: json.dumps(v).encode()
)

def publish_event(topic: str, event: dict):
    future = producer.send(
        topic,
        key=event['aggregate_id'].encode(),
        value=event,
        headers=[
            ('event_type', event['type'].encode()),
            ('correlation_id', event['correlation_id'].encode())
        ]
    )
    return future.get(timeout=10)

# Subscriber
consumer = KafkaConsumer(
    'order-events',
    bootstrap_servers=['localhost:9092'],
    group_id='notification-service',
    auto_offset_reset='earliest',
    enable_auto_commit=False,
    value_deserializer=lambda m: json.loads(m.decode())
)

for message in consumer:
    event = message.value
    try:
        handle_event(event)
        consumer.commit()
    except Exception as e:
        log.error(f"Failed to handle event: {e}")
```

### 3.3 Fan-Out/Fan-In Pattern

**Pattern:** Parallel processing with aggregation.

```
              ┌─────────────┐
         ┌───>│  Worker 1   │───┐
         │    └─────────────┘   │
┌───────┐│    ┌─────────────┐   │┌───────────┐
│ Split │├───>│  Worker 2   │───┼│ Aggregate │
└───────┘│    └─────────────┘   │└───────────┘
         │    ┌─────────────┐   │
         └───>│  Worker 3   │───┘
              └─────────────┘
```

**Implementation:**

```python
import asyncio

async def fan_out_fan_in(items: list, process_func, aggregate_func):
    """Process items in parallel, then aggregate results."""

    # Fan-out: Process all items concurrently
    tasks = [process_func(item) for item in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Separate successes from failures
    successes = []
    failures = []
    for item, result in zip(items, results):
        if isinstance(result, Exception):
            failures.append({"item": item, "error": str(result)})
        else:
            successes.append(result)

    # Fan-in: Aggregate results
    aggregated = aggregate_func(successes)

    return {
        "result": aggregated,
        "success_count": len(successes),
        "failure_count": len(failures),
        "failures": failures
    }

# Usage
async def enrich_product(product_id: str):
    """Fetch product details from multiple sources."""
    async with aiohttp.ClientSession() as session:
        tasks = [
            fetch_inventory(session, product_id),
            fetch_pricing(session, product_id),
            fetch_reviews(session, product_id)
        ]
        inventory, pricing, reviews = await asyncio.gather(*tasks)

        return {
            "id": product_id,
            "inventory": inventory,
            "pricing": pricing,
            "reviews": reviews
        }
```

### 3.4 Saga Pattern

**Pattern:** Coordinate distributed transactions with compensating actions.

```
┌─────────────────────────────────────────────────────────────┐
│                     Order Saga                               │
├─────────────────────────────────────────────────────────────┤
│ Step 1: Reserve Inventory      Compensate: Release          │
│ Step 2: Process Payment        Compensate: Refund           │
│ Step 3: Create Shipping        Compensate: Cancel           │
│ Step 4: Send Notification      Compensate: N/A              │
└─────────────────────────────────────────────────────────────┘

Success Path:
Reserve ───> Payment ───> Shipping ───> Notify ───> Complete

Failure at Payment:
Reserve ───> Payment(fail) ───> Release Inventory ───> Fail
```

**Choreography-based Saga:**

```python
# Each service listens for events and publishes its own

# Inventory Service
@event_handler("OrderCreated")
async def handle_order_created(event: OrderCreated):
    try:
        reservation = await inventory.reserve(event.items)
        await publish(InventoryReserved(
            order_id=event.order_id,
            reservation_id=reservation.id
        ))
    except InsufficientInventoryError:
        await publish(InventoryReservationFailed(
            order_id=event.order_id,
            reason="insufficient_inventory"
        ))

# Payment Service
@event_handler("InventoryReserved")
async def handle_inventory_reserved(event: InventoryReserved):
    try:
        payment = await payments.charge(event.order_id)
        await publish(PaymentCompleted(
            order_id=event.order_id,
            payment_id=payment.id
        ))
    except PaymentFailedError:
        await publish(PaymentFailed(
            order_id=event.order_id,
            reason="payment_declined"
        ))

# Inventory Service - Compensate
@event_handler("PaymentFailed")
async def handle_payment_failed(event: PaymentFailed):
    await inventory.release(event.order_id)
    await publish(InventoryReleased(order_id=event.order_id))
```

**Orchestration-based Saga:**

```python
class OrderSaga:
    """Central orchestrator for order processing saga."""

    async def execute(self, order: Order) -> SagaResult:
        saga_log = SagaLog(saga_id=generate_id())

        try:
            # Step 1: Reserve Inventory
            reservation = await self.inventory_client.reserve(order.items)
            saga_log.add_step("reserve_inventory", reservation.id)

            # Step 2: Process Payment
            payment = await self.payment_client.charge(
                order.customer_id,
                order.total
            )
            saga_log.add_step("process_payment", payment.id)

            # Step 3: Create Shipping
            shipment = await self.shipping_client.create(
                order.id,
                order.shipping_address
            )
            saga_log.add_step("create_shipping", shipment.id)

            # Step 4: Complete Order
            await self.order_service.complete(order.id)

            return SagaResult(status="completed", saga_log=saga_log)

        except Exception as e:
            # Compensate in reverse order
            await self.compensate(saga_log)
            return SagaResult(status="failed", error=str(e))

    async def compensate(self, saga_log: SagaLog):
        """Execute compensating actions in reverse order."""
        for step in reversed(saga_log.steps):
            compensator = self.compensators.get(step.name)
            if compensator:
                try:
                    await compensator(step.resource_id)
                except Exception as e:
                    log.error(f"Compensation failed for {step.name}: {e}")
                    # May need manual intervention
```

---

## 4. Event-Driven Patterns

### 4.1 Event Sourcing

**Pattern:** Store state changes as sequence of events.

```
┌─────────────────────────────────────────────────────────────┐
│                     Event Store                              │
├─────────────────────────────────────────────────────────────┤
│ Event 1: AccountOpened     { account_id: "123", ... }       │
│ Event 2: MoneyDeposited    { amount: 1000 }                 │
│ Event 3: MoneyWithdrawn    { amount: 500 }                  │
│ Event 4: MoneyDeposited    { amount: 250 }                  │
├─────────────────────────────────────────────────────────────┤
│ Current State: Balance = 1000 - 500 + 250 = 750             │
└─────────────────────────────────────────────────────────────┘
```

**Implementation:**

```python
from dataclasses import dataclass
from typing import List
from abc import ABC, abstractmethod

@dataclass
class Event:
    event_id: str
    aggregate_id: str
    event_type: str
    timestamp: datetime
    data: dict
    version: int

class Aggregate(ABC):
    def __init__(self, aggregate_id: str):
        self.id = aggregate_id
        self.version = 0
        self.uncommitted_events: List[Event] = []

    @abstractmethod
    def apply(self, event: Event) -> None:
        """Apply event to update state."""
        pass

    def load_from_history(self, events: List[Event]) -> None:
        """Replay events to reconstruct state."""
        for event in events:
            self.apply(event)
            self.version = event.version

    def raise_event(self, event_type: str, data: dict) -> None:
        """Create and apply new event."""
        event = Event(
            event_id=generate_id(),
            aggregate_id=self.id,
            event_type=event_type,
            timestamp=datetime.utcnow(),
            data=data,
            version=self.version + 1
        )
        self.apply(event)
        self.uncommitted_events.append(event)
        self.version = event.version

class BankAccount(Aggregate):
    def __init__(self, account_id: str):
        super().__init__(account_id)
        self.balance = 0
        self.is_open = False

    def open(self, initial_deposit: int) -> None:
        if self.is_open:
            raise ValueError("Account already open")
        self.raise_event("AccountOpened", {
            "initial_deposit": initial_deposit
        })

    def deposit(self, amount: int) -> None:
        if not self.is_open:
            raise ValueError("Account not open")
        self.raise_event("MoneyDeposited", {"amount": amount})

    def withdraw(self, amount: int) -> None:
        if amount > self.balance:
            raise ValueError("Insufficient funds")
        self.raise_event("MoneyWithdrawn", {"amount": amount})

    def apply(self, event: Event) -> None:
        if event.event_type == "AccountOpened":
            self.is_open = True
            self.balance = event.data["initial_deposit"]
        elif event.event_type == "MoneyDeposited":
            self.balance += event.data["amount"]
        elif event.event_type == "MoneyWithdrawn":
            self.balance -= event.data["amount"]

# Event Store
class EventStore:
    async def save(self, aggregate: Aggregate) -> None:
        """Save uncommitted events with optimistic concurrency."""
        if not aggregate.uncommitted_events:
            return

        # Check version for optimistic locking
        current_version = await self.get_version(aggregate.id)
        expected_version = aggregate.version - len(aggregate.uncommitted_events)

        if current_version != expected_version:
            raise ConcurrencyError("Aggregate was modified")

        await self.append_events(aggregate.id, aggregate.uncommitted_events)
        aggregate.uncommitted_events.clear()

    async def load(self, aggregate_id: str) -> List[Event]:
        """Load all events for an aggregate."""
        return await self.get_events(aggregate_id)
```

### 4.2 CQRS (Command Query Responsibility Segregation)

**Pattern:** Separate read and write models.

```
                        ┌─────────────────┐
                   ┌───>│  Command Model  │
                   │    │   (Write DB)    │
┌─────────┐        │    └────────┬────────┘
│ Command │────────┘             │ Events
└─────────┘                      ▼
                        ┌─────────────────┐
                        │  Event Handler  │
                        └────────┬────────┘
                                 │
┌─────────┐             ┌────────▼────────┐
│  Query  │────────────>│   Query Model   │
└─────────┘             │   (Read DB)     │
                        └─────────────────┘
```

**Implementation:**

```python
# Command Side
class OrderCommandHandler:
    def __init__(self, event_store: EventStore, event_bus: EventBus):
        self.event_store = event_store
        self.event_bus = event_bus

    async def handle_create_order(self, cmd: CreateOrderCommand) -> str:
        # Create aggregate
        order = Order(generate_id())
        order.create(
            customer_id=cmd.customer_id,
            items=cmd.items
        )

        # Save events
        await self.event_store.save(order)

        # Publish events for projections
        for event in order.uncommitted_events:
            await self.event_bus.publish(event)

        return order.id

# Query Side - Projection
class OrderProjection:
    def __init__(self, read_db: Database):
        self.read_db = read_db

    @event_handler("OrderCreated")
    async def on_order_created(self, event: Event):
        await self.read_db.orders.insert({
            "id": event.aggregate_id,
            "customer_id": event.data["customer_id"],
            "status": "created",
            "total": event.data["total"],
            "created_at": event.timestamp
        })

    @event_handler("OrderShipped")
    async def on_order_shipped(self, event: Event):
        await self.read_db.orders.update(
            {"id": event.aggregate_id},
            {"$set": {
                "status": "shipped",
                "shipped_at": event.timestamp,
                "tracking_number": event.data["tracking_number"]
            }}
        )

# Query Side - Read Model
class OrderQueryService:
    def __init__(self, read_db: Database):
        self.read_db = read_db

    async def get_order(self, order_id: str) -> Optional[OrderView]:
        doc = await self.read_db.orders.find_one({"id": order_id})
        return OrderView(**doc) if doc else None

    async def get_customer_orders(
        self,
        customer_id: str,
        status: Optional[str] = None
    ) -> List[OrderView]:
        query = {"customer_id": customer_id}
        if status:
            query["status"] = status

        docs = await self.read_db.orders.find(query).to_list()
        return [OrderView(**doc) for doc in docs]
```

### 4.3 Outbox Pattern

**Pattern:** Ensure reliable event publishing with local transactions.

```
┌─────────────────────────────────────────────────────────────┐
│                    Same Transaction                          │
├──────────────────────────┬──────────────────────────────────┤
│    Business Table        │         Outbox Table             │
│    ┌─────────────┐       │       ┌─────────────────┐        │
│    │   orders    │       │       │ outbox_events   │        │
│    │ - id        │       │       │ - id            │        │
│    │ - status    │       │       │ - aggregate_id  │        │
│    │ - ...       │       │       │ - event_type    │        │
│    └─────────────┘       │       │ - payload       │        │
│                          │       │ - published_at  │        │
│                          │       └─────────────────┘        │
└──────────────────────────┴──────────────────────────────────┘
                                           │
                                           ▼
                               ┌─────────────────────┐
                               │   Outbox Publisher  │
                               │   (Polling/CDC)     │
                               └─────────────────────┘
                                           │
                                           ▼
                               ┌─────────────────────┐
                               │   Message Broker    │
                               └─────────────────────┘
```

**Implementation:**

```python
# Within same database transaction
class OrderService:
    async def create_order(self, data: CreateOrderData) -> Order:
        async with self.db.transaction() as tx:
            # Insert order
            order = Order(
                id=generate_id(),
                customer_id=data.customer_id,
                status="created"
            )
            await tx.orders.insert(order)

            # Insert event into outbox (same transaction!)
            await tx.outbox.insert({
                "id": generate_id(),
                "aggregate_type": "Order",
                "aggregate_id": order.id,
                "event_type": "OrderCreated",
                "payload": order.to_event_payload(),
                "created_at": datetime.utcnow(),
                "published_at": None
            })

            return order

# Outbox Publisher (separate process)
class OutboxPublisher:
    async def run(self):
        while True:
            async with self.db.transaction() as tx:
                # Get unpublished events
                events = await tx.outbox.find(
                    {"published_at": None},
                    limit=100,
                    for_update=True  # Lock rows
                )

                for event in events:
                    try:
                        await self.message_broker.publish(
                            topic=f"{event['aggregate_type']}.events",
                            message=event['payload']
                        )

                        await tx.outbox.update(
                            {"id": event["id"]},
                            {"published_at": datetime.utcnow()}
                        )
                    except Exception as e:
                        log.error(f"Failed to publish event: {e}")

            await asyncio.sleep(1)  # Poll interval
```

---

## 5. Integration Patterns

### 5.1 API Gateway Pattern

**Pattern:** Single entry point for all clients.

```
┌────────────────────────────────────────────────────────────┐
│                       API Gateway                           │
├────────────────────────────────────────────────────────────┤
│  - Authentication/Authorization                             │
│  - Rate Limiting                                            │
│  - Request/Response Transformation                          │
│  - Load Balancing                                           │
│  - Caching                                                  │
│  - Request Routing                                          │
└──────────────────────────┬─────────────────────────────────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
  │ User Service│   │Order Service│   │Product Svc  │
  └─────────────┘   └─────────────┘   └─────────────┘
```

**Kong/AWS API Gateway Config:**

```yaml
# Kong declarative config
services:
  - name: user-service
    url: http://user-service:8080
    routes:
      - name: users-route
        paths:
          - /api/v1/users
        methods:
          - GET
          - POST
    plugins:
      - name: rate-limiting
        config:
          minute: 100
          policy: local
      - name: jwt
        config:
          secret_is_base64: false
      - name: request-transformer
        config:
          add:
            headers:
              - "X-Request-ID:$(uuid)"
```

### 5.2 Backend for Frontend (BFF)

**Pattern:** Dedicated backend per frontend type.

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  Mobile App │    │   Web App   │    │  Admin App  │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       ▼                  ▼                  ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ Mobile BFF  │    │   Web BFF   │    │  Admin BFF  │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       └──────────────────┼──────────────────┘
                          ▼
              ┌───────────────────────┐
              │   Backend Services    │
              └───────────────────────┘
```

**Mobile BFF Example:**

```python
# Mobile BFF - Optimized for mobile constraints
@app.get("/api/mobile/v1/home")
async def get_home_screen():
    """
    Aggregate data for mobile home screen in single call.
    Optimized for bandwidth and battery.
    """
    async with aiohttp.ClientSession() as session:
        # Parallel calls to backend services
        user_task = fetch_user_summary(session)
        orders_task = fetch_recent_orders(session, limit=3)
        recommendations_task = fetch_recommendations(session, limit=5)

        user, orders, recommendations = await asyncio.gather(
            user_task, orders_task, recommendations_task
        )

    # Mobile-optimized response (smaller payload)
    return {
        "user": {
            "name": user["display_name"],
            "avatar_url": user["avatar_thumbnail_url"]  # Smaller image
        },
        "recent_orders": [
            {
                "id": o["id"],
                "status": o["status"],
                "total": o["total"]
            } for o in orders
        ],
        "recommendations": [
            {
                "id": r["id"],
                "name": r["name"][:50],  # Truncated
                "price": r["price"],
                "image_url": r["thumbnail_url"]
            } for r in recommendations
        ]
    }
```

### 5.3 Anti-Corruption Layer

**Pattern:** Isolate domain from external systems.

```
┌─────────────────────────────────────────────────────────────┐
│                      Your Domain                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │              Anti-Corruption Layer                   │    │
│  │  ┌───────────────┐  ┌───────────────────────────┐   │    │
│  │  │   Adapter     │  │       Translator          │   │    │
│  │  │ (Interface)   │  │  (Transform Data)         │   │    │
│  │  └───────┬───────┘  └─────────────┬─────────────┘   │    │
│  └──────────┼────────────────────────┼─────────────────┘    │
│             │                        │                       │
└─────────────┼────────────────────────┼───────────────────────┘
              │                        │
              ▼                        ▼
     ┌─────────────────────────────────────────┐
     │           Legacy/External System         │
     └─────────────────────────────────────────┘
```

**Implementation:**

```python
# Your domain model
@dataclass
class Customer:
    id: str
    email: str
    name: str
    tier: CustomerTier
    created_at: datetime

# Legacy system model (different structure, naming)
@dataclass
class LegacyCRMContact:
    CONTACT_ID: int
    EMAIL_ADDR: str
    FIRST_NM: str
    LAST_NM: str
    ACCT_TYPE_CD: str
    CREATE_DT: str

# Anti-Corruption Layer
class CustomerAdapter:
    """Adapter to legacy CRM system."""

    def __init__(self, legacy_client: LegacyCRMClient):
        self._client = legacy_client
        self._translator = CustomerTranslator()

    async def get_customer(self, customer_id: str) -> Customer:
        # Call legacy system
        legacy_id = self._translate_id_to_legacy(customer_id)
        legacy_contact = await self._client.get_contact(legacy_id)

        # Translate to domain model
        return self._translator.to_domain(legacy_contact)

    async def save_customer(self, customer: Customer) -> None:
        # Translate to legacy format
        legacy_contact = self._translator.to_legacy(customer)
        await self._client.update_contact(legacy_contact)

class CustomerTranslator:
    """Translate between domain and legacy models."""

    TIER_MAPPING = {
        "G": CustomerTier.GOLD,
        "S": CustomerTier.SILVER,
        "B": CustomerTier.BRONZE,
        "R": CustomerTier.REGULAR
    }

    def to_domain(self, legacy: LegacyCRMContact) -> Customer:
        return Customer(
            id=f"cust_{legacy.CONTACT_ID}",
            email=legacy.EMAIL_ADDR.lower(),
            name=f"{legacy.FIRST_NM} {legacy.LAST_NM}".strip(),
            tier=self.TIER_MAPPING.get(legacy.ACCT_TYPE_CD, CustomerTier.REGULAR),
            created_at=datetime.strptime(legacy.CREATE_DT, "%Y%m%d")
        )

    def to_legacy(self, customer: Customer) -> LegacyCRMContact:
        names = customer.name.split(" ", 1)
        return LegacyCRMContact(
            CONTACT_ID=int(customer.id.replace("cust_", "")),
            EMAIL_ADDR=customer.email.upper(),
            FIRST_NM=names[0] if names else "",
            LAST_NM=names[1] if len(names) > 1 else "",
            ACCT_TYPE_CD=self._reverse_tier_mapping(customer.tier),
            CREATE_DT=customer.created_at.strftime("%Y%m%d")
        )
```

---

## 6. Resilience Patterns

### 6.1 Circuit Breaker

**Pattern:** Prevent cascading failures by failing fast.

```
         ┌─────────┐
    ┌───>│  CLOSED │<────────────────────┐
    │    └────┬────┘                     │
    │         │ Failure threshold        │ Success threshold
    │         ▼                          │
    │    ┌─────────┐      Timeout   ┌────┴─────┐
    │    │  OPEN   │───────────────>│ HALF-OPEN│
    │    └─────────┘                └──────────┘
    │         │                          │
    └─────────┴──────────────────────────┘
           Reset timeout
```

**Implementation:**

```python
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    success_threshold: int = 3
    timeout: timedelta = timedelta(seconds=30)

    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    success_count: int = field(default=0)
    last_failure_time: datetime = field(default=None)

    async def call(self, func, *args, **kwargs):
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
            else:
                raise CircuitOpenError(f"Circuit {self.name} is open")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise

    def _should_attempt_reset(self) -> bool:
        if self.last_failure_time is None:
            return True
        return datetime.utcnow() - self.last_failure_time >= self.timeout

    def _on_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0

    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()
        self.success_count = 0

        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN

# Usage
payment_circuit = CircuitBreaker(name="payment-service")

async def process_payment(amount: int):
    return await payment_circuit.call(
        payment_client.charge,
        amount=amount
    )
```

### 6.2 Retry with Backoff

**Pattern:** Retry transient failures with increasing delays.

```python
import asyncio
import random
from typing import Type, Tuple

async def retry_with_backoff(
    func,
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
    **kwargs
):
    """
    Retry function with exponential backoff and jitter.
    """
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as e:
            last_exception = e

            if attempt == max_retries:
                break

            # Calculate delay with exponential backoff
            delay = min(
                base_delay * (exponential_base ** attempt),
                max_delay
            )

            # Add jitter to prevent thundering herd
            if jitter:
                delay = delay * (0.5 + random.random())

            log.warning(
                f"Attempt {attempt + 1} failed: {e}. "
                f"Retrying in {delay:.2f}s..."
            )

            await asyncio.sleep(delay)

    raise last_exception

# Usage
result = await retry_with_backoff(
    external_api.call,
    endpoint="/data",
    max_retries=5,
    retryable_exceptions=(ConnectionError, TimeoutError)
)
```

### 6.3 Bulkhead

**Pattern:** Isolate failures to prevent resource exhaustion.

```python
import asyncio
from contextlib import asynccontextmanager

class Bulkhead:
    """
    Limit concurrent access to a resource.
    """

    def __init__(self, name: str, max_concurrent: int, max_waiting: int = 100):
        self.name = name
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.waiting_count = 0
        self.max_waiting = max_waiting

    @asynccontextmanager
    async def acquire(self, timeout: float = None):
        if self.waiting_count >= self.max_waiting:
            raise BulkheadFullError(
                f"Bulkhead {self.name} queue is full"
            )

        self.waiting_count += 1
        try:
            acquired = await asyncio.wait_for(
                self.semaphore.acquire(),
                timeout=timeout
            )
            if not acquired:
                raise BulkheadTimeoutError(
                    f"Timeout waiting for bulkhead {self.name}"
                )
            try:
                yield
            finally:
                self.semaphore.release()
        finally:
            self.waiting_count -= 1

# Usage - Separate bulkheads for different services
payment_bulkhead = Bulkhead("payment", max_concurrent=10)
inventory_bulkhead = Bulkhead("inventory", max_concurrent=20)

async def checkout(order):
    async with payment_bulkhead.acquire(timeout=5):
        payment = await payment_service.charge(order.total)

    async with inventory_bulkhead.acquire(timeout=5):
        await inventory_service.reserve(order.items)

    return payment
```

### 6.4 Timeout Pattern

**Pattern:** Limit waiting time for operations.

```python
import asyncio
from contextlib import asynccontextmanager

@asynccontextmanager
async def timeout_context(seconds: float, operation: str = "operation"):
    """Context manager for timeouts with cleanup."""
    try:
        async with asyncio.timeout(seconds):
            yield
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{operation} timed out after {seconds}s"
        )

# Usage
async def fetch_with_timeout(url: str) -> dict:
    async with timeout_context(5.0, f"Fetching {url}"):
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                return await response.json()

# Cascading timeouts for multi-service calls
async def get_order_details(order_id: str) -> dict:
    # Overall timeout
    async with timeout_context(10.0, "get_order_details"):
        # Individual service timeouts
        order = await asyncio.wait_for(
            order_service.get(order_id),
            timeout=3.0
        )

        customer = await asyncio.wait_for(
            customer_service.get(order.customer_id),
            timeout=2.0
        )

        items = await asyncio.wait_for(
            product_service.get_many(order.product_ids),
            timeout=3.0
        )

        return {
            "order": order,
            "customer": customer,
            "items": items
        }
```

### 6.5 Dead Letter Queue

**Pattern:** Handle failed messages that cannot be processed.

```python
class MessageProcessor:
    def __init__(
        self,
        main_queue: Queue,
        dlq: Queue,
        max_retries: int = 3
    ):
        self.main_queue = main_queue
        self.dlq = dlq
        self.max_retries = max_retries

    async def process_messages(self):
        while True:
            message = await self.main_queue.receive()

            try:
                await self._process(message)
                await self.main_queue.ack(message)

            except RetryableError as e:
                retry_count = message.attributes.get("retry_count", 0)

                if retry_count >= self.max_retries:
                    # Move to DLQ after max retries
                    await self.dlq.send(
                        body=message.body,
                        attributes={
                            **message.attributes,
                            "error": str(e),
                            "failed_at": datetime.utcnow().isoformat(),
                            "original_queue": self.main_queue.name
                        }
                    )
                    await self.main_queue.ack(message)
                    log.error(f"Message sent to DLQ: {message.id}")
                else:
                    # Retry with backoff
                    await self.main_queue.nack(
                        message,
                        delay=2 ** retry_count  # Exponential backoff
                    )

            except NonRetryableError as e:
                # Send directly to DLQ
                await self.dlq.send(
                    body=message.body,
                    attributes={
                        **message.attributes,
                        "error": str(e),
                        "error_type": "non_retryable"
                    }
                )
                await self.main_queue.ack(message)

# DLQ Processor (manual intervention or automated reprocessing)
class DLQProcessor:
    async def reprocess_message(self, message_id: str):
        message = await self.dlq.get(message_id)
        original_queue = message.attributes["original_queue"]

        # Send back to original queue
        await self.get_queue(original_queue).send(
            body=message.body,
            attributes={
                "retry_count": 0,
                "reprocessed_at": datetime.utcnow().isoformat()
            }
        )

        await self.dlq.delete(message_id)
```

---

## Summary

These patterns provide building blocks for robust, scalable API integrations:

| Category | Key Patterns | Use Case |
|----------|--------------|----------|
| Request-Response | Sync, Acknowledge, Polling, Callback | Client-server communication |
| Async | Queue, Pub-Sub, Fan-out/Fan-in, Saga | Decoupled processing |
| Event-Driven | Event Sourcing, CQRS, Outbox | State management at scale |
| Integration | Gateway, BFF, Anti-Corruption | System boundaries |
| Resilience | Circuit Breaker, Retry, Bulkhead | Fault tolerance |

Choose patterns based on:
- **Latency requirements** - Sync for low latency, async for tolerance
- **Coupling** - Events for loose coupling, RPC for tight integration
- **Consistency** - Sync for strong, async/saga for eventual
- **Scale** - Async patterns scale better
- **Complexity** - Start simple, add patterns as needed
