# Networking Protocols and Communication: A Complete Interview-Ready Deep Dive

A production-grade reference covering the networking stack that every distributed system sits on top of. Covers TCP/UDP fundamentals (handshakes, congestion control, when to pick UDP), the HTTP evolution from 1.1 through HTTP/2 to HTTP/3 and QUIC, gRPC and Protocol Buffers (why ML serving uses it), REST design principles (idempotency, versioning, HATEOAS), WebSocket (scaling, heartbeats), GraphQL (N+1 problem, when it makes sense), DNS resolution (caching, TTL, failover), TLS and mTLS (certificate rotation, pinning), and connection pooling across HTTP, databases, and gRPC channels.

Every section is written for engineers who will be asked "why did you pick gRPC here instead of REST?" or "how does HTTP/2 multiplexing actually work?" in a system design interview.

Prerequisites: familiarity with distributed system fundamentals from the `README.md` roadmap.

---

## Table of Contents

0. [Start here — the whole chapter in plain words](#start-here--the-whole-chapter-in-plain-words)
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
15. [Real-world cases — incidents with numbers](#15-real-world-cases--incidents-with-numbers)

---

## Start here — the whole chapter in plain words

**The problem.** Every call between two computers pays a toll in round trips. Opening a
connection, proving identity with TLS and warming up TCP's speed can cost several round trips
before any useful data arrives. On a phone far from the server, each round trip can be 100 ms or
more. This chapter explains where those round trips come from, which protocol (TCP, UDP, HTTP/1.1,
HTTP/2, HTTP/3, gRPC, WebSocket, DNS, TLS) removes which ones, and what breaks when you get it wrong.

**A real-world example.** A shopping app user in Berlin opens a product page. The API lives in
Virginia, so one round trip (RTT) is about 100 ms. The response is 200 KB. All numbers below are
illustrative and assume no packet loss unless stated.

- **Fresh connection, TCP + TLS 1.2:** TCP handshake 1 RTT, TLS 1.2 2 RTT, then the request
  1 RTT → the first byte arrives at 4 RTT = 400 ms. TCP slow start sends 14.6 KB, then 29.2,
  58.4, 116.8 KB (219 KB total), so 200 KB needs 4 flights → the last byte arrives at about
  7 RTT = **700 ms**.
- **Switch to TLS 1.3 (§9):** one fewer RTT → about **600 ms**.
- **Reuse a warm connection (§10):** no handshakes and the window is already large → about
  1 RTT = **100 ms** plus server time.
- **HTTP/3 with 0-RTT resumption (§3.3):** the request rides in the first packet → first byte at
  1 RTT, last byte at about 4 RTT = **400 ms** even on a brand-new connection.
- **Lossy mobile network (§3.2):** the page loads 30 images over one HTTP/2 connection. A single
  lost packet pauses all 30 for about one RTT. With HTTP/3 only the image that lost the packet waits.
- **Behind the API (§4, §10):** the product service calls pricing and stock over gRPC on pooled
  connections, passes along its remaining time budget (deadline), and uses an L7 or client-side
  load balancer so every pricing pod gets traffic.

| Term | Plain meaning | Everyday analogy |
|---|---|---|
| RTT (round-trip time) | time for a packet to go there and a reply to come back | asking a question across a room and hearing the answer |
| TCP | reliable, ordered byte stream between two machines | a phone call: you dial, both confirm, then talk in order |
| UDP | single packets, no delivery guarantee | posting postcards: fast, but some may never arrive |
| Handshake | setup messages exchanged before real data | "Hello?" "Hello, I hear you." "Great, here's why I called." |
| Slow start | TCP begins slowly and doubles its speed each RTT | a new employee gets small tasks first, bigger ones as trust grows |
| Congestion control | TCP slows down when the network looks overloaded | easing off the gas when traffic ahead brakes |
| Head-of-line (HOL) blocking | one stuck item holds up everything behind it | one slow shopper at the only open checkout lane |
| Multiplexing | many requests share one connection at the same time | many conversations over one phone line, each tagged with a name |
| QUIC / HTTP/3 | a newer transport over UDP with independent streams and built-in TLS | several separate lanes instead of one: a crash in one lane does not stop the others |
| TLS / mTLS | encryption plus identity check; mTLS checks both sides | showing ID at the door; mTLS: the door also shows you its badge |
| gRPC + protobuf | typed remote function calls with compact binary messages | a pre-printed form both sides agreed on, instead of a free-form letter |
| WebSocket / SSE | a connection that stays open so the server can push messages | an open phone line vs. calling back every minute to ask "anything new?" |
| DNS + TTL | name-to-address lookup, cached for TTL seconds | a phone book entry you trust for a fixed time before checking again |
| Connection pool | a set of already-open connections that requests borrow and return | a taxi rank: cars wait ready instead of being built for each ride |
| TIME_WAIT | a closed connection's slot kept reserved for a while | a parking space coned off for a minute after a car leaves |

### Symbols and parameters used in this chapter

| Symbol | What it means | Typical value | Simple example |
|---|---|---|---|
| RTT | round-trip time | 0.1–1 ms same datacenter, 60–100 ms across the US, 100–300 ms mobile | Berlin to Virginia ≈ 100 ms |
| one-way latency | time in one direction, about RTT / 2 | half the RTT | 30 ms one-way → 60 ms RTT |
| MSS | max TCP payload per packet | 1,460 bytes on Ethernet | 1 MB ≈ 685 packets |
| cwnd | congestion window: data TCP may have "in the air" unacknowledged | starts at 10 × MSS | grows 14.6 → 29.2 → 58.4 KB per RTT in slow start |
| IW | initial congestion window | 10 segments ≈ 14.6 KB | first flight of a new connection |
| ssthresh | size at which slow start stops doubling | set after the first loss | above it, cwnd grows slowly |
| BDP | bandwidth-delay product = bandwidth × RTT: how much data must be in flight to fill the link | 100 Mbps × 60 ms = 750 KB; 1 Gbps × 60 ms = 7.5 MB | a 64 KB window on a 60 ms path caps at 64 KB × 8 / 0.06 s ≈ 8.7 Mbps |
| p (loss rate) | fraction of packets lost | 0.01–0.1% wired, 1–2% bad mobile | 1% loss hurts Cubic much more than BBR (§1.2) |
| MSL / TIME_WAIT | max segment lifetime; how long a closed socket is kept | Linux: 60 s TIME_WAIT | 10,000 closes/s × 60 s = 600,000 sockets |
| ephemeral ports | local ports for outgoing connections | Linux 32768–60999 = 28,232 | ≈ 470 new connections/s to one IP:port before running out |
| `tcp_keepalive_time` / `_intvl` / `_probes` | idle time before probing, gap between probes, probes before giving up | Linux defaults 7200 s / 75 s / 9 | 7200 + 75 × 9 = 7875 s ≈ 2.2 h to notice a dead peer |
| `TCP_NODELAY` | turn off Nagle's batching of small writes | on for RPC | avoids ~40 ms delayed-ACK stalls |
| RPS / λ | requests per second | 1,000–100,000 | API gateway at 50,000 RPS |
| in flight (L = λ × W) | requests being served at once = rate × latency (Little's law) | — | 50,000 RPS × 0.02 s = 1,000 in flight |
| max concurrent streams | HTTP/2 requests allowed at once on one connection | 100–250 (nginx 128) | 1,000 in flight / 100 = 10 connections |
| Mbps vs MB/s | megabits vs megabytes per second (× 8) | — | 50 MB/s = 400 Mbps |
| TTL (DNS) | seconds a DNS answer may be cached | 30–300 s, 3600 s for static | TTL 60 s → most clients move within ~1 min |
| `MaxIdleConnsPerHost` | Go HTTP client: idle connections kept per backend | default 2 | raise to ~100 for a busy backend |
| pool_size | database pool size, HikariCP rule: cores × 2 + spindles | 4-core SSD box → 9 | 10 connections often beat 100 |
| deadline | absolute time by which a call must finish, passed downstream | 50 ms in-region, ~500 ms cross-region | 500 ms budget, 100 ms used → 400 ms passed on |
| TTFT / ITL | LLM time to first token / time between tokens | 200 ms / 30 ms | 500 tokens → 200 + 500 × 30 = 15.2 s |
| p50 / p99 | latency that 50% / 99% of requests beat | — | p99 = 190 ms → 1 in 100 requests is slower |
| gRPC codes 4 / 8 / 14 | DEADLINE_EXCEEDED / RESOURCE_EXHAUSTED / UNAVAILABLE | — | 14 is usually safe to retry |
| HTTP 502 / 503 / 504 | bad upstream response / overloaded / upstream too slow | — | proxy timeout hit → 504 |

If a section below gets too technical, read its **In plain words** box first.

---

## 1. TCP Fundamentals

> **In plain words.** TCP turns an unreliable network into a reliable, ordered stream of bytes. The price: a handshake before any data moves, a slow start before it moves fast, and some leftover state after it closes. Most TCP advice boils down to "open connections rarely and reuse them".
>
> **Real-world example.** A checkout service in Frankfurt calls a payment API in Virginia (RTT about 90 ms). A fresh TCP + TLS 1.3 connection costs 2 RTT = 180 ms before the request is even sent. A pooled connection skips that, so the call takes about 1 RTT = 90 ms plus server time.

### 1.1 The Three-Way Handshake

Every TCP connection begins with a three-way handshake. This is not a detail you can skip -- it directly determines the latency floor for every new connection your system opens.

```
CLIENT                                    SERVER
  |                                          |
  |  ──── SYN (seq=x) ──────────────────>    |
  |                                          |  (1/2 RTT)
  |  <──── SYN-ACK (seq=y, ack=x+1) ────    |
  |                                          |  (1/2 RTT)
  |  ──── ACK (ack=y+1) + [data] ───────>   |
  |                                          |
  |  Connection established.                 |
  |  Minimum cost: 1 RTT before data flows.  |

Total latency for first byte:
  - TCP handshake:        1 RTT
  - TLS 1.3 handshake:  + 1 RTT  (or 0 for resumption)
  - HTTP request:        + 1 RTT
  ────────────────────────────────────────────
  First byte arrives:     3 RTT minimum (TLS 1.3 full handshake; 4 RTT with TLS 1.2)
                          2 RTT minimum (TLS 1.3 0-RTT resumption: request rides
                                         in the first TLS flight; plain PSK
                                         resumption without early data is still 1 RTT)
```

**Why this matters in system design**: if your service is in us-east and the database is in us-west (~30ms one-way latency), each new TCP connection costs ~60ms just for the handshake. Add TLS and you are at ~120ms before any data moves. This is why connection pooling (§10) is not optional for any serious system.

### 1.2 Congestion Control

TCP's congestion control algorithm determines how fast data actually flows. The two algorithms you need to know:

**Cubic (the default on most Linux kernels)**:
- Uses a cubic function to grow the congestion window after a loss event
- Loss-based: it only backs off when packets are dropped
- Problem: in high-bandwidth, high-latency links (long fat networks), Cubic is too conservative. It takes a long time to fill the pipe after a loss event
- Problem: because it only reacts to loss, Cubic keeps pushing until a queue overflows. With deep buffers this fills queues and adds delay (bufferbloat); with shallow datacenter switch buffers it causes frequent drops

**BBR (Bottleneck Bandwidth and Round-trip propagation time)**:
- Developed by Google, deployed on YouTube, Google Cloud, and most Google services
- Model-based: measures the actual bottleneck bandwidth and minimum RTT, then paces packets to match
- Does not wait for packet loss to reduce rate -- it actively probes and adjusts
- Much better on lossy WAN links: Google's BBR paper (Cardwell et al., ACM Queue 2016) reports 2-25x higher throughput than Cubic on its B4 inter-datacenter WAN

```
Cubic vs BBR behavior on a 100 Mbps link with 1% random packet loss
(illustrative, ~30-50 ms RTT; the Mathis estimate for loss-based TCP,
MSS/RTT × 1.22/sqrt(p), gives ~2.8-4.7 Mbps here):

Cubic:
  Throughput: ~3-5 Mbps  (loss causes aggressive backoff)
  Latency:    high       (fills buffers before detecting congestion)

BBR:
  Throughput: ~70-90 Mbps (paces to bottleneck bandwidth)
  Latency:    low         (avoids filling buffers)
```

**Interview relevance**: when designing a system that sends large payloads over WAN (geo-replicated databases, cross-region model weight transfer, CDN origin pull), mention that BBR is the right congestion control choice and that it is enabled per-socket via `setsockopt` or globally via sysctl.

### 1.3 TCP Slow Start

Every new TCP connection starts with a small congestion window (typically 10 segments × 1,460 bytes = ~14.6KB, per RFC 6928) and roughly doubles it each RTT until it hits the slow-start threshold (`ssthresh`) or detects loss. This means:

```
RTT 0:  sends 14 KB
RTT 1:  sends 28 KB
RTT 2:  sends 56 KB
RTT 3:  sends 112 KB
RTT 4:  sends 224 KB
...

Time to send a 1 MB response on a fresh connection (30ms RTT):
  14 + 28 + 56 + 112 + 224 + 448 = 882 KB after 6 rounds, so 7 rounds
  ~7 RTTs = ~210ms just for slow start to ramp up (plus the handshakes)

Time to send the same 1 MB on a warm, pooled connection:
  ~30ms   (congestion window already large)
```

**Design implication**: for small, frequent requests the fixed cost of a new connection (handshake RTTs, then a small first window) can be larger than the request itself. For microservice architectures with many small RPCs, connection reuse is critical. Note that Linux also shrinks the window of a connection that sat idle for longer than one retransmission timeout (`net.ipv4.tcp_slow_start_after_idle=1` by default), so a pooled connection that was idle can partly slow-start again; long-lived RPC services often set it to 0.

### 1.4 TIME_WAIT and Socket Exhaustion

When a TCP connection closes, the side that initiates the close enters `TIME_WAIT`. The RFC says to wait 2 × MSL (Maximum Segment Lifetime; RFC 793 suggests MSL = 2 minutes). Linux hardcodes TIME_WAIT at 60 seconds. During this time, the (source IP, source port, dest IP, dest port) tuple is occupied and cannot be reused.

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

Problem: a client or proxy opening 10,000 short-lived connections/second
  to ONE backend (same dest IP:port), and closing them first,
  accumulates 10,000 × 60 = 600,000 TIME_WAIT sockets
  Linux default ephemeral port range: 32768-60999 = 28,232 ports
  Sustainable rate to one dest IP:port: 28,232 / 60s ≈ 470 new conns/s

  Result: EADDRNOTAVAIL / "cannot assign requested address", connection failures

  (A server that closes first also piles up TIME_WAIT sockets, but they share
  its listening port, so they cost memory, not ephemeral ports.)
```

**Mitigations**:
1. **Connection pooling** (§10) -- reuse connections instead of creating/destroying them
2. `SO_REUSEADDR` -- lets a restarted server re-bind its listening port while old connections are in TIME_WAIT (it does not fix outbound port exhaustion; `SO_REUSEPORT` is for several sockets sharing one listening port)
3. `net.ipv4.tcp_tw_reuse=1` -- allow reuse of TIME_WAIT sockets for new outbound connections (needs TCP timestamps; safe for clients). Do not look for `tcp_tw_recycle`: it broke clients behind NAT and was removed in Linux 4.12
4. Increase ephemeral port range: `net.ipv4.ip_local_port_range = 1024 65535`
5. Use long-lived connections (HTTP/2 multiplexing, gRPC channels)

**Interview pattern**: any time you have a service making many short-lived outbound connections (e.g., a proxy service, a load balancer, a service calling many backends), mention TIME_WAIT exhaustion as a failure mode and connection pooling as the fix.

### 1.5 Nagle's Algorithm and TCP_NODELAY

Nagle's algorithm batches small writes into larger TCP segments to reduce the number of packets. The rule: a small segment may be sent only if no earlier data is still unacknowledged. On its own that costs at most one RTT, but combined with the receiver's **delayed ACK** (the receiver waits up to ~40ms on Linux, up to 200ms on some other stacks, before acknowledging) a write-write-read pattern can stall for the whole delayed-ACK timer.

```
Without TCP_NODELAY (Nagle enabled):
  write(4 bytes)  → nothing unacked, sent immediately
  write(4 bytes)  → first write still unacked: buffer
  write(4 bytes)  → buffer, still waiting
  Receiver delays its ACK (waiting for a response to piggyback on),
  so the last 8 bytes leave only when the ACK arrives:
  Latency: up to the delayed-ACK timer (~40ms Linux, up to 200ms elsewhere)

With TCP_NODELAY (Nagle disabled):
  write(4 bytes)  → send immediately
  write(4 bytes)  → send immediately
  write(4 bytes)  → send immediately
  Latency: each write leaves at once (one-way network delay only)
```

**Rule**: for RPC-style communication (gRPC, Redis, database wire protocols), always set `TCP_NODELAY`. For bulk data transfer (file uploads, backups), leave Nagle enabled. gRPC sets `TCP_NODELAY` by default.

### 1.6 Keep-Alive

TCP keep-alive sends periodic probes on idle connections to detect dead peers. The Linux defaults are terrible for production:

```
Default:
  tcp_keepalive_time  = 7200s  (2 hours before first probe)
  tcp_keepalive_intvl = 75s    (75s between probes)
  tcp_keepalive_probes = 9     (9 failed probes to declare dead)

  Time to detect dead peer: 7200 + (75 × 9) = 7875 seconds = ~2.2 hours

Production setting:
  tcp_keepalive_time  = 60s
  tcp_keepalive_intvl = 10s
  tcp_keepalive_probes = 6

  Time to detect dead peer: 60 + (10 × 6) = 120 seconds = 2 minutes
```

Application-level keep-alive (gRPC PING frames, WebSocket pings, HTTP/2 PING) is generally preferred because it works through NATs and load balancers that may not forward TCP keep-alive probes.

---

## 2. UDP and When to Use It

> **In plain words.** UDP just sends packets. No handshake, no retries, no ordering. That is a feature when old data is useless (a voice sample from 200 ms ago) and a bug when every byte matters (a bank transfer).
>
> **Real-world example.** A video call sends 50 audio packets per second. If 1 packet is lost, re-sending it would arrive too late to play, so the app skips it and you hear a 20 ms glitch instead of a 300 ms freeze.

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
| Service discovery (mDNS, SSDP) | Multicast to discover peers on a local network | Bonjour/Avahi (mDNS), UPnP (SSDP) |

### 2.3 When Not to Use UDP

**Never** use raw UDP when you need:
- Reliable, ordered delivery of all data (use TCP)
- Congestion-friendly behavior on the public internet (use TCP or QUIC)
- Data integrity beyond the UDP checksum (use TLS over TCP, or DTLS over UDP)

**The QUIC pattern**: if you need UDP's properties (no head-of-line blocking, connection migration) but also need reliability, QUIC builds those guarantees in userspace on top of UDP. This is what HTTP/3 does.

---

## 3. HTTP/1.1 vs HTTP/2 vs HTTP/3

> **In plain words.** HTTP/1.1 sends one request at a time per connection. HTTP/2 sends many at once over one TCP connection, but one lost packet still pauses all of them. HTTP/3 moves to QUIC over UDP, so a lost packet only pauses the one request it belonged to.
>
> **Real-world example.** A shopping app loads 30 product images on a phone with 2% packet loss. On HTTP/2 each lost packet freezes all 30 downloads for about one round trip. On HTTP/3 only the one image with the lost packet waits.

### 3.1 HTTP/1.1 — Sequential and Wasteful

HTTP/1.1 is a text-based protocol with one request-response pair per TCP connection at a time (pipelining exists in the spec but is disabled in practice). To achieve concurrency, browsers open up to 6 parallel TCP connections per origin.

```
HTTP/1.1 with persistent connections (Connection: keep-alive):

Connection 1:  [req1] ──> [res1] [req3] ──> [res3] [req5] ──> [res5]
Connection 2:  [req2] ──> [res2] [req4] ──> [res4] [req6] ──> [res6]
                               time ──────────────────>

Problems:
1. Head-of-line (HOL) blocking: req3 cannot start until res1 finishes
2. TCP connection overhead: up to 6 handshakes, 6 slow-start ramps
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
                         (rarely helped in practice; Chrome removed it in 2022)
  5. Stream priority:    the original H2 priority tree was deprecated by RFC 9113;
                         RFC 9218 "Extensible Priorities" replaces it for H2 and H3
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
On lossy links this can be worse than HTTP/1.1 with 6
connections, where only 1 of 6 connections would be blocked.
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
2. Fast handshake:      TLS 1.3 integrated: 1 RTT for a new connection,
                         0-RTT (data in the first packet) when resuming
3. Connection migration: connection ID survives Wi-Fi→cellular handoff
4. Userspace control:    congestion control and loss recovery in application
```

**Handshake comparison**:
```
Setup round trips BEFORE the first request can be sent
(add 1 more RTT to get the first response byte back):

TCP + TLS 1.2 (typical HTTP/1.1):  3 RTT  (TCP 1 + TLS 2)
TCP + TLS 1.3 (typical HTTP/2):    2 RTT  (TCP 1 + TLS 1)
HTTP/3 (QUIC):                     1 RTT  (QUIC combines transport + crypto)
HTTP/3 0-RTT:                      0 RTT  (resumption, sends data immediately)

The TLS version, not the HTTP version, decides the TLS cost:
HTTP/1.1 over TLS 1.3 is also 2 RTT.
```

### 3.4 Comparison Matrix

| Feature | HTTP/1.1 | HTTP/2 | HTTP/3 (QUIC) |
|---|---|---|---|
| Transport | TCP | TCP | UDP (QUIC) |
| Multiplexing | No (1 req/conn) | Yes (streams) | Yes (independent streams) |
| HOL blocking | Per-connection | TCP-level (all streams) | None (per-stream only) |
| Header compression | None | HPACK | QPACK |
| Handshake latency (setup before request) | 2-3 RTT (TCP + TLS 1.3/1.2) | 2-3 RTT (TCP + TLS 1.3/1.2) | 1 RTT (0 with resumption) |
| Connection migration | No | No | Yes (connection ID) |
| Server push | No | In spec; removed from major browsers | In spec; rarely implemented |
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

> **In plain words.** gRPC lets one service call a function on another service as if it were local. Messages are defined in a `.proto` schema and sent as compact binary over HTTP/2. It adds streaming, deadlines and generated client code.
>
> **Real-world example.** A ride-hailing dispatcher asks a pricing service for a fare 5,000 times per second. With gRPC the request is a typed `GetFare(trip)` call of about 100 bytes, and if the caller only has 150 ms left, the pricing service knows that too and gives up in time.

### 4.1 What gRPC Is

gRPC is a high-performance RPC framework built on HTTP/2, using Protocol Buffers (protobuf) as its interface definition language and serialization format. It is one of the most common choices for service-to-service communication and is widely supported by ML model servers.

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
Size: ~170 bytes (compact, no whitespace)

Protobuf (binary, schema-driven):
[binary encoding of the same data]
Size: ~77 bytes  (~55% smaller; strings dominate here, so savings are
                  modest. Number-heavy payloads shrink much more.)

Serialization speed (illustrative; depends heavily on language and library):
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

Many ML model servers expose a gRPC API (TensorFlow Serving, Triton Inference Server, TorchServe), usually next to an HTTP/REST one. LLM servers such as vLLM mainly expose an OpenAI-compatible HTTP API with SSE streaming. Internally, gRPC is popular for specific technical reasons:

```
ML inference request/response characteristics:
  - Request:  dense float tensors (embeddings: 768-4096 floats)
  - Response: probability distributions, logits, generated tokens
  - Volume:   1,000-100,000 inferences/second
  - Latency:  p50 < 10ms for feature lookups, < 100ms for model inference

Why gRPC wins:
  1. Binary serialization: a 768-dim float32 embedding is 3,072 bytes in protobuf
                           vs ~6,000-8,000 bytes in JSON with floats rounded to
                           ~6 digits, ~17,000 at full precision (each float
                           becomes a string)
  2. Streaming:           token-by-token LLM generation maps to server streaming
  3. Code generation:     typed messages catch field/type mistakes at compile time
                           (tensor shapes are still checked at runtime)
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

**Which algorithm, once you are at L7.** Round-robin fixes the *connection* problem but not the *slow replica* problem: a replica that is half as fast (GC, noisy neighbour) still gets 1/N of the calls and its queue grows without bound. Prefer **power-of-two-choices least-request** (Envoy `LEAST_REQUEST`, gRPC xDS `least_request`), which reads requests in flight. With thousands of clients, add **subsetting** so each client keeps connections to k backends instead of all N. The simulation and the algorithm table are in [`34-adaptive-load-control-and-backpressure.md`](34-adaptive-load-control-and-backpressure.md) §6.7.

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

> **In plain words.** REST is a style for HTTP APIs: resources have URLs, you act on them with GET/PUT/POST/DELETE, and every request carries everything the server needs. The most useful idea for distributed systems is idempotency: retrying a request must not do the work twice.
>
> **Real-world example.** A customer taps "Pay 49.99 EUR" and the network drops before the reply. The app retries with the same idempotency key, and the server returns the stored first result instead of charging the card a second time.

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
PATCH  /users/123        → NOT guaranteed idempotent ("set name" is, "increment counter" is not)
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
and common in large public APIs (Stripe, for example, uses /v1 paths plus
a date-based version header).
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
Common rate limiting headers (X-RateLimit-* is a convention, not a standard; Retry-After is standard):

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

> **In plain words.** WebSocket turns one HTTP connection into a two-way pipe that stays open. Either side can send a message at any time, with no polling. The hard parts are keeping millions of open connections alive and routing a message to the server that holds the right user.
>
> **Real-world example.** A chat app has 2 million online users spread over 40 gateway servers (50,000 connections each). When Alice (server 7) messages Bob (server 31), server 7 publishes to a pub/sub channel and server 31 pushes it down Bob's socket.

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
  
  Either side sends at any time. No polling. Latency ≈ one network trip.
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
  - Browser JavaScript cannot send protocol-level PING frames at all
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

> **In plain words.** GraphQL lets the client ask for exactly the fields it needs in one request. It saves round trips for the client but moves the work to the server, which can easily end up running one database query per item.
>
> **Real-world example.** A mobile home screen shows 10 friends and each friend's latest order. With REST that is 1 + 10 = 11 calls from the phone; with GraphQL it is 1. Without batching, the server now runs the 1 + 10 = 11 SQL queries itself; with DataLoader it runs 2 (one for friends, one `WHERE user_id IN (...)` for orders).

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
   Prevents arbitrary query injection (common for first-party mobile/web clients)
```

---

## 8. DNS Resolution

> **In plain words.** DNS turns a name like `api.example.com` into an IP address. Answers are cached at several layers for a time called the TTL. Short TTLs mean fast failover but more lookups; long TTLs mean the opposite. Caches that ignore the TTL are a classic outage cause.
>
> **Real-world example.** A bank moves its API to a standby region. The DNS record has TTL 60 s, so most clients switch within about a minute, but a service whose connection pool never reconnects keeps sending traffic to the dead region until someone restarts it.

### 8.1 How DNS Resolution Works

DNS is the first network call in almost every distributed system interaction. Understanding its resolution chain, caching behavior, and failure modes is essential.

```
DNS resolution chain:

Application calls getaddrinfo("api.example.com")
  │
  ▼
┌──────────────────────────────┐
│ 1. Application/Library Cache │  (JVM InetAddress cache, nscd; Go has none)
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
  - Clients may cache beyond TTL (the JVM ignores the record TTL and uses its own
    setting: 30s by default, forever if a security manager is installed)

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
  Widely used by CDNs and global services (many also use anycast).

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
   The JVM ignores DNS TTLs. Default: 30 seconds, but FOREVER (TTL = -1)
   when a security manager is installed (older app servers did this)
   Fix: -Dsun.net.inetaddr.ttl=30 (cache for 30 seconds)
   Or: networkaddress.cache.ttl=30 in java.security

2. Connection pool caching stale IPs:
   HTTP client creates connection pool to api.example.com
   DNS resolves to 10.0.1.1, pool creates 100 connections
   DNS changes to 10.0.2.1 (deployment, failover)
   Connection pool still sends traffic to 10.0.1.1 (stale connections!)
   
   Fix: set max connection lifetime in pool (e.g., 5 minutes)
   Fix: re-resolve and rebalance (e.g., gRPC's DNS resolver re-resolves when
        connections fail; plain HTTP pools usually do not)

3. Kubernetes DNS resolution at scale:
   Pod DNS queries go to CoreDNS (cluster service)
   High QPS services can overwhelm CoreDNS
   
   Fix: NodeLocal DNSCache (DaemonSet that caches on each node)
   Fix: Use headless services with client-side resolution
   Fix: Lower ndots (default 5 in Kubernetes) in pod DNS config (reduces search domain expansion)

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

> **In plain words.** TLS encrypts the connection and proves the server is who it claims to be. TLS 1.2 needs 2 round trips to set up, TLS 1.3 needs 1. mTLS makes the client prove its identity too, which is how services in a zero-trust network know who is calling.
>
> **Real-world example.** A payments service only accepts calls from the checkout service. With mTLS, checkout presents a certificate that names it; a compromised analytics pod without that certificate is rejected during the handshake, before it can send a single request.

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

**0-RTT replay risk**: an attacker can capture and replay the 0-RTT data. This is safe for GET requests but dangerous for POST requests (could replay a payment). Most servers and RPC stacks leave 0-RTT disabled by default for this reason.

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
  - B2B API authentication (for example, some banking and payment APIs)
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
  - Used by: SPIFFE/SPIRE-based meshes (SPIRE's default X.509 SVID lifetime is 1 hour)
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

> **In plain words.** A connection pool keeps a set of already-open connections and lends them out. You pay for the handshake once instead of on every request. The pool must be sized right, and it must throw away connections that are dead or point to an old address.
>
> **Real-world example.** An order service does 1,000 database queries per second, each taking 5 ms. That is only 1,000 × 0.005 = 5 queries in flight on average, so a pool of about 10–20 connections is plenty, instead of opening 1,000 new connections per second.

### 10.1 Why Connection Pooling Exists

Opening a new connection for every request is prohibitively expensive:

```
Cost of a new connection:

TCP:  1 RTT (handshake)                    ~1ms datacenter, ~30ms cross-region
TLS:  1 RTT (TLS 1.3) or 2 RTT (TLS 1.2) ~1-2ms datacenter, ~30-60ms cross-region
Auth: 1 RTT (database auth, token exchange) ~1-5ms
TCP slow start: 7+ RTTs to reach full throughput

Total for first useful byte: 3-5 RTTs = ~3-10ms datacenter (RTT ~1ms plus
  auth work), ~90-150ms cross-region (RTT ~30ms)

vs. reusing a pooled connection: 0 extra RTTs, immediate data transfer

At 10,000 requests/second with 2ms connection overhead:
  New connections: 10,000 × 2ms = 20 s of extra waiting per second, i.e.
                   every request is 2ms slower and ~20 requests sit in
                   handshakes at any moment; plus 10,000 × 60s = 600,000
                   TIME_WAIT sockets and TLS handshake CPU on both sides
  Pooled:          no handshakes, no TIME_WAIT churn
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
  If your service calls one backend at 1000 RPS with ~20 requests in flight:
    → only 2 idle connections are kept; the other ~18 are closed after
      each burst and reopened, which can mean hundreds of new
      connections per second
    → TIME_WAIT socket buildup and, above ~470 new conns/s to one
      IP:port, ephemeral port exhaustion

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
    (the default policy, pick_first, sends everything to one backend)
  - Know the server's max concurrent streams per connection (defaults
    differ: HTTP/2 recommends at least 100; nginx uses 128)
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

> **In plain words.** An ML request usually crosses several hops: browser to gateway, gateway to feature store, feature store to model server. Each hop gets the protocol that fits it: JSON/REST where humans and browsers are involved, gRPC/binary inside, and streaming (SSE or gRPC streams) for LLM tokens.
>
> **Real-world example.** A support chatbot streams a 500-token answer. The first token arrives after 200 ms and the rest at 30 ms each, so the full answer takes 200 + 500 × 30 = 15,200 ms. With streaming the user starts reading at 0.2 s instead of staring at a spinner for 15 s.

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
  POST /v1/chat/completions (OpenAI-compatible, body has "stream": true)
  Response Content-Type: text/event-stream
  
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

Protocol comparison for feature serving (illustrative numbers):

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

> **In plain words.** A few formulas cover most networking capacity questions: requests in flight = requests per second × latency; bandwidth = requests per second × bytes per request × 8 bits; and a single TCP connection can carry at most window size ÷ RTT.
>
> **Real-world example.** An e-commerce API gets 50,000 requests/s at 20 ms each, so 50,000 × 0.02 = 1,000 requests are in flight at any moment. With HTTP/2 and 100 streams per connection, 1,000 ÷ 100 = 10 connections are enough.

### 12.1 Connection Math

```
Scenario: API gateway serving 50,000 requests/second

HTTP/1.1 (one request per connection at a time):
  Average request duration: 20ms
  Connections needed: 50,000 × 0.020 = 1,000 concurrent connections
  With connection reuse (keep-alive): ~1,000 pooled connections
  Without connection reuse: 50,000 new connections/second
    → 50,000 × 60s = 3,000,000 TIME_WAIT sockets at steady state
    → To one backend IP:port, the 28,232 ephemeral ports run out
      in under a second

HTTP/2 (multiplexed, ~100 streams per connection):
  Streams limit concurrency, not rate: 1,000 in flight / 100 = 10 connections
  In practice: 10-50 connections (20-100 in-flight streams each,
  1,000-5,000 requests/second each)
  Much better resource utilization

gRPC (HTTP/2 with long-lived channels):
  Connections: 1 per backend server (multiplexed)
  With 10 backend servers: 10 connections total
  Each handles 5,000 RPC/s, ~100 in flight at a time
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
    JSON:     300 MB/s = 2.4 Gbps (about a quarter of a 10G NIC for one service)
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

Note: adding p99s is a rough, usually pessimistic budget, not the true p99 of
the sum. Real p99 must be measured end to end.
```

---

## 13. Failure Modes and Debugging

> **In plain words.** Most network errors have a small set of causes, and each error name points to one of them: nobody listening (refused), packets silently dropped (timeout), someone slammed the door (reset). Knowing which layer produced the error tells you where to look.
>
> **Real-world example.** An IoT backend sees `ECONNRESET` on 3% of device uploads. The load balancer drops connections idle for more than 350 s, and devices upload every 10 minutes on a reused connection. Sending a keep-alive ping every 60 s brings the errors to near zero.

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
   Cause: upstream crashed, refused or reset the connection, or returned an invalid
          response (e.g., upstream keep-alive timeout shorter than the proxy's).
   Debug: check upstream health and logs, align keep-alive/idle timeouts.

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

> **In plain words.** Interviewers rarely want packet diagrams. They want you to pick a protocol for each hop and justify it with one number and one trade-off. A good answer names the client, the traffic pattern, the payload and the failure you are protecting against.
>
> **Real-world example.** "Mobile app to our API: HTTPS/JSON, because browsers and phones speak it and we can debug it with curl. Service to service: gRPC, because we make 20,000 calls/s with deadlines. The catch: gRPC needs L7 or client-side load balancing, or new pods get no traffic."

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
| Real-time video (calls) | UDP | WebRTC (RTP/SRTP) | Binary | Real-time, loss tolerant (video-on-demand usually uses HLS/DASH over HTTP) |
| DNS | UDP | DNS protocol | Binary | Single packet, stateless |
| Metrics collection | UDP | StatsD | Text | Fire-and-forget, loss acceptable |
| Database replication | TCP+mTLS | gRPC stream | Protobuf | Ordered, reliable log shipping |
| CDN edge → client | UDP (QUIC) | HTTP/3 | Varies | 0-RTT, no HOL blocking |
| Feature store reads | TCP | gRPC or Redis | Proto/RESP | Sub-10ms p99 |

---

## 15. Real-world cases — incidents with numbers

> **In plain words.** Six short incident stories. Each one shows a symptom you might see in
> production, the numbers that pointed to the cause, the fix, and what changed afterwards.
>
> **Real-world example.** "Payments fail every few minutes with `cannot assign requested address`"
> turns out to be 2,000 new connections per second to one backend, which uses up 28,232 ports in
> about 14 seconds (Case 1).

These are **composite scenarios** built from failure modes this chapter describes; numbers are
illustrative but internally consistent.

**Quick index:** outbound connect errors under load → Case 1 · new pods get no traffic → Case 2 ·
mobile latency worse on bad networks despite HTTP/2 → Case 3 · small messages take ~40 ms → Case 4 ·
errors continue long after a DNS failover → Case 5 · cross-region copy far slower than the link →
Case 6.

### Case 1 — Payments proxy runs out of ports

- **Setup.** A Go payments proxy forwards 3,000 requests/s to one fraud-check backend (one
  virtual IP and port). Each call takes about 20 ms, so about 3,000 × 0.02 = 60 calls are in flight.
  The HTTP client uses Go's default `MaxIdleConnsPerHost = 2`.
- **Symptom.** During a sale, bursts of `dial tcp: connect: cannot assign requested address`
  errors; payment success rate drops from 99.9% to 93%.
- **Measurement/Diagnosis.** `ss -s` shows about 28,000 sockets in TIME_WAIT, all to the same
  destination. Only 2 connections stay idle in the pool, so most of the ~60 in-flight connections
  are closed after use: about 2,000 new connections/s. Steady state would need 2,000 × 60 s =
  120,000 TIME_WAIT slots, but only 28,232 ephemeral ports exist, so they run out after
  28,232 / 2,000 ≈ 14 s.
- **Fix.** Set `MaxIdleConnsPerHost = 200` and `MaxConnsPerHost = 300`. New connections drop from
  ~2,000/s to under 5/s; TIME_WAIT count drops from ~28,000 to a few hundred; connect errors go
  to zero.
- **Lesson.** Port exhaustion is a pool-sizing bug, not a kernel-tuning problem. Size the idle
  pool to the number of requests in flight (rate × latency).

### Case 2 — New dispatch pods receive zero gRPC traffic

- **Setup.** A ride-hailing dispatch service calls an ETA service over gRPC through a TCP (L4)
  load balancer. The ETA service autoscales from 3 to 6 pods at the evening peak.
- **Symptom.** p99 ETA latency climbs from 40 ms to 900 ms even after scaling out.
- **Measurement/Diagnosis.** The 3 old pods run at 90% CPU; the 3 new pods at 2%. Each dispatch
  client holds one long-lived HTTP/2 connection opened before the scale-out, and every RPC is
  multiplexed over it. The L4 balancer only balances connections, so new pods never get any.
- **Fix.** Switch to client-side `round_robin` over a headless service (one subchannel per pod),
  and set a server `MaxConnectionAge` of 5 minutes so clients reconnect and see new pods. CPU
  evens out to about 90% × 3 / 6 = 45% per pod; p99 returns to about 45 ms.
- **Lesson.** gRPC needs L7 or client-side load balancing (§4.6). Scaling out does nothing if
  traffic is pinned to old connections.

### Case 3 — Video platform's mobile API is slow on bad networks

- **Setup.** A video platform's mobile app loads its home screen (about 40 packets of API
  responses and thumbnails) over one HTTP/2 connection. In some markets, cellular loss is about 2%
  and RTT about 150 ms.
- **Symptom.** p95 home-screen load is 1.9 s in those markets vs 0.7 s on Wi-Fi.
- **Measurement/Diagnosis.** With 2% loss and 40 packets, the chance that at least one packet is
  lost is 1 − 0.98^40 ≈ 55%. Each loss stalls every stream on the connection for at least one RTT
  (150 ms), longer if the retransmission timer fires. Packet traces show all streams waiting on
  one retransmitted packet (§3.2).
- **Fix.** Enable HTTP/3 at the CDN edge, with fallback to HTTP/2 when UDP is blocked. A loss now
  stalls only the stream that lost the packet, and 0-RTT resumption saves the setup RTT on
  returning users. p95 drops from 1.9 s to about 1.3 s (illustrative).
- **Lesson.** HTTP/2 fixes HOL blocking at the HTTP layer but not at the TCP layer. On lossy
  networks, QUIC's independent streams matter.

### Case 4 — Chat messages take 40 ms instead of 1 ms

- **Setup.** A chat backend sends each message to an internal fan-out service over a custom TCP
  protocol: one small `write()` for the header, a second for the body, then waits for a reply.
- **Symptom.** p50 publish latency is about 41 ms inside one datacenter where RTT is 0.3 ms.
- **Measurement/Diagnosis.** `tcpdump` shows the header leave at once, the body wait about 40 ms,
  and the receiver's ACK arriving right before the body. Nagle holds the body until the header is
  acknowledged, and the receiver's delayed-ACK timer (~40 ms on Linux) holds the ACK (§1.5).
- **Fix.** Set `TCP_NODELAY` and send header and body in one write. p50 drops from ~41 ms to
  ~1 ms.
- **Lesson.** A latency that clusters at 40 ms (or 200 ms) on a fast network is a Nagle/delayed-ACK
  fingerprint.

### Case 5 — Bank ledger writes fail for 22 minutes after a failover

- **Setup.** A bank's ledger service writes to a database behind `db-primary.internal`, DNS TTL
  60 s. The pool keeps connections open forever.
- **Symptom.** After a planned failover, 100% of writes fail with "database is read-only" for
  22 minutes until an engineer restarts the pods.
- **Measurement/Diagnosis.** DNS switched correctly within 60 s, but the pool's 40 connections
  were still open to the old primary, now a read-only replica. Nothing forced a reconnect, so the
  new DNS answer was never used (§8.4, §10.5).
- **Fix.** Set max connection lifetime to 5 minutes, drop TTL to 30 s, and evict all pooled
  connections on a "read-only" error. In the next failover test, writes recover in about 45 s.
- **Lesson.** A DNS TTL only matters if clients look the name up again. Connection lifetime must
  be shorter than the failover time you promise.

### Case 6 — Cross-region backup copies at a tenth of the link speed

- **Setup.** An IoT telemetry platform copies 500 GB of nightly data from Europe to the US over a
  1 Gbps link with 80 ms RTT. The copy tool sets a fixed 1 MB socket buffer.
- **Symptom.** The copy runs at about 100 Mbps and takes about 11 hours, spilling into the
  morning peak.
- **Measurement/Diagnosis.** Bandwidth-delay product = 1 Gbps × 0.08 s = 10 MB must be in flight
  to fill the link. One connection can carry at most window / RTT = 1 MB × 8 / 0.08 s = 100 Mbps.
  At 100 Mbps, 500 GB takes 500 × 8 / 0.1 s ≈ 11.1 hours.
- **Fix.** Remove the fixed buffer (setting `SO_RCVBUF` turns off Linux auto-tuning), raise
  `net.ipv4.tcp_rmem`/`tcp_wmem` maximums to 16 MB, and use BBR since the path has some loss.
  Throughput rises to about 940 Mbps; the copy takes 500 × 8 / 0.94 s ≈ 1.2 hours.
- **Lesson.** On long paths, the window, not the link, is often the limit. Compute the BDP before
  buying more bandwidth.

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
