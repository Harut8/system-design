# Networking Protocols and Communication: A Complete Interview-Ready Deep Dive

A production-grade reference covering the networking stack that every distributed system sits on top of. Covers TCP/UDP fundamentals (handshakes, congestion control, when to pick UDP), the HTTP evolution from 1.1 through HTTP/2 to HTTP/3 and QUIC, gRPC and Protocol Buffers (why ML serving uses it), REST design principles (idempotency, versioning, HATEOAS), WebSocket (scaling, heartbeats), GraphQL (N+1 problem, when it makes sense), DNS resolution (caching, TTL, failover), TLS and mTLS (certificate rotation, pinning), and connection pooling across HTTP, databases, and gRPC channels.

Every section is written for engineers who will be asked "why did you pick gRPC here instead of REST?" or "how does HTTP/2 multiplexing actually work?" in a system design interview.

Prerequisites: familiarity with distributed system fundamentals from the `README.md` roadmap.

---

## Table of Contents

1. [TCP Fundamentals](#1-tcp-fundamentals)
2. [UDP and When to Use It](#2-udp-and-when-to-use-it)
3. [HTTP/1.1 vs HTTP/2 vs HTTP/3](#3-http11-vs-http2-vs-http3)
4. [gRPC and Protocol Buffers](#4-grpc-and-protocol-buffers)
5. [REST Design Principles](#5-rest-design-principles)
6. [WebSocket](#6-websocket)
7. [GraphQL](#7-graphql)
8. [DNS Resolution](#8-dns-resolution)
9. [TLS and mTLS](#9-tls-and-mtls)
10. [Connection Pooling](#10-connection-pooling)
11. [Protocol Selection for ML/AI Systems](#11-protocol-selection-for-mlai-systems)
12. [Capacity Planning and Performance Math](#12-capacity-planning-and-performance-math)
13. [Failure Modes and Debugging](#13-failure-modes-and-debugging)
14. [Interview Patterns](#14-interview-patterns)

---

## 1. TCP Fundamentals

### 1.1 The Three-Way Handshake

Every TCP connection begins with a three-way handshake. This is not a detail you can skip -- it directly determines the latency floor for every new connection your system opens.

```
CLIENT                                    SERVER
  |                                          |
  |  ──── SYN (seq=x) ──────────────────>    |
  |                                          |  (1 RTT)
  |  <──── SYN-ACK (seq=y, ack=x+1) ────    |
  |                                          |  (1 RTT)
  |  ──── ACK (ack=y+1) + [data] ───────>   |
  |                                          |
  |  Connection established.                 |
  |  Minimum cost: 1 RTT before data flows.  |

Total latency for first byte:
  - TCP handshake:        1 RTT
  - TLS 1.3 handshake:  + 1 RTT  (or 0 for resumption)
  - HTTP request:        + 1 RTT
  ────────────────────────────────────────────
  First byte arrives:     3 RTT minimum (no TLS resumption)
                          2 RTT minimum (with TLS resumption)
```

**Why this matters in system design**: if your service is in us-east and the database is in us-west (~30ms one-way latency), each new TCP connection costs ~60ms just for the handshake. Add TLS and you are at ~120ms before any data moves. This is why connection pooling (§10) is not optional for any serious system.

### 1.2 Congestion Control

TCP's congestion control algorithm determines how fast data actually flows. The two algorithms you need to know:

**Cubic (the default on most Linux kernels)**:
- Uses a cubic function to grow the congestion window after a loss event
- Loss-based: it only backs off when packets are dropped
- Problem: in high-bandwidth, high-latency links (long fat networks), Cubic is too conservative. It takes a long time to fill the pipe after a loss event
- Problem: in datacenter networks with shallow buffers, Cubic causes buffer bloat by filling switch queues

**BBR (Bottleneck Bandwidth and Round-trip propagation time)**:
- Developed by Google, deployed on YouTube, Google Cloud, and most Google services
- Model-based: measures the actual bottleneck bandwidth and minimum RTT, then paces packets to match
- Does not wait for packet loss to reduce rate -- it actively probes and adjusts
- Dramatically better performance on WAN links (2-25x improvement over Cubic for lossy links)

```
Cubic vs BBR behavior on a 100 Mbps link with 1% packet loss:

Cubic:
  Throughput: ~3-5 Mbps  (loss causes aggressive backoff)
  Latency:    high       (fills buffers before detecting congestion)

BBR:
  Throughput: ~70-90 Mbps (paces to bottleneck bandwidth)
  Latency:    low         (avoids filling buffers)
```

**Interview relevance**: when designing a system that sends large payloads over WAN (geo-replicated databases, cross-region model weight transfer, CDN origin pull), mention that BBR is the right congestion control choice and that it is enabled per-socket via `setsockopt` or globally via sysctl.

### 1.3 TCP Slow Start

Every new TCP connection starts with a small congestion window (typically 10 segments = ~14KB) and doubles it each RTT until it hits a threshold or detects loss. This means:

```
RTT 0:  sends 14 KB
RTT 1:  sends 28 KB
RTT 2:  sends 56 KB
RTT 3:  sends 112 KB
RTT 4:  sends 224 KB
...

Time to send a 1 MB response on a fresh connection (30ms RTT):
  ~7 RTTs = ~210ms just for slow start to ramp up

Time to send the same 1 MB on a warm, pooled connection:
  ~30ms   (congestion window already large)
```

**Design implication**: slow start is the reason why small, frequent requests suffer more from new connections than large bulk transfers. For microservice architectures with many small RPCs, connection reuse is critical.

### 1.4 TIME_WAIT and Socket Exhaustion

When a TCP connection closes, the side that initiates the close enters `TIME_WAIT` state for 2 * MSL (Maximum Segment Lifetime, typically 60 seconds on Linux). During this time, the (source IP, source port, dest IP, dest port) tuple is occupied and cannot be reused.

```
Connection close sequence:

ACTIVE CLOSER                    PASSIVE CLOSER
  |                                    |
  |  ──── FIN ────────────────────>    |
  |  <──── ACK ────────────────────    |
  |  <──── FIN ────────────────────    |
  |  ──── ACK ────────────────────>    |
  |                                    |
  |  TIME_WAIT (60s)                   |  CLOSED
  |  ...                               |
  |  CLOSED                            |

Problem: a server handling 10,000 short-lived connections/second
  accumulates 10,000 × 60 = 600,000 TIME_WAIT sockets
  Linux default ephemeral port range: 32768-60999 = ~28,000 ports

  Result: EADDRINUSE errors, connection failures
```

**Mitigations**:
1. **Connection pooling** (§10) -- reuse connections instead of creating/destroying them
2. `SO_REUSEADDR` / `SO_REUSEPORT` -- allow binding to TIME_WAIT sockets
3. `tcp_tw_reuse=1` -- allow reuse of TIME_WAIT sockets for new outbound connections (safe for clients)
4. Increase ephemeral port range: `net.ipv4.ip_local_port_range = 1024 65535`
5. Use long-lived connections (HTTP/2 multiplexing, gRPC channels)

**Interview pattern**: any time you have a service making many short-lived outbound connections (e.g., a proxy service, a load balancer, a service calling many backends), mention TIME_WAIT exhaustion as a failure mode and connection pooling as the fix.

### 1.5 Nagle's Algorithm and TCP_NODELAY

Nagle's algorithm batches small writes into larger TCP segments to reduce the number of packets. This introduces latency (up to 200ms delay) for small messages.

```
Without TCP_NODELAY (Nagle enabled):
  write(4 bytes)  → buffer, wait for ACK or more data
  write(4 bytes)  → buffer, still waiting
  write(4 bytes)  → buffer, ACK arrives, send all 12 bytes
  Latency: 200ms+ for the first 4 bytes

With TCP_NODELAY (Nagle disabled):
  write(4 bytes)  → send immediately
  write(4 bytes)  → send immediately
  write(4 bytes)  → send immediately
  Latency: ~RTT for each write
```

**Rule**: for RPC-style communication (gRPC, Redis, database wire protocols), always set `TCP_NODELAY`. For bulk data transfer (file uploads, backups), leave Nagle enabled. gRPC sets `TCP_NODELAY` by default.

### 1.6 Keep-Alive

TCP keep-alive sends periodic probes on idle connections to detect dead peers. The Linux defaults are terrible for production:

```
Default:
  tcp_keepalive_time  = 7200s  (2 hours before first probe)
  tcp_keepalive_intvl = 75s    (75s between probes)
  tcp_keepalive_probes = 9     (9 failed probes to declare dead)

  Time to detect dead peer: 7200 + (75 × 9) = 7875 seconds = ~2 hours

Production setting:
  tcp_keepalive_time  = 60s
  tcp_keepalive_intvl = 10s
  tcp_keepalive_probes = 6

  Time to detect dead peer: 60 + (10 × 6) = 120 seconds = 2 minutes
```

Application-level keep-alive (gRPC PING frames, WebSocket pings, HTTP/2 PING) is generally preferred because it works through NATs and load balancers that may not forward TCP keep-alive probes.

---

## 2. UDP and When to Use It

### 2.1 UDP Basics

UDP is a connectionless, unreliable datagram protocol. No handshake, no ordering, no retransmission, no congestion control. Each `sendto()` call emits one datagram that either arrives intact or does not arrive at all.

```
TCP vs UDP header overhead:

TCP:  20-60 bytes header + connection state per flow
UDP:  8 bytes header, no connection state

TCP:  Ordered, reliable byte stream
UDP:  Unordered, unreliable datagrams

TCP:  Congestion control (backs off on loss)
UDP:  No congestion control (sends at whatever rate you choose)
```

### 2.2 When to Use UDP

| Use case | Why UDP | Example |
|---|---|---|
| Real-time media (voice, video) | Retransmitting a dropped video frame is worse than skipping it -- the moment has passed | WebRTC, Zoom, Discord |
| Game state updates | Last position update supersedes all previous ones | Multiplayer games, Overwatch |
| DNS queries | Single request-response, fits in one datagram, handshake overhead unacceptable | Every DNS resolver |
| Health checks / heartbeats | Lightweight, no connection setup cost | SWIM protocol gossip |
| QUIC / HTTP/3 | Builds reliability and multiplexing on top of UDP in userspace | Google, Cloudflare |
| Metrics collection | Losing one metric sample is acceptable, throughput matters | StatsD over UDP |
| Service discovery (mDNS, SSDP) | Multicast to discover peers | Consul, Kubernetes |

### 2.3 When Not to Use UDP

**Never** use raw UDP when you need:
- Reliable, ordered delivery of all data (use TCP)
- Congestion-friendly behavior on the public internet (use TCP or QUIC)
- Data integrity beyond the UDP checksum (use TLS over TCP, or DTLS over UDP)

**The QUIC pattern**: if you need UDP's properties (no head-of-line blocking, connection migration) but also need reliability, QUIC builds those guarantees in userspace on top of UDP. This is what HTTP/3 does.

---

## 3. HTTP/1.1 vs HTTP/2 vs HTTP/3

### 3.1 HTTP/1.1 — Sequential and Wasteful

HTTP/1.1 is a text-based protocol with one request-response pair per TCP connection at a time. To achieve concurrency, browsers open 6-8 parallel TCP connections per origin.

```
HTTP/1.1 with persistent connections (Connection: keep-alive):

Connection 1:  [req1] ──> [res1] [req3] ──> [res3] [req5] ──> [res5]
Connection 2:  [req2] ──> [res2] [req4] ──> [res4] [req6] ──> [res6]
                               time ──────────────────>

Problems:
1. Head-of-line (HOL) blocking: req3 cannot start until res1 finishes
2. TCP connection overhead: 6-8 handshakes, 6-8 slow-start ramps
3. Redundant headers: Cookie, User-Agent, Accept sent on every request (~800 bytes)
4. No server push: server cannot send resources proactively
```

**Workarounds that existed (and why they are hacks)**:
- Domain sharding: spread assets across subdomains to bypass 6-connection limit
- Sprite sheets: combine many images into one to reduce requests
- Inlining CSS/JS: embed resources in HTML to avoid round trips
- Concatenation: bundle multiple JS files into one

### 3.2 HTTP/2 — Binary, Multiplexed, Header-Compressed

HTTP/2 is a binary framing protocol that multiplexes multiple logical streams over a single TCP connection.

```
HTTP/2 multiplexing over a single TCP connection:

┌──────────────────────────────────────────────────────┐
│                  Single TCP Connection                │
│                                                      │
│  Stream 1: [HEADERS] [DATA] [DATA]                   │
│  Stream 3: [HEADERS] [DATA]                          │
│  Stream 5: [HEADERS] [DATA] [DATA] [DATA]            │
│  Stream 7: [HEADERS]                                 │
│                                                      │
│  Frames interleaved:                                 │
│  [H:1][H:3][D:1][H:5][D:3][D:5][D:1][H:7][D:5][D:5]│
└──────────────────────────────────────────────────────┘

Key features:
  1. Binary framing:     structured frames, not text parsing
  2. Multiplexing:       multiple streams on one connection
  3. HPACK compression:  header table compresses repeated headers
  4. Server push:        server can push resources before client asks
  5. Stream priority:    client can prioritize streams (deprecated in H2, replaced in H3)
  6. Flow control:       per-stream and per-connection flow control
```

**HPACK header compression**:
```
First request:
  :method: GET
  :path: /api/users
  :authority: api.example.com
  cookie: session=abc123def456...  (200 bytes)
  user-agent: Mozilla/5.0...       (150 bytes)
  Total: ~500 bytes

Second request (same connection):
  :method: GET
  :path: /api/orders         (only the changed header)
  [everything else referenced from header table by index]
  Total: ~20 bytes           (96% reduction)
```

**The remaining problem — TCP head-of-line blocking**:

```
HTTP/2 multiplexing looks great until TCP loses a packet:

TCP stream:  [pkt1:S1] [pkt2:S3] [pkt3:S1] [pkt4:S5] [pkt5:S3]
                                     ↑
                                   LOST

TCP must deliver in order. Even though pkt4 (Stream 5) and
pkt5 (Stream 3) arrived, TCP cannot deliver them to the
application until pkt3 is retransmitted and received.

All streams are blocked by one lost packet on one stream.
This is worse than HTTP/1.1 with 6 connections, where only
1 of 6 connections would be blocked.
```

This is the fundamental problem that HTTP/3 and QUIC solve.

### 3.3 HTTP/3 and QUIC — UDP-Based, No HOL Blocking

HTTP/3 runs over QUIC, a transport protocol built on UDP that provides:

```
QUIC architecture:

┌──────────────────────────────────────────────────────┐
│                    HTTP/3 (Application)               │
├──────────────────────────────────────────────────────┤
│                    QUIC (Transport)                   │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐     │
│  │  Stream 1  │  │  Stream 2  │  │  Stream 3  │     │
│  │  (own seq) │  │  (own seq) │  │  (own seq) │     │
│  └────────────┘  └────────────┘  └────────────┘     │
│                                                      │
│  ┌──────────────────────────────────────────────┐    │
│  │  TLS 1.3 (integrated, not layered on top)    │    │
│  └──────────────────────────────────────────────┘    │
│                                                      │
│  ┌──────────────────────────────────────────────┐    │
│  │  Connection ID (survives IP changes)          │    │
│  └──────────────────────────────────────────────┘    │
├──────────────────────────────────────────────────────┤
│                    UDP (Network)                      │
└──────────────────────────────────────────────────────┘

Key advantages:
1. No HOL blocking:     each stream is independently sequenced
2. 0-RTT handshake:     TLS 1.3 integrated, can send data in first packet
3. Connection migration: connection ID survives Wi-Fi→cellular handoff
4. Userspace control:    congestion control and loss recovery in application
```

**Handshake comparison**:
```
HTTP/1.1 + TLS 1.2:  3 RTT  (TCP SYN + TLS + HTTP)
HTTP/2 + TLS 1.3:    2 RTT  (TCP SYN + TLS/HTTP combined)
HTTP/3 (QUIC):        1 RTT  (QUIC combines transport + crypto)
HTTP/3 0-RTT:         0 RTT  (resumption, sends data immediately)
```

### 3.4 Comparison Matrix

| Feature | HTTP/1.1 | HTTP/2 | HTTP/3 (QUIC) |
|---|---|---|---|
| Transport | TCP | TCP | UDP (QUIC) |
| Multiplexing | No (1 req/conn) | Yes (streams) | Yes (independent streams) |
| HOL blocking | Per-connection | TCP-level (all streams) | None (per-stream only) |
| Header compression | None | HPACK | QPACK |
| Handshake latency | 3 RTT | 2 RTT | 1 RTT (0 with resumption) |
| Connection migration | No | No | Yes (connection ID) |
| Server push | No | Yes | Yes (rarely used) |
| Encryption | Optional (TLS) | Effectively required | Always (built-in TLS 1.3) |
| Deployment complexity | Low | Medium | High (UDP, middlebox issues) |

### 3.5 When to Use Which

| Scenario | Protocol | Why |
|---|---|---|
| Internal microservices (datacenter) | HTTP/2 (via gRPC) | Low latency, multiplexing, no UDP middlebox issues |
| Client-facing web/mobile API | HTTP/2, migrating to HTTP/3 | Broad support, good performance |
| High-latency mobile clients | HTTP/3 | 0-RTT, connection migration, no HOL blocking |
| Legacy integrations | HTTP/1.1 | Compatibility, simplicity |
| Real-time bidirectional | WebSocket over HTTP/1.1 or HTTP/2 | Long-lived connection, full-duplex |
| CDN edge to origin | HTTP/2 | Multiplexing reduces origin connections |

---

## 4. gRPC and Protocol Buffers

### 4.1 What gRPC Is

gRPC is a high-performance RPC framework built on HTTP/2, using Protocol Buffers (protobuf) as its interface definition language and serialization format. It is the standard for service-to-service communication in modern distributed systems and is the dominant protocol for ML model serving.

```
gRPC architecture:

┌──────────────────┐                    ┌──────────────────┐
│   gRPC Client    │                    │   gRPC Server    │
│                  │                    │                  │
│  Generated Stub  │  ─── HTTP/2 ────> │  Service Impl    │
│  (type-safe)     │  ← protobuf ────  │  (registered     │
│                  │    binary frames   │   handlers)      │
└──────────────────┘                    └──────────────────┘

Stack:
  Application:    Generated client/server stubs
  Serialization:  Protocol Buffers (binary, schema-driven)
  Transport:      HTTP/2 (multiplexing, streaming, flow control)
  Security:       TLS / mTLS (optional but recommended)
```

### 4.2 Protocol Buffers (Protobuf)

Protobuf is a binary serialization format with a schema defined in `.proto` files. Code generation produces type-safe client and server code in 10+ languages.

```protobuf
// recommendation_service.proto

syntax = "proto3";
package ml.serving;

service RecommendationService {
  // Unary: one request, one response
  rpc GetRecommendations(RecommendationRequest) returns (RecommendationResponse);

  // Server streaming: one request, stream of responses
  rpc StreamRecommendations(RecommendationRequest) returns (stream RecommendationItem);

  // Client streaming: stream of requests, one response
  rpc BatchIngest(stream UserEvent) returns (IngestSummary);

  // Bidirectional streaming: both sides stream
  rpc RealTimePersonalization(stream UserAction) returns (stream RecommendationItem);
}

message RecommendationRequest {
  string user_id = 1;
  int32 count = 2;
  repeated string exclude_ids = 3;
  map<string, string> context = 4;
}

message RecommendationItem {
  string item_id = 1;
  float score = 2;
  string reason = 3;
}
```

**Protobuf vs JSON size comparison**:
```
JSON (human-readable, text):
{
  "user_id": "u_12345",
  "items": [
    {"item_id": "p_001", "score": 0.95, "reason": "collaborative_filtering"},
    {"item_id": "p_042", "score": 0.89, "reason": "content_based"}
  ]
}
Size: ~220 bytes

Protobuf (binary, schema-driven):
[binary encoding of the same data]
Size: ~65 bytes  (70% smaller)

Serialization speed:
  JSON:     ~500 ns to serialize, ~800 ns to deserialize
  Protobuf: ~100 ns to serialize, ~150 ns to deserialize
  (5-6x faster)
```

### 4.3 The Four gRPC Patterns

```
1. UNARY RPC (most common):
   Client ──[Request]──> Server
   Client <──[Response]── Server

   Use: point lookups, single predictions, CRUD operations

2. SERVER STREAMING:
   Client ──[Request]──────────────> Server
   Client <──[Response 1]─────────── Server
   Client <──[Response 2]─────────── Server
   Client <──[Response N]─────────── Server

   Use: large result sets, real-time feeds, model inference with token streaming

3. CLIENT STREAMING:
   Client ──[Request 1]──────────> Server
   Client ──[Request 2]──────────> Server
   Client ──[Request N]──────────> Server
   Client <──[Summary Response]─── Server

   Use: batch uploads, log ingestion, telemetry collection

4. BIDIRECTIONAL STREAMING:
   Client ──[Req 1]──> Server
   Client <──[Res 1]── Server
   Client ──[Req 2]──> Server
   Client ──[Req 3]──> Server
   Client <──[Res 2]── Server
   Client <──[Res 3]── Server

   Use: chat, real-time collaboration, interactive ML (send features, get predictions)
```

### 4.4 Why ML Serving Uses gRPC

gRPC dominates ML model serving (TensorFlow Serving, Triton Inference Server, TorchServe, vLLM) for specific technical reasons:

```
ML inference request/response characteristics:
  - Request:  dense float tensors (embeddings: 768-4096 floats)
  - Response: probability distributions, logits, generated tokens
  - Volume:   1,000-100,000 inferences/second
  - Latency:  p50 < 10ms for feature lookups, < 100ms for model inference

Why gRPC wins:
  1. Binary serialization: a 768-dim float32 embedding is 3,072 bytes in protobuf
                           vs ~6,000 bytes in JSON (each float becomes a string)
  2. Streaming:           token-by-token LLM generation maps to server streaming
  3. Code generation:     type-safe tensor shapes catch errors at compile time
  4. HTTP/2 multiplexing: one connection handles thousands of concurrent inferences
  5. Deadlines:           built-in deadline propagation across service chains
  6. Load balancing:      works with service mesh (Envoy, Istio) at L7
```

### 4.5 gRPC Deadlines and Cancellation

gRPC has built-in deadline propagation -- the most underappreciated feature for distributed systems:

```
Deadline propagation across a service chain:

Client sets deadline: 500ms
  │
  ├──> Service A (receives remaining: 500ms)
  │      │ spends 100ms
  │      ├──> Service B (receives remaining: 400ms)
  │      │      │ spends 200ms
  │      │      ├──> Service C (receives remaining: 200ms)
  │      │      │      │ spends 250ms → DEADLINE_EXCEEDED
  │      │      │      │ C is cancelled immediately
  │      │      │ B receives cancellation, stops work
  │      │ A receives cancellation, stops work
  │
  Client receives DEADLINE_EXCEEDED error

Without deadline propagation (REST):
  Client timeout: 500ms
  A has its own timeout: 2000ms
  B has its own timeout: 5000ms
  C has its own timeout: 10000ms
  
  Client gives up at 500ms, but A→B→C keep working,
  consuming resources for a response nobody will read.
```

**Design rule**: in a gRPC service chain, always propagate the incoming deadline minus a small buffer for your own processing. Never set a deadline longer than the incoming one.

### 4.6 gRPC Load Balancing

gRPC over HTTP/2 uses long-lived connections. This breaks simple L4 (TCP-level) load balancing because new requests multiplex over existing connections and never trigger new connection establishment.

```
L4 load balancing (BROKEN for gRPC):

Client ──[TCP conn]──> Load Balancer ──[TCP conn]──> Server A
                                                      (all requests go here)
                                       Server B idle
                                       Server C idle

The LB assigned the TCP connection to Server A.
All subsequent gRPC calls multiplex over that one connection.
Servers B and C receive zero traffic.

L7 load balancing (CORRECT for gRPC):

Client ──[TCP conn]──> L7 Proxy (Envoy) ──[HTTP/2 stream 1]──> Server A
                                          ──[HTTP/2 stream 3]──> Server B
                                          ──[HTTP/2 stream 5]──> Server C

The proxy inspects each HTTP/2 stream (gRPC call) and routes
it independently. Even round-robin works correctly.

Client-side load balancing (ALSO CORRECT):

Client has list of servers [A, B, C] from service discovery.
For each RPC, picks a server using round-robin, least-connections,
or weighted random. Maintains HTTP/2 connections to all servers.
No proxy in the path → lower latency.
```

**Interview answer**: "gRPC requires L7 load balancing because HTTP/2 multiplexing defeats L4 balancers. We would use Envoy as an L7 proxy, or client-side balancing with `grpc-go`'s built-in round-robin resolver backed by service discovery."

### 4.7 gRPC vs REST: Decision Matrix

| Factor | gRPC | REST (HTTP+JSON) |
|---|---|---|
| Performance | 5-10x faster serialization, smaller payloads | Adequate for most use cases |
| Contract | Strict `.proto` schema, code generation | OpenAPI spec (optional, often drifts) |
| Streaming | Native (4 patterns) | Not native (need WebSocket or SSE) |
| Browser support | Requires grpc-web proxy | Native |
| Debugging | Binary, needs `grpcurl` or tooling | `curl`, browser dev tools |
| Ecosystem | Strong in Go, Java, C++, Python | Universal |
| Versioning | Field numbers, backward compatible by design | URL versioning, content negotiation |
| Human readability | None (binary) | High (JSON) |

**Rule of thumb**: use gRPC for service-to-service communication (especially ML serving). Use REST for public-facing APIs and browser clients.

---

## 5. REST Design Principles

### 5.1 REST Is a Constraint Set, Not a Protocol

REST (Representational State Transfer) is an architectural style defined by six constraints. Most "REST APIs" violate most of them. What people call REST is usually "JSON over HTTP with resource-oriented URLs."

The constraints that actually matter for system design:

**1. Statelessness**: every request contains all information needed to process it. No server-side session state between requests. This is what makes REST services horizontally scalable -- any server can handle any request.

**2. Uniform interface**: resources are identified by URIs, manipulated through representations (JSON), and self-descriptive (Content-Type, Cache-Control headers).

**3. Cacheability**: responses must declare themselves cacheable or non-cacheable. GET responses with proper `Cache-Control` and `ETag` headers can be cached at every layer (browser, CDN, reverse proxy).

### 5.2 Idempotency

Idempotency is the most important property of REST methods for distributed systems. An operation is idempotent if performing it multiple times produces the same result as performing it once.

```
HTTP Method Idempotency:

GET    /users/123        → Idempotent, Safe (no side effects)
HEAD   /users/123        → Idempotent, Safe
PUT    /users/123        → Idempotent (replaces entire resource)
DELETE /users/123        → Idempotent (deleting twice = same as once)
PATCH  /users/123        → NOT idempotent (increment counter twice ≠ once)
POST   /users            → NOT idempotent (creates new resource each time)
```

**Why idempotency matters**: in a distributed system, network failures cause ambiguity. Did the server receive my request? If I retry, will it create a duplicate?

```
Idempotency key pattern for non-idempotent operations:

POST /payments
{
  "idempotency_key": "pay_2024_03_15_user123_order456",
  "amount": 99.99,
  "currency": "USD"
}

Server behavior:
  1. Check if idempotency_key exists in store
  2. If yes → return cached response (no duplicate charge)
  3. If no  → process payment, store result keyed by idempotency_key
  4. Set TTL on idempotency key (24-72 hours)

Implementation:
  - Store: Redis with TTL, or a dedicated idempotency table
  - Key scope: per-client or per-user (not global)
  - Race condition: use SETNX (set-if-not-exists) to handle concurrent retries
```

### 5.3 Versioning Strategies

```
1. URL versioning (most common):
   GET /v1/users/123
   GET /v2/users/123
   
   Pros: explicit, easy to route, easy to deprecate
   Cons: URL proliferation, clients must update endpoints

2. Header versioning:
   GET /users/123
   Accept: application/vnd.myapi.v2+json
   
   Pros: clean URLs, content negotiation
   Cons: harder to test (need custom headers), less visible

3. Query parameter versioning:
   GET /users/123?version=2
   
   Pros: easy to add
   Cons: optional parameter, easy to forget

Recommendation for system design interviews: URL versioning.
It is the most explicit, easiest to route at the load balancer/API gateway,
and what Google, Stripe, and AWS use.
```

### 5.4 Pagination Patterns

```
1. OFFSET-BASED (simple, but problematic at scale):
   GET /users?offset=100&limit=25
   
   Problem: offset=1000000 still scans 1M rows in most databases
   Problem: if a record is inserted while paginating, you get duplicates or gaps

2. CURSOR-BASED (recommended):
   GET /users?cursor=eyJ1c2VyX2lkIjoiMTIzIn0&limit=25
   
   Response:
   {
     "data": [...],
     "next_cursor": "eyJ1c2VyX2lkIjoiMTQ4In0",
     "has_more": true
   }
   
   The cursor encodes the last seen sort key (e.g., base64 of {"user_id": "148"})
   Database query: WHERE user_id > '148' ORDER BY user_id LIMIT 25
   
   Pros: consistent performance regardless of page depth, stable under inserts
   Cons: cannot jump to arbitrary page, cursor is opaque

3. KEYSET PAGINATION (cursor-based, but the key is visible):
   GET /users?after_id=148&limit=25
   
   Same as cursor-based but the key is explicit.
   Used by: GitHub API, Stripe API
```

### 5.5 HATEOAS

HATEOAS (Hypermedia as the Engine of Application State) means responses include links to related actions and resources:

```json
{
  "id": "order_789",
  "status": "pending",
  "total": 99.99,
  "_links": {
    "self":    {"href": "/orders/order_789"},
    "cancel":  {"href": "/orders/order_789/cancel", "method": "POST"},
    "payment": {"href": "/payments?order_id=order_789"},
    "items":   {"href": "/orders/order_789/items"}
  }
}
```

**Interview reality**: almost nobody implements full HATEOAS. It is worth mentioning to show you know the theory, but in practice API clients are coded against known endpoints, not discovered dynamically. The exception is pagination links (`next`, `prev`), which are universally useful.

### 5.6 Rate Limiting and API Design

```
Standard rate limiting headers:

HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit:     100        (max requests per window)
X-RateLimit-Remaining: 0          (requests left in window)
X-RateLimit-Reset:     1710532800 (Unix timestamp when window resets)
Retry-After:           30         (seconds to wait before retrying)

Algorithms:
1. Fixed window:    100 req/min, counter resets at :00
   Problem: 200 requests possible in 1 second (100 at :59, 100 at :00)

2. Sliding window log: track timestamp of each request, count in window
   Accurate but memory-intensive (store every timestamp)

3. Sliding window counter: combine fixed windows with interpolation
   Good accuracy, low memory

4. Token bucket: tokens added at fixed rate, consumed per request
   Allows bursts up to bucket capacity
   Used by: AWS API Gateway, Stripe

5. Leaky bucket: requests enter a FIFO queue, processed at fixed rate
   Smooths bursts, constant output rate
```

---

## 6. WebSocket

### 6.1 What WebSocket Solves

HTTP is request-response: the client always initiates. WebSocket upgrades an HTTP connection to a full-duplex, bidirectional channel where either side can send messages at any time.

```
HTTP polling vs WebSocket:

HTTP Polling (wasteful):
  Client: GET /messages?since=100  → Server: [] (empty)     (wasted)
  Client: GET /messages?since=100  → Server: [] (empty)     (wasted)
  Client: GET /messages?since=100  → Server: [msg101]       (useful)
  Client: GET /messages?since=101  → Server: [] (empty)     (wasted)
  
  90% of requests return empty. Each costs a full HTTP round trip.

Long Polling (better, still awkward):
  Client: GET /messages?since=100  → Server holds connection open...
                                     ...30 seconds later: [msg101]
  Client: GET /messages?since=101  → Server holds connection open...
  
  Better utilization but: connection timeouts, proxy issues,
  awkward error handling, one-directional.

WebSocket (correct solution):
  Client: GET /ws  (HTTP Upgrade)
  Server: 101 Switching Protocols
  
  ──── Full-duplex channel established ────
  
  Server → Client: {"type": "message", "id": 101, "text": "hello"}
  Client → Server: {"type": "typing", "user": "alice"}
  Server → Client: {"type": "presence", "user": "bob", "status": "online"}
  
  Either side sends at any time. No polling. Sub-millisecond latency.
```

### 6.2 The WebSocket Handshake

```
Client request:
  GET /ws HTTP/1.1
  Host: chat.example.com
  Upgrade: websocket
  Connection: Upgrade
  Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==
  Sec-WebSocket-Version: 13

Server response:
  HTTP/1.1 101 Switching Protocols
  Upgrade: websocket
  Connection: Upgrade
  Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=

After 101, the TCP connection is no longer HTTP.
Both sides speak the WebSocket frame protocol.
```

### 6.3 Scaling WebSocket

WebSocket connections are long-lived and stateful, which creates specific scaling challenges:

```
The sticky session problem:

User Alice connects to Server A via WebSocket.
User Bob connects to Server B via WebSocket.
Alice sends a message to Bob.

Server A has Alice's connection but not Bob's.
How does Alice's message reach Bob?

Solution 1: Pub/Sub backplane (Redis, Kafka, NATS)

  ┌──────────┐         ┌──────────────┐         ┌──────────┐
  │ Server A │ ──pub── │ Redis Pub/Sub │ ──sub── │ Server B │
  │ (Alice)  │         │              │         │ (Bob)    │
  └──────────┘         └──────────────┘         └──────────┘

  1. Server A receives message from Alice
  2. Server A publishes to Redis channel "user:bob"
  3. Server B is subscribed to "user:bob", receives message
  4. Server B sends message to Bob over WebSocket

Solution 2: Consistent hashing for connection routing

  All connections for a user go to the same server.
  Load balancer routes by user_id hash.
  Problem: rebalancing on server failure.

Solution 3: Dedicated connection gateway (Socket.IO, Centrifugo)

  Stateless app servers + dedicated WebSocket gateway layer.
  Gateway handles connections, app servers handle business logic.
```

### 6.4 Heartbeats and Dead Connection Detection

```
WebSocket ping/pong (protocol-level):
  
  Server sends PING frame every 30 seconds
  Client responds with PONG frame
  
  If no PONG received within 10 seconds:
    → Close connection, clean up resources
  
  If client detects no PING for 60 seconds:
    → Assume server is dead, reconnect

Application-level heartbeat (more common in practice):
  
  Client sends: {"type": "ping", "ts": 1710532800}
  Server sends: {"type": "pong", "ts": 1710532800}
  
  Advantages over protocol-level ping:
  - Works through intermediaries that may not forward WebSocket pings
  - Can include application data (last seen event ID for gap detection)
  - Measurable latency (compare timestamps)
```

### 6.5 When to Use WebSocket vs Alternatives

| Scenario | Protocol | Why |
|---|---|---|
| Chat, messaging | WebSocket | Full-duplex, low latency, bidirectional |
| Live dashboards | SSE (Server-Sent Events) | One-directional (server→client), simpler |
| Notifications | SSE or WebSocket | SSE if one-way is sufficient |
| Collaborative editing | WebSocket | Bidirectional, low latency, high frequency |
| Live sports scores | SSE | One-directional, auto-reconnect built in |
| Multiplayer game state | WebSocket or raw UDP | WebSocket for web, UDP for native clients |
| Stock tickers | WebSocket | High frequency, bidirectional (subscribe/unsubscribe) |

**SSE (Server-Sent Events)**: a simpler alternative to WebSocket for server-to-client streaming. Uses a regular HTTP connection with `Content-Type: text/event-stream`. Built-in reconnection with `Last-Event-ID`. Works through HTTP proxies without special configuration. Choose SSE when the server pushes updates and the client only needs to subscribe.

---

## 7. GraphQL

### 7.1 What GraphQL Solves

GraphQL lets the client specify exactly which fields it needs, solving the over-fetching and under-fetching problems of REST:

```
REST over-fetching:
  GET /users/123
  Returns: id, name, email, address, phone, avatar_url, bio,
           created_at, updated_at, preferences, settings...
  
  Client only needed: name, avatar_url
  Wasted bandwidth: ~80% of the response

REST under-fetching (the N+1 problem):
  GET /users/123                    → {name: "Alice", ...}
  GET /users/123/posts              → [{id: 1}, {id: 2}, ...]
  GET /posts/1/comments             → [...]
  GET /posts/2/comments             → [...]
  GET /posts/1/comments/5/author    → {...}
  ...
  
  One page load = 10-20 HTTP requests in a waterfall

GraphQL single request:
  POST /graphql
  {
    user(id: "123") {
      name
      avatarUrl
      posts(first: 5) {
        title
        comments(first: 3) {
          text
          author { name }
        }
      }
    }
  }
  
  One request. Exactly the data needed. No over-fetching.
```

### 7.2 The N+1 Problem in GraphQL

GraphQL moves the N+1 problem from the client to the server. Naive resolver implementations execute one database query per field:

```
Query:
  { users(first: 10) { name, department { name } } }

Naive execution:
  1. SELECT * FROM users LIMIT 10                     (1 query)
  2. SELECT * FROM departments WHERE id = 1           (user 1)
  3. SELECT * FROM departments WHERE id = 2           (user 2)
  ...
  11. SELECT * FROM departments WHERE id = 10         (user 10)
  
  Total: 11 queries (1 + N)

With DataLoader (batching + caching):
  1. SELECT * FROM users LIMIT 10                     (1 query)
  2. SELECT * FROM departments WHERE id IN (1,2,3,4,5,6,7,8,9,10)  (1 query, batched)
  
  Total: 2 queries

DataLoader pattern:
  - Collects all IDs requested in one tick of the event loop
  - Batches them into a single query
  - Caches results for the duration of the request
  - Available in every language: dataloader (JS), aiodataloader (Python),
    gqlgen dataloaders (Go)
```

### 7.3 When GraphQL Makes Sense

| Good fit | Poor fit |
|---|---|
| Multiple client types (web, mobile, TV) needing different data shapes | Simple CRUD with uniform data needs |
| Deeply nested, graph-shaped data (social network, org chart) | High-throughput internal service-to-service (use gRPC) |
| Rapid frontend iteration without backend changes | Real-time streaming (WebSocket/gRPC streaming is simpler) |
| API gateway aggregating multiple backend services | File uploads (REST/multipart is more natural) |
| Public API where clients are unknown | Simple APIs with few resources |

### 7.4 GraphQL Security Considerations

```
1. Query depth limiting:
   Prevent: { user { friends { friends { friends { ... } } } } }
   → Set max depth (e.g., 10 levels)

2. Query complexity analysis:
   Assign cost to each field. Reject queries above threshold.
   name: cost 1, posts: cost 5, posts.comments: cost 10
   Total cost for a query must be < 1000

3. Rate limiting by query cost:
   Instead of "100 requests/minute", use "10,000 cost units/minute"
   A simple query costs 5, a complex one costs 500

4. Persisted queries:
   Client sends a hash, server looks up the pre-approved query
   Prevents arbitrary query injection
   Used by: GitHub GraphQL API, Shopify
```

---

## 8. DNS Resolution

### 8.1 How DNS Resolution Works

DNS is the first network call in almost every distributed system interaction. Understanding its resolution chain, caching behavior, and failure modes is essential.

```
DNS resolution chain:

Application calls getaddrinfo("api.example.com")
  │
  ▼
┌──────────────────────────────┐
│ 1. Application/Library Cache │  (glibc nscd, Go net.Resolver cache)
│    TTL: varies               │  Hit? → return immediately
└──────────────┬───────────────┘
               │ miss
               ▼
┌──────────────────────────────┐
│ 2. OS Stub Resolver          │  (/etc/resolv.conf → nameserver)
│    + systemd-resolved cache  │  Hit? → return from local cache
└──────────────┬───────────────┘
               │ miss
               ▼
┌──────────────────────────────┐
│ 3. Recursive Resolver        │  (ISP, corporate, 8.8.8.8, 1.1.1.1)
│    Has its own cache (large) │  Hit? → return cached, respecting TTL
└──────────────┬───────────────┘
               │ miss
               ▼
┌──────────────────────────────┐
│ 4. Root DNS Servers          │  → "Ask .com TLD servers"
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ 5. TLD DNS Servers (.com)    │  → "Ask ns1.example.com"
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│ 6. Authoritative DNS Server  │  → "api.example.com = 93.184.216.34"
│    (ns1.example.com)         │     TTL: 300 seconds
└──────────────────────────────┘
```

### 8.2 TTL and Caching Behavior

```
DNS record with TTL:

  api.example.com.  300  IN  A  93.184.216.34
                     │
                     └── TTL = 300 seconds (5 minutes)

What TTL means in practice:
  - Resolver caches the record for up to 300 seconds
  - After TTL expires, next query triggers a fresh lookup
  - Clients may cache beyond TTL (Java's InetAddress caches forever by default!)

TTL tradeoffs:
  Low TTL (30-60s):
    + Fast failover (traffic shifts within seconds)
    + Quick blue/green deployment cutover
    - More DNS queries, higher latency for cold lookups
    - More load on authoritative DNS servers

  High TTL (3600s+):
    + Fewer DNS queries, better performance
    + Resilient to DNS server outages (cached records survive)
    - Slow failover (stale records served for up to an hour)
    - Cannot do rapid traffic shifting

  Production recommendation:
    - Internal services: 30-60s TTL (fast failover matters)
    - External APIs:     300s TTL (balance between freshness and performance)
    - Static assets CDN: 3600s+ TTL (rarely change)
```

### 8.3 DNS-Based Load Balancing and Failover

```
Round-robin DNS:
  api.example.com.  60  IN  A  10.0.1.1
  api.example.com.  60  IN  A  10.0.1.2
  api.example.com.  60  IN  A  10.0.1.3

  Resolver returns all IPs, client picks one (usually first).
  Different clients get different orderings → rough load distribution.
  
  Problem: no health checking. If 10.0.1.2 is dead, 1/3 of traffic fails.

Weighted DNS (Route 53, Cloudflare):
  api.example.com.  60  IN  A  10.0.1.1  weight=70
  api.example.com.  60  IN  A  10.0.1.2  weight=30

  70% of resolutions return 10.0.1.1, 30% return 10.0.1.2.
  Useful for canary deployments and gradual traffic shifting.

GeoDNS / Latency-based routing:
  api.example.com → 10.0.1.1  (resolver in US → US datacenter)
  api.example.com → 10.0.2.1  (resolver in EU → EU datacenter)

  Routes users to nearest datacenter based on resolver location.
  Used by every major CDN and global service.

Health-checked DNS failover:
  Primary:   api.example.com → 10.0.1.1  (health check every 10s)
  Secondary: api.example.com → 10.0.2.1  (failover)
  
  If primary health check fails → remove its record.
  Failover time = health check interval + DNS TTL propagation.
  With TTL=60s: worst case ~70 seconds to failover.
```

### 8.4 DNS Pitfalls in Distributed Systems

```
1. Java DNS caching (the classic trap):
   Default: InetAddress caches DNS results FOREVER (TTL = -1)
   Fix: -Dsun.net.inetaddr.ttl=30 (cache for 30 seconds)
   Or: networkaddress.cache.ttl=30 in java.security

2. Connection pool caching stale IPs:
   HTTP client creates connection pool to api.example.com
   DNS resolves to 10.0.1.1, pool creates 100 connections
   DNS changes to 10.0.2.1 (deployment, failover)
   Connection pool still sends traffic to 10.0.1.1 (stale connections!)
   
   Fix: set max connection lifetime in pool (e.g., 5 minutes)
   Fix: periodic re-resolution in the pool (Go's net.Resolver does this)

3. Kubernetes DNS resolution at scale:
   Pod DNS queries go to CoreDNS (cluster service)
   High QPS services can overwhelm CoreDNS
   
   Fix: NodeLocal DNSCache (DaemonSet that caches on each node)
   Fix: Use headless services with client-side resolution
   Fix: Set ndots:1 in pod DNS config (reduces search domain expansion)

4. /etc/resolv.conf search domains:
   search default.svc.cluster.local svc.cluster.local cluster.local
   
   Lookup for "api.example.com" tries:
     api.example.com.default.svc.cluster.local  (fail)
     api.example.com.svc.cluster.local           (fail)
     api.example.com.cluster.local                (fail)
     api.example.com.                             (success)
   
   4 DNS queries instead of 1. Fix: use FQDN with trailing dot:
     "api.example.com." → 1 query
```

---

## 9. TLS and mTLS

### 9.1 TLS 1.3 Handshake

TLS 1.3 simplified the handshake from 2 RTT (TLS 1.2) to 1 RTT, and supports 0-RTT resumption:

```
TLS 1.3 full handshake (1 RTT):

CLIENT                                          SERVER
  |                                                |
  |  ClientHello                                   |
  |    + supported_versions (TLS 1.3)              |
  |    + key_share (ECDHE public key)              |
  |    + signature_algorithms                      |
  |  ──────────────────────────────────────────>    |
  |                                                |
  |                          ServerHello           |
  |                            + key_share         |
  |                          EncryptedExtensions   |
  |                          Certificate           |
  |                          CertificateVerify     |
  |                          Finished              |
  |  <──────────────────────────────────────────   |
  |                                                |
  |  Finished                                      |
  |  [Application Data]                            |
  |  ──────────────────────────────────────────>    |
  |                                                |
  |  1 RTT total. Application data can flow        |
  |  immediately after client Finished.            |

TLS 1.3 0-RTT resumption:

CLIENT                                          SERVER
  |                                                |
  |  ClientHello                                   |
  |    + pre_shared_key (from previous session)    |
  |    + early_data_indication                     |
  |  [0-RTT Application Data]  ← sent immediately |
  |  ──────────────────────────────────────────>    |
  |                                                |
  |  0 RTT. Data sent in the first packet.         |
  |  Risk: 0-RTT data is replayable.              |
  |  Only safe for idempotent requests.            |
```

**0-RTT replay risk**: an attacker can capture and replay the 0-RTT data. This is safe for GET requests but dangerous for POST requests (could replay a payment). gRPC does not use 0-RTT by default for this reason.

### 9.2 mTLS (Mutual TLS)

In standard TLS, only the server presents a certificate. In mTLS, both sides authenticate:

```
Standard TLS:
  Client verifies server's certificate → "I'm talking to the real server"
  Server does not verify client        → "I don't know who the client is"

mTLS:
  Client verifies server's certificate → "I'm talking to the real server"
  Server verifies client's certificate → "I know this is an authorized client"

mTLS in a service mesh:

┌──────────┐  mTLS   ┌──────────┐  mTLS   ┌──────────┐
│ Service A │ ←────→ │ Service B │ ←────→ │ Service C │
│ (cert: A) │        │ (cert: B) │        │ (cert: C) │
└──────────┘         └──────────┘         └──────────┘

Each service has its own X.509 certificate.
Each connection verifies both sides.
No service can impersonate another.

Use cases:
  - Zero-trust networking (SPIFFE/SPIRE)
  - Kubernetes service mesh (Istio, Linkerd)
  - API authentication (Stripe, Plaid)
  - Database connections (PostgreSQL ssl_cert)
```

### 9.3 Certificate Rotation

Certificates expire. Rotating them without downtime is a critical operational concern:

```
Certificate rotation without downtime:

Timeline:
  Day 0:   Issue cert A (expires Day 90)
  Day 60:  Issue cert B (expires Day 150), start serving both
  Day 61:  Clients that cached cert A fingerprint are still fine
  Day 75:  All clients have seen cert B
  Day 90:  Cert A expires, remove it. Cert B is sole cert.
  Day 120: Issue cert C, repeat cycle.

Overlap period (Day 60-90) is critical:
  - Server presents cert B
  - Clients that pinned cert A continue working (if you pin the CA, not leaf)
  - No client experiences a TLS error

Automated rotation (the modern approach):
  - SPIFFE/SPIRE: rotates SVIDs (SPIFFE Verifiable Identity Documents) every hour
  - cert-manager (Kubernetes): automates Let's Encrypt certificate lifecycle
  - AWS ACM: fully managed certificate rotation for ALB/NLB/CloudFront
  - Vault PKI: issues short-lived certificates (1-24 hours) on demand

Short-lived certificates (best practice):
  - Validity: 1-24 hours
  - Rotation: automated, every validity period
  - Revocation: unnecessary (cert expires before revocation propagates)
  - Used by: SPIFFE/SPIRE, Google BeyondCorp, Netflix
```

### 9.4 Certificate Pinning

Certificate pinning constrains which certificates a client accepts, preventing man-in-the-middle attacks even if a CA is compromised:

```
Pinning levels:

1. Leaf certificate pinning:
   Client only accepts the exact server certificate.
   Problem: rotation requires client update.
   Used by: mobile apps (but falling out of favor).

2. Public key pinning (HPKP):
   Client pins the server's public key (survives cert renewal if key stays same).
   Problem: key compromise requires client update.
   Deprecated in browsers (too easy to brick a domain).

3. CA pinning (recommended):
   Client only accepts certificates signed by a specific CA.
   Rotation-friendly: any cert from that CA is accepted.
   Used by: internal services (pin your internal CA).

4. SPIFFE trust bundle:
   Clients trust a SPIFFE trust bundle (set of CA certificates).
   Trust bundle is rotated independently of leaf certificates.
   Most flexible for microservice environments.
```

---

## 10. Connection Pooling

### 10.1 Why Connection Pooling Exists

Opening a new connection for every request is prohibitively expensive:

```
Cost of a new connection:

TCP:  1 RTT (handshake)                    ~1ms datacenter, ~30ms cross-region
TLS:  1 RTT (TLS 1.3) or 2 RTT (TLS 1.2) ~1-2ms datacenter, ~30-60ms cross-region
Auth: 1 RTT (database auth, token exchange) ~1-5ms
TCP slow start: 7+ RTTs to reach full throughput

Total for first useful byte: 3-5 RTTs = 3-15ms datacenter, 100-250ms cross-region

vs. reusing a pooled connection: 0 RTTs, immediate data transfer

At 10,000 requests/second with 2ms connection overhead:
  New connections: 10,000 × 2ms = 20 seconds of CPU time per second (impossible)
  Pooled:          10,000 × 0ms = negligible overhead
```

### 10.2 HTTP Connection Pooling

```
HTTP/1.1 connection pool:

┌─────────────────────────────────────────────────┐
│ HTTP Client (e.g., Go http.Client)              │
│                                                  │
│  Pool for api.example.com:443                    │
│  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐     │
│  │conn1│ │conn2│ │conn3│ │conn4│ │conn5│      │
│  │busy │ │idle │ │busy │ │idle │ │busy │      │
│  └─────┘ └─────┘ └─────┘ └─────┘ └─────┘     │
│                                                  │
│  Pool for db.internal:5432                       │
│  ┌─────┐ ┌─────┐ ┌─────┐                       │
│  │conn1│ │conn2│ │conn3│                        │
│  │busy │ │busy │ │idle │                        │
│  └─────┘ └─────┘ └─────┘                       │
└─────────────────────────────────────────────────┘

Key parameters:
  MaxIdleConns:        maximum idle connections across all hosts
  MaxIdleConnsPerHost: maximum idle connections per host (default 2 in Go!)
  MaxConnsPerHost:     maximum total connections per host
  IdleConnTimeout:     close idle connections after this duration

Common misconfiguration:
  Go's default MaxIdleConnsPerHost = 2
  If your service calls one backend at 1000 RPS:
    → only 2 connections reused, 998 create new connections per second
    → TIME_WAIT socket exhaustion within minutes

Fix: set MaxIdleConnsPerHost = 100 (or match your concurrency)
```

### 10.3 Database Connection Pooling

```
Database connection pool (PgBouncer, HikariCP, SQLAlchemy pool):

Application Layer:
  ┌──────────────────────────────────────────────┐
  │ 100 concurrent requests                      │
  │ (goroutines / threads / async tasks)         │
  └─────────────────────┬────────────────────────┘
                        │ acquire connection
                        ▼
  ┌──────────────────────────────────────────────┐
  │ Connection Pool (max_size=20)                │
  │                                              │
  │ Active connections: 18/20                    │
  │ Waiting queue:      3 requests               │
  │                                              │
  │ ┌────┐┌────┐┌────┐...┌────┐                 │
  │ │ c1 ││ c2 ││ c3 │   │c20 │                │
  │ └────┘└────┘└────┘   └────┘                │
  └─────────────────────┬────────────────────────┘
                        │ 20 persistent TCP connections
                        ▼
  ┌──────────────────────────────────────────────┐
  │ PostgreSQL (max_connections=100)             │
  │ Each connection: ~5-10 MB RAM                │
  │ 100 connections: 500 MB - 1 GB RAM           │
  └──────────────────────────────────────────────┘

Sizing formula:
  Pool size = number of CPU cores × 2 + number of disk spindles
  (For SSDs: pool size ≈ CPU cores × 2)
  
  Counter-intuitive: a pool of 10 often outperforms a pool of 100.
  More connections = more lock contention, more context switching,
  more memory pressure on the database.

  HikariCP recommendation: pool_size = (core_count * 2) + effective_spindle_count
  For a 4-core server with SSD: pool_size = (4 * 2) + 1 = 9

PgBouncer modes:
  Session:     one client = one server connection for session duration (safest)
  Transaction: connection returned to pool after each transaction (most efficient)
  Statement:   connection returned after each statement (breaks multi-statement txns)
```

### 10.4 gRPC Channel Pooling

gRPC uses a "channel" abstraction that manages a pool of HTTP/2 connections:

```
gRPC channel architecture:

┌──────────────────────────────────────┐
│ gRPC Channel (to api.example.com)   │
│                                      │
│  ┌──────────────────────────────┐   │
│  │ Name Resolver                 │   │  Resolves DNS → [10.0.1.1, 10.0.1.2]
│  └──────────────┬───────────────┘   │
│                 ▼                    │
│  ┌──────────────────────────────┐   │
│  │ Load Balancer (pick_first,   │   │  Selects backend for each RPC
│  │  round_robin, custom)        │   │
│  └──────────────┬───────────────┘   │
│                 ▼                    │
│  ┌────────┐ ┌────────┐             │
│  │SubConn1│ │SubConn2│             │  One HTTP/2 connection per backend
│  │10.0.1.1│ │10.0.1.2│             │  Each multiplexes thousands of RPCs
│  └────────┘ └────────┘             │
└──────────────────────────────────────┘

Best practices:
  - Create ONE channel per target service, share across all goroutines/threads
  - Do NOT create a new channel per RPC (defeats connection reuse)
  - Set keepalive parameters to detect dead connections
  - Use round_robin LB policy for backend with multiple replicas
  - Set max concurrent streams per connection (default: 100 in most impls)
```

### 10.5 Connection Pool Failure Modes

```
1. Pool exhaustion:
   All connections in use, new requests queue (then timeout).
   Symptom: latency spike → timeout cascade → service outage.
   Fix: set acquire timeout, monitor pool utilization, right-size pool.

2. Connection leak:
   Code path acquires connection but does not release it (missing defer/finally).
   Pool slowly drains until exhaustion.
   Fix: connection lifetime limits, leak detection (log connections held > 30s).

3. Stale connections:
   Pooled connection's peer has closed (server restart, network partition).
   Next request on stale connection fails.
   Fix: health check on acquire (test-on-borrow), periodic validation,
        max connection lifetime (force rotation).

4. Thundering herd:
   All pool connections are stale simultaneously (database restart).
   All requests try to create new connections at once.
   Fix: connection creation rate limiting, exponential backoff with jitter.

5. DNS change blindness:
   Pool holds connections to old IP after DNS change.
   Fix: max connection lifetime shorter than DNS TTL.
```

---

## 11. Protocol Selection for ML/AI Systems

### 11.1 ML Inference Pipeline Protocol Map

```
End-to-end ML inference request:

┌─────────┐  HTTPS/   ┌──────────┐  gRPC     ┌──────────────┐  gRPC     ┌─────────────┐
│ Client  │  REST     │ API      │  (proto)  │ Feature      │  (proto)  │ Model       │
│ (Web/   │ ────────> │ Gateway  │ ────────> │ Store        │ ────────> │ Server      │
│  Mobile)│          │ (Kong/   │          │ (Feast/      │          │ (Triton/    │
│         │          │  Envoy)  │          │  custom)     │          │  vLLM)      │
└─────────┘          └──────────┘          └──────────────┘          └─────────────┘
      ↑                    │                                              │
      │               REST/JSON                                     gRPC binary
      │             (human-readable)                            (tensor payloads)
      │                    │
      │                    ▼
      │              ┌──────────┐
      └───────────── │ Response │  JSON (user-facing)
                     │ Assembly │
                     └──────────┘

Protocol choices and why:
  Client → Gateway:     REST/JSON (browser compatible, debuggable)
  Gateway → Feature:    gRPC (internal, high throughput, type safety)
  Feature → Model:      gRPC (tensor payloads, streaming for LLM tokens)
  Model → Gateway:      gRPC server streaming (token-by-token for LLM)
  Gateway → Client:     SSE or chunked HTTP (streaming tokens to browser)
```

### 11.2 LLM Token Streaming

```
LLM inference requires streaming tokens as they are generated:

Option 1: gRPC server streaming (internal)
  service LLMService {
    rpc Generate(GenerateRequest) returns (stream GenerateToken);
  }
  
  Latency to first token (TTFT): ~200ms
  Inter-token latency (ITL):     ~30ms
  Total for 500 tokens:          200ms + (500 × 30ms) = ~15.2s
  
  With streaming: user sees first token at 200ms
  Without streaming: user waits 15.2s for complete response

Option 2: Server-Sent Events (client-facing)
  GET /v1/chat/completions (OpenAI-compatible)
  Accept: text/event-stream
  
  data: {"choices": [{"delta": {"content": "Hello"}}]}
  data: {"choices": [{"delta": {"content": " world"}}]}
  data: [DONE]

Option 3: WebSocket (bidirectional, for chat UIs)
  Client can send: cancel, regenerate, edit-and-resend
  Server streams: tokens, tool calls, status updates
```

### 11.3 Feature Store Communication

```
Feature store read path (latency-critical):

Online store requirements:
  - p99 latency: < 10ms
  - Throughput: 100K+ reads/second
  - Payload: feature vectors (10-1000 floats)

Protocol comparison for feature serving:

| Protocol | p50 latency | Payload size (100 floats) | Throughput |
|----------|-------------|---------------------------|------------|
| REST/JSON | 2-5ms | ~1,200 bytes | 10K RPS |
| gRPC/proto | 0.5-2ms | ~420 bytes | 50K+ RPS |
| Redis protocol | 0.1-0.5ms | ~800 bytes | 100K+ RPS |
| Custom binary (Arrow) | 0.3-1ms | ~400 bytes | 80K+ RPS |

Recommendation:
  - Feast online store: Redis protocol for raw speed
  - Custom feature service: gRPC for type safety and streaming batch
  - Client-facing feature API: REST gateway wrapping gRPC backend
```

---

## 12. Capacity Planning and Performance Math

### 12.1 Connection Math

```
Scenario: API gateway serving 50,000 requests/second

HTTP/1.1 (one request per connection at a time):
  Average request duration: 20ms
  Connections needed: 50,000 × 0.020 = 1,000 concurrent connections
  With connection reuse (keep-alive): ~1,000 pooled connections
  Without connection reuse: 50,000 new connections/second
    → 50,000 × 60s = 3,000,000 TIME_WAIT sockets per minute
    → System fails within seconds

HTTP/2 (multiplexed, ~100 streams per connection):
  Connections needed: 50,000 / 100 = 500 connections
  In practice: 10-50 connections (each handles 1,000-5,000 streams)
  Much better resource utilization

gRPC (HTTP/2 with long-lived channels):
  Connections: 1 per backend server (multiplexed)
  With 10 backend servers: 10 connections total
  Each handles 5,000 concurrent RPCs
```

### 12.2 Bandwidth Math

```
Payload size matters:

REST/JSON response for a user profile:
  {"id":"u123","name":"Alice Smith","email":"alice@example.com",...}
  ~500 bytes uncompressed, ~200 bytes gzipped

Same data in protobuf:
  ~120 bytes (no field names, varint encoding)

At 100,000 requests/second:
  JSON uncompressed: 100K × 500B = 50 MB/s = 400 Mbps
  JSON gzipped:      100K × 200B = 20 MB/s = 160 Mbps (+ CPU for compression)
  Protobuf:          100K × 120B = 12 MB/s = 96 Mbps (no compression needed)

For a 768-dim float32 embedding vector:
  JSON:     768 floats as strings ≈ 6,000 bytes
  Protobuf: 768 × 4 bytes = 3,072 bytes (packed repeated float)
  
  At 50,000 inferences/second:
    JSON:     300 MB/s = 2.4 Gbps (saturates a 10G NIC on a single service)
    Protobuf: 150 MB/s = 1.2 Gbps (half the bandwidth)
```

### 12.3 Latency Budget Breakdown

```
End-to-end latency budget for an ML inference request (p99 target: 200ms):

Component          |  p50  |  p99  | Protocol    | Notes
───────────────────┼───────┼───────┼─────────────┼──────────────────
DNS resolution     |  0ms  |  5ms  | UDP         | Cached 99% of time
TLS handshake      |  0ms  | 30ms  | TLS 1.3     | Reused 95% of time
API gateway route  |  1ms  |  5ms  | HTTP/2      | Envoy/Kong overhead
Auth (JWT verify)  |  1ms  |  3ms  | Local       | No network call
Feature fetch      |  2ms  | 10ms  | gRPC        | Redis online store
Model inference    | 15ms  | 80ms  | gRPC        | GPU inference
Post-processing    |  1ms  |  5ms  | Local       | Ranking, filtering
Response serialize |  0ms  |  2ms  | JSON/proto  | Client-facing
Network (client)   | 10ms  | 50ms  | HTTPS       | CDN edge → origin
───────────────────┼───────┼───────┼─────────────┼──────────────────
Total              | 30ms  |190ms  |             | Under 200ms budget
```

---

## 13. Failure Modes and Debugging

### 13.1 TCP/Network Failure Modes

```
1. Connection refused (ECONNREFUSED):
   Server is not listening on the port.
   Cause: service not running, wrong port, firewall.
   Debug: telnet host port, netstat -tlnp on server.

2. Connection timeout:
   SYN sent, no SYN-ACK received.
   Cause: firewall dropping packets (not rejecting), wrong IP, network partition.
   Debug: tcpdump on both sides, traceroute.

3. Connection reset (ECONNRESET / RST):
   Peer sent TCP RST.
   Cause: server crashed, load balancer closed idle connection,
          sending data to half-closed connection.
   Debug: check server logs, LB idle timeout settings.

4. Broken pipe (EPIPE / SIGPIPE):
   Writing to a connection the peer has closed.
   Cause: client closed before server finished responding.
   Debug: handle SIGPIPE, check client timeout settings.

5. SSL/TLS handshake failure:
   Certificate expired, hostname mismatch, protocol version mismatch,
   cipher suite mismatch, CA not trusted.
   Debug: openssl s_client -connect host:port, check certificate dates.

6. DNS resolution failure (NXDOMAIN, SERVFAIL):
   Domain does not exist or DNS server is unreachable.
   Cause: typo in hostname, DNS server down, split-horizon DNS misconfigured.
   Debug: dig +trace hostname, check /etc/resolv.conf.
```

### 13.2 HTTP/gRPC Failure Modes

```
7. HTTP 502 Bad Gateway:
   Proxy/LB cannot reach the upstream server.
   Cause: upstream crashed, upstream too slow (proxy timeout < upstream response time).
   Debug: check upstream health, increase proxy timeout, check upstream logs.

8. HTTP 503 Service Unavailable:
   Server is overloaded or in maintenance.
   Cause: all workers busy, circuit breaker open, deployment in progress.
   Debug: check server resource utilization, connection pool status.

9. HTTP 504 Gateway Timeout:
   Proxy/LB timed out waiting for upstream response.
   Cause: upstream slow (DB query, external API), undersized upstream pool.
   Debug: trace the request through the stack, find the slow component.

10. gRPC UNAVAILABLE (code 14):
    Equivalent to HTTP 503. Transient, safe to retry.
    Cause: server overloaded, connection lost, load balancer draining.

11. gRPC DEADLINE_EXCEEDED (code 4):
    Request did not complete within the deadline.
    Cause: upstream slow, deadline too tight, cascading slowness.
    Debug: check which service in the chain consumed the most time.

12. gRPC RESOURCE_EXHAUSTED (code 8):
    Rate limit hit, memory limit, max concurrent streams.
    Cause: client sending too fast, server under-provisioned.
    Debug: check rate limiter config, server resource metrics.
```

### 13.3 Debugging Tools

```
Network layer:
  tcpdump -i any -nn port 443                    # capture packets
  ss -tnp                                        # list TCP connections
  ss -s                                          # socket statistics (TIME_WAIT count)
  curl -v --trace-time https://api.example.com   # verbose HTTP with timing
  grpcurl -plaintext localhost:50051 list         # gRPC reflection

DNS:
  dig +trace example.com                         # full resolution chain
  dig @8.8.8.8 example.com                       # query specific resolver
  dig +short example.com                         # just the answer

TLS:
  openssl s_client -connect host:443             # TLS handshake details
  openssl x509 -in cert.pem -text -noout         # certificate details
  echo | openssl s_client -connect h:443 2>/dev/null | openssl x509 -dates  # expiry

HTTP/2:
  nghttp -v https://example.com                  # HTTP/2 frame-level debug
  curl --http2 -v https://example.com            # HTTP/2 with curl

gRPC:
  grpcurl -d '{"user_id": "123"}' \
    localhost:50051 ml.serving.RecommendationService/GetRecommendations
  
  grpc_health_probe -addr=localhost:50051         # health check
```

---

## 14. Interview Patterns

### 14.1 Pattern: "Why did you choose gRPC / REST / WebSocket here?"

**Framework for answering**:

```
1. Who is the client?
   Browser/mobile → REST (or GraphQL) + SSE/WebSocket for streaming
   Internal service → gRPC
   
2. What is the communication pattern?
   Request-response → REST or gRPC unary
   Server push → SSE, WebSocket, or gRPC server streaming
   Bidirectional → WebSocket or gRPC bidi streaming
   
3. What are the performance requirements?
   High throughput, binary data → gRPC
   Human debuggability matters → REST/JSON
   
4. What is the payload?
   Tensors, embeddings, binary → gRPC/protobuf
   Structured data, nested → GraphQL
   Simple JSON → REST
```

### 14.2 Pattern: Chat / Messaging System

```
Protocol selections:
  - Client → Server: WebSocket (full-duplex, low latency)
  - Server → Server: gRPC (internal, type-safe, multiplexed)
  - Message fan-out: Redis Pub/Sub or Kafka (backplane between WS servers)
  - Presence: WebSocket heartbeat + Redis/CRDT for presence state
  - Push notifications: Firebase/APNs (when WS disconnected)
  - Message storage API: REST (for history, search)

Scaling WebSocket:
  - Sticky sessions via consistent hashing on user_id
  - Pub/Sub backplane (Redis) for cross-server message delivery
  - Connection gateway layer (stateful) + app logic layer (stateless)
  - Heartbeat interval: 30s, dead peer detection: 90s
```

### 14.3 Pattern: Real-Time ML Feature Pipeline

```
Protocol selections:
  - Event ingestion: gRPC client streaming (batch events)
  - Stream processing: Kafka consumer → Flink (internal, binary)
  - Feature writes: gRPC to feature store (Redis wire protocol for online)
  - Feature reads: gRPC (p99 < 10ms for model serving)
  - Model inference: gRPC server streaming (LLM token streaming)
  - Client API: REST + SSE (streaming tokens to browser)

Key design points:
  - Protobuf for all internal payloads (embeddings are float tensors)
  - gRPC deadlines propagated through the chain
  - Connection pooling: gRPC channels shared across threads
  - L7 load balancing (Envoy) for gRPC backend
```

### 14.4 Pattern: CDN + API Gateway

```
Protocol selections:
  - Client → CDN edge: HTTP/3 (QUIC, 0-RTT, connection migration)
  - CDN edge → Origin: HTTP/2 (multiplexed, stable datacenter network)
  - Origin → Microservices: gRPC (internal, binary, streaming)
  - DNS: GeoDNS with health checks, TTL=60s for failover
  - TLS: mTLS between origin and microservices, TLS 1.3 for clients
  - Certificate management: cert-manager + Let's Encrypt (edge),
    SPIFFE/SPIRE (internal mTLS)

Performance:
  - HTTP/3 reduces first-paint by 1-2 RTT for mobile users
  - Connection migration prevents reset on Wi-Fi↔cellular handoff
  - Edge caching: static assets at CDN, dynamic API at origin
  - Connection pooling: edge maintains persistent HTTP/2 to origin
```

### 14.5 Pattern: Multi-Region Database Replication

```
Protocol selections:
  - Client → nearest region: GeoDNS + HTTP/2
  - Cross-region replication: gRPC streaming (continuous log shipping)
  - Consensus: gRPC (Raft RPCs between nodes)
  - Health checks: gRPC health protocol + TCP keepalive
  - Congestion control: BBR (cross-region, high-latency links)

Key points:
  - TLS everywhere, mTLS for inter-node communication
  - gRPC deadlines: cross-region RPCs get longer deadlines (~500ms)
    vs intra-region (~50ms)
  - DNS TTL=30s for fast failover during region evacuation
  - Connection pooling: persistent gRPC channels between regions,
    with health checking and automatic reconnection
```

### 14.6 Quick-Reference Decision Matrix

| Scenario | Transport | App Protocol | Serialization | Why |
|---|---|---|---|---|
| Public API | TCP+TLS | REST/HTTP | JSON | Browser support, debuggability |
| Internal service mesh | TCP+mTLS | gRPC/HTTP2 | Protobuf | Performance, type safety |
| ML model serving | TCP+TLS | gRPC | Protobuf | Tensor payloads, streaming |
| LLM token streaming | TCP+TLS | SSE or gRPC stream | JSON/Proto | Progressive rendering |
| Chat / collaboration | TCP+TLS | WebSocket | JSON | Full-duplex, low latency |
| IoT telemetry | TCP/UDP | MQTT or gRPC | Proto/CBOR | Lightweight, constrained devices |
| Video streaming | UDP | QUIC/WebRTC | Binary | Real-time, loss tolerant |
| DNS | UDP | DNS protocol | Binary | Single packet, stateless |
| Metrics collection | UDP | StatsD | Text | Fire-and-forget, loss acceptable |
| Database replication | TCP+mTLS | gRPC stream | Protobuf | Ordered, reliable log shipping |
| CDN edge → client | UDP (QUIC) | HTTP/3 | Varies | 0-RTT, no HOL blocking |
| Feature store reads | TCP | gRPC or Redis | Proto/RESP | Sub-10ms p99 |

---

## Cross-References

### Within `distributed-systems/`
- `07-kafka-and-event-streaming.md`: Kafka wire protocol, consumer protocol, zero-copy I/O
- `08-caching-strategies-and-patterns.md`: CDN caching, cache invalidation over the network
- `10-sharding-and-consistent-hashing.md`: Partition routing protocols, client-side vs proxy routing
- `22-stream-processing-flink-watermarks-eos.md`: Flink RPC framework, backpressure over network
- `29-failure-detection-phi-accrual.md`: Heartbeat protocols, timeout tuning
- `33-resilience-patterns-circuit-breakers.md`: Circuit breakers, retry strategies, deadline propagation
- `34-adaptive-load-control-and-backpressure.md`: AIMD for connection concurrency, load shedding
- `35-reliability-math-slos-and-error-budgets.md`: Availability math for networked services

### Within `databases/`
- `../databases/12-replication-and-distributed-storage.md`: Replication wire protocols
- `../databases/19-distributed-databases-deep-dive.md`: Distributed transaction protocols (2PC, Percolator)

### Within `sre-observability/`
- `../sre-observability/02-opentelemetry-deep-dive.md`: W3C trace context propagation over HTTP/gRPC
- `../sre-observability/26-llm-and-ai-observability.md`: LLM inference latency measurement (TTFT, ITL)

### Within `ai-rag/`
- `../ai-rag/appendix-e-deployment-and-compute.md`: Model serving deployment, gRPC serving infrastructure
