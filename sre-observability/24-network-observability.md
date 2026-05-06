# 24 — Network Observability

> Below the application, below the database, below the mesh, lives the network. Most application teams are blind to it — they see "this RPC took 800ms" and assume it's compute. Often it's retransmits, packet loss, MTU mismatch, NAT exhaustion, conntrack overflow, or a flapping BGP route. Network observability is what makes these stories visible.

This chapter is about the L3-L4 layer — the network the application doesn't see — and increasingly the eBPF-based tools that bring this layer into the observability stack with low overhead and rich detail.

---

## Table of Contents

1. [Thesis](#1-thesis)
2. [The five network signal sources](#2-five-sources)
3. [TCP-level signals: retransmits, RTT, window](#3-tcp-signals)
4. [Connection-level signals: refused, reset, half-open](#4-connection-signals)
5. [Kernel signals: conntrack, sockets, queues](#5-kernel-signals)
6. [Flow logs: NetFlow, sFlow, VPC flow logs](#6-flow-logs)
7. [eBPF as a network observability primitive](#7-ebpf)
8. [DNS: the silent failure surface](#8-dns)
9. [Load balancer observability](#9-load-balancer)
10. [Cross-zone, cross-region, cross-cloud signals](#10-cross-region)
11. [The L4 vs L7 distinction](#11-l4-vs-l7)
12. [Multi-tenancy in network telemetry](#12-multi-tenancy)
13. [The "who talks to whom" map](#13-talk-map)
14. [Pcap-on-demand and packet-level debugging](#14-pcap)
15. [Anti-patterns](#15-anti-patterns)
16. [Worked example: diagnosing a phantom packet loss](#16-worked-example)
17. [Pitfalls](#17-pitfalls)
18. [Mental models](#18-mental-models)

---

## 1. Thesis

Three claims:

1. **Network failures are misattributed to applications.** A 50-ms RTT increase due to a NIC offload regression looks identical to a 50-ms application slowdown — until you have network observability.
2. **eBPF turned network observability from an art into engineering.** Pre-eBPF, network problems required tcpdump, Wireshark, manual triage. eBPF gives you the same visibility *continuously*, in production, with bounded overhead.
3. **The network's own SLOs matter.** Packet loss < 0.001%, RTT p99 < 5ms intra-zone, DNS resolution < 100ms p99. Without these, you can't tell when the network is the cause.

If your team has had an outage and the postmortem ended with "we restarted the pods and it cleared up; no root cause found" — that was almost certainly a network problem, and you don't have the observability to see it.

---

## 2. The Five Network Signal Sources

```
┌─────────────────────────────────────────────────────────────┐
│  L1  Hardware                NIC, switch, fiber             │
│  L2  Data link               MAC, VLAN, frame errors        │
│  L3  Network                 IP, routing, MTU               │
│  L4  Transport               TCP, UDP, retransmits          │
│  L5+ Session/App             TLS, HTTP, gRPC                │
└─────────────────────────────────────────────────────────────┘

Five places to observe:
  1. /proc/net/* (kernel counters from the host)
  2. eBPF probes (kernel events)
  3. NIC / switch counters (SNMP, sFlow, NetFlow)
  4. Cloud flow logs (VPC flow logs, equivalent)
  5. Synthetic probes (active measurement)
```

Most teams have *some* of (1) and (4); few have (2) systematically; very few have (3) and (5). The richer end is where the next decade of network observability is heading.

---

## 3. TCP-Level Signals

The most common diagnostic targets.

### 3.1 Retransmits

A retransmit means the sender got no ACK and resent. Causes: packet loss, slow receiver, lossy network, broken middleboxes.

```
node_netstat_Tcp_RetransSegs       counter; total retransmits
node_netstat_Tcp_OutSegs           counter; total segments sent

retransmit_rate = rate(retrans) / rate(out)
```

A healthy LAN: < 0.001%. WAN: < 0.1%. Internet: < 1%. Anomalies: any sustained increase.

### 3.2 RTT (round-trip time)

Smoothed RTT estimation per connection (kernel-level).

```
ss -i             # shows per-socket RTT
tcp_smoothed_rtt_ms   # eBPF-derivable; per-flow histogram
```

Useful: RTT distribution per (source, destination) pair. Enables alerts like "p99 RTT to db tier 3× normal."

### 3.3 Window scaling and CWND

For high-bandwidth-delay-product paths (long-distance, fast networks), TCP window scaling matters. Old kernels or misconfigured systems leave windows small; throughput tanks.

```
ss -i  → shows cwnd, rwnd
```

Generally not a daily concern, but a known fix for "why is my US-Asia transfer slow."

### 3.4 SYN backlog

When TCP accept queue overflows, the kernel drops connection requests *silently*.

```
node_netstat_TcpExt_ListenOverflows
node_netstat_TcpExt_ListenDrops
```

If non-zero, you have an accept-queue overflow. Apps see "connection refused" or hangs. Tune `net.core.somaxconn` and the application's listen backlog.

### 3.5 The TCP retransmit alert pattern

```promql
sum(rate(node_netstat_Tcp_RetransSegs[5m])) / sum(rate(node_netstat_Tcp_OutSegs[5m])) > 0.01
```

> 1% retransmit rate sustained = network problem. Page the network / platform team.

---

## 4. Connection-Level Signals

What your sockets are doing.

### 4.1 Connection states

```
ss -s              # summary
node_netstat_Tcp_CurrEstab    # gauge; current ESTABLISHED conns
node_netstat_Tcp_ResetsSent   # RST sent (rejected / abort)
node_netstat_Tcp_AttemptFails # SYN attempted, not completed
```

### 4.2 The CLOSE_WAIT problem

`CLOSE_WAIT` accumulating: the application isn't `close()`-ing sockets. Resource leak. App sees "too many open files" eventually.

```
sum by (state) (node_tcp_socket_states{state="CLOSE_WAIT"})
```

If `CLOSE_WAIT` > a threshold per node, alert.

### 4.3 The TIME_WAIT problem

Many `TIME_WAIT` is usually fine — it's TCP's anti-replay mechanism. Becomes a problem only at very high connection rates (port exhaustion). Tune `net.ipv4.ip_local_port_range`, `tcp_tw_reuse`.

### 4.4 Refused connections

```
connection_refused_total       # app-instrumented
node_netstat_TcpExt_TCPAbortOnNoroute
node_netstat_TcpExt_TCPAbortOnLinger
```

Distinguish: refused (no listener), no route (network broken), reset (RST received).

---

## 5. Kernel Signals

Below TCP, the host kernel has its own observability surface.

### 5.1 Conntrack

For NAT'd / firewalled traffic, the kernel maintains a connection-tracking table. When full, new connections fail silently.

```
nf_conntrack_count             # gauge; entries in table
nf_conntrack_max               # gauge; table size
nf_conntrack_entries_limit_reached  # counter; rejections
```

The metric to alert on: `nf_conntrack_count / nf_conntrack_max > 0.8`. This was the cause of the famous Cloudflare conntrack outage; many teams have repeated it.

### 5.2 Socket buffer overflows

```
node_netstat_Udp_RcvbufErrors      # UDP socket buffer full; packets dropped
node_netstat_TcpExt_TCPRcvCollapsed # TCP buffer reorg (under pressure)
```

UDP buffer overflow is a silent failure mode — the receiving app never sees the packets. Common in metric-scrape paths (high-volume UDP).

### 5.3 Network device errors

```
node_network_receive_errs_total{device}
node_network_transmit_errs_total{device}
node_network_receive_drop_total{device}
node_network_transmit_drop_total{device}
```

Errors / drops at the device level usually indicate hardware issue, MTU mismatch, or driver problem. Alert if non-zero.

### 5.4 Queues: txqueuelen, qdisc

The egress queue. When packets pile up, latency rises (bufferbloat).

```
ip -s link show ...   # qdisc stats
```

Bufferbloat is a sneaky tail-latency cause. `fq_codel` qdisc helps; modern kernels default to it.

### 5.5 NIC offloads (LRO, GSO, GRO, TSO)

The NIC offloads work to the hardware (segmentation, checksumming). When these regress (driver bug, kernel update), throughput drops or RTT increases. Hard to spot without explicit comparison.

```
ethtool -k <iface>      # shows offload state
```

Production network teams keep an inventory of NIC offload settings; regressions are postmortem-worthy.

---

## 6. Flow Logs

The macroscopic view.

### 6.1 NetFlow / sFlow / IPFIX

Switch-emitted flow records: per-flow source/destination, port, bytes, duration. Sampled (typically 1-in-100 to 1-in-10000).

Use cases:
- Top talkers (which hosts move the most data?)
- Traffic patterns (which pairs talk to each other?)
- Security: anomalous traffic destinations.

Tools: NetBox, ntop, Akvorado, Kentik.

### 6.2 VPC Flow Logs (cloud)

AWS VPC Flow Logs / GCP VPC Flow Logs / Azure Network Watcher. Cloud-emitted equivalent of NetFlow.

```
{srcaddr, dstaddr, srcport, dstport, protocol, bytes, packets, action: ACCEPT/REJECT}
```

Useful for:
- Confirming whether traffic was rejected by security groups.
- Top talkers per VPC.
- Cross-AZ traffic accounting (cost!).

### 6.3 The cost dimension

Cross-AZ traffic in AWS costs $0.01/GB. Cross-region, much more. Flow logs make this attributable per-service. *That alone justifies enabling them.*

### 6.4 Sampling

Flow logs are *sampled*. A 1-in-1000 NetFlow sample at high rates loses small flows. Good enough for top-talker analysis; not sufficient for incident-specific debugging.

---

## 7. eBPF as a Network Observability Primitive

The transformation of the field.

### 7.1 What eBPF gives you

eBPF programs attach to kernel events: `kprobe` (kernel functions), `tracepoint`, `uprobe`. For networking:
- Per-flow RTT histograms (bcc: `tcprtt`).
- Retransmit detection per flow.
- Connection lifecycle events.
- Packet-drop attribution.
- L7 protocol detection (HTTP, gRPC, DNS) without parsing all bytes.

All in production, with sub-1% CPU overhead.

### 7.2 Tools

| Tool | What it does |
|---|---|
| **Pixie** | Auto-tracing of HTTP/gRPC/DNS via eBPF; in-cluster store |
| **Cilium / Hubble** | Mesh + flow telemetry via eBPF |
| **Beyla** (Grafana) | Auto-instrumentation via eBPF; emits OTel traces |
| **Inspektor Gadget** | k8s-targeted eBPF debugging tools |
| **bcc / bpftrace** | Lower-level eBPF scripting |
| **Datadog Network Performance Monitoring** | Vendor-managed eBPF NPM |
| **Tetragon** (Cilium) | Security-focused eBPF observability |

### 7.3 The auto-instrumentation pitch

Beyla, Pixie, Datadog NPM: "drop us in, get instant tracing of all your HTTP/gRPC traffic." The pitch is real but partial:

Pros:
- Zero code change.
- Catches calls that escape app instrumentation.
- Works for legacy or third-party services.

Cons:
- L7 parsing in eBPF is best-effort (can't parse encrypted TLS without keys).
- Less rich than app-level OTel (no business attributes, no custom spans).
- Kernel-version sensitive.

The right play: eBPF as a *complement* to app instrumentation, not a replacement. Catches what app instrumentation misses; gives a cluster-wide RED view "for free."

### 7.4 The kernel version requirement

Modern eBPF features (CO-RE, BTF) require kernel 5.x+. In 2026, most production clusters meet this; some legacy environments don't. Verify before betting on eBPF.

---

## 8. DNS: The Silent Failure Surface

DNS is everywhere; DNS observability is rarely set up.

### 8.1 Why it matters

Every cross-service call resolves a hostname. If DNS is slow:
- Your service's connection latency rises.
- Caches mask it for some users; others see full hits.
- DNS itself can be the failure (timeouts, NXDOMAIN).

### 8.2 What to observe

```
dns_lookup_duration_seconds_bucket    # histogram per resolver
dns_lookup_errors_total{type}         # by error type
dns_resolve_total{rcode}              # success/failure breakdown
```

Tools: CoreDNS metrics in k8s; per-pod DNS tracing via eBPF; node_exporter for systemd-resolved.

### 8.3 The k8s DNS surprise

In Kubernetes, in-cluster DNS goes through CoreDNS. CoreDNS bottlenecks affect every service. Common pathologies:
- ndots: 5 (the default) causes DNS retries; high latency.
- conntrack overflow on the DNS NAT path.
- CoreDNS sizing.

Most k8s clusters benefit from `dns-policy` tuning and node-local DNS cache (NodeLocal DNSCache).

### 8.4 The DNS SLO

```yaml
- name: dns_resolution
  metric: dns_lookup_duration_seconds_bucket{le="0.1"}
  target: 0.999
```

99.9% of DNS lookups complete in 100ms. A regression here cascades to every service.

---

## 9. Load Balancer Observability

The L4/L7 distribution layer.

### 9.1 What to observe

Per LB:
- Active connections.
- New connections per second.
- Bytes in/out.
- HTTP status (per code).
- Latency at the LB (separate from origin).
- Upstream health (which backends are healthy?).
- TLS handshake latency / failures.

### 9.2 Common LBs

| LB | Telemetry source |
|---|---|
| AWS ALB / NLB | CloudWatch + ALB access logs (S3) |
| GCP Cloud Load Balancing | Stackdriver + log-based metrics |
| Azure App Gateway / Front Door | Azure Monitor |
| HAProxy | stats endpoint |
| NGINX | stub_status; vts module |
| Envoy (standalone) | OTLP + admin endpoints |
| MetalLB / kube-proxy | host-level metrics |

### 9.3 The "LB lies" pattern

A common confusion: app reports 200ms p99; LB reports 800ms p99. The diff is *queue time at the LB* (request waited in the LB's accept queue). Surfaces only with LB-side metrics.

### 9.4 The TLS termination cost

TLS handshakes (1-RTT or 2-RTT depending on resumption) are part of latency. TLS misconfiguration (no session resumption, no OCSP stapling) adds tens of ms per request. LB-level TLS metrics expose this.

---

## 10. Cross-Zone, Cross-Region, Cross-Cloud Signals

The geographies your packets cross.

### 10.1 Why it matters

- Latency: intra-zone < cross-zone < cross-region < cross-cloud.
- Cost: same.
- Availability: each boundary is a failure domain.

### 10.2 The signals

- **Per-zone RTT histograms** — eBPF-derivable.
- **Cross-zone byte volume** — VPC flow logs.
- **Cross-region byte volume** — flow logs + cloud-provider billing data.
- **Per-region availability** — synthetic probes between regions.

### 10.3 The "stickiness" pattern

For latency-sensitive services, requests stay within a zone where possible. Service-mesh locality routing (Istio's `localityLbSetting`) implements this. Observability surfaces compliance:

```
mesh_request_total{src_zone, dst_zone}
```

Most calls should have `src_zone == dst_zone`. Anomalies indicate routing drift.

### 10.4 The cost dashboard

Cross-AZ + cross-region traffic, per service, costed:

```
service          intra-AZ    cross-AZ    cross-region    cost / month
checkout-svc     2.4 TB      400 GB      0 GB            $4
data-pipeline    50 GB       1.2 TB      900 GB          $130
```

Often surprising. The per-service breakdown drives architecture conversations.

---

## 11. The L4 vs L7 Distinction

Network observability splits on this axis.

| Layer | What you see | Tools |
|---|---|---|
| **L4** | Connections, packets, bytes, TCP states | /proc, eBPF, NetFlow, kernel-level mesh |
| **L7** | HTTP/gRPC requests, status codes, paths | App instrumentation, sidecar proxies, Pixie/Beyla |

### 11.1 When to use which

- **L4** for "is the network broken?" — packet loss, RTT, conntrack.
- **L7** for "is the application broken?" — RED metrics per service / endpoint.

A complete picture needs both. A dashboard with L7 only misses NIC errors; a dashboard with L4 only misses 5xx rates.

### 11.2 The convergence

eBPF tools increasingly span both layers — Pixie does L7 RED on top of L4 events. The historical L4/L7 toolchain split is collapsing.

---

## 12. Multi-Tenancy in Network Telemetry

When multiple teams share the network.

### 12.1 The attribution problem

A node's `node_netstat_*` counters aggregate all pods on the node. Per-pod attribution requires eBPF or per-cgroup accounting.

### 12.2 The eBPF solution

eBPF programs can attribute flows to source / destination cgroups (i.e., pods). Tools like Cilium / Hubble produce per-pod flow telemetry directly.

### 12.3 The chargeback dimension

Cross-AZ traffic per pod / per service / per team → cost attribution. Drives architecture conversations:

> "Your data-pipeline service moves 1.2 TB/day cross-AZ; that's $360/month. Could we co-locate the consumer with the producer?"

---

## 13. The "Who Talks to Whom" Map

The macro view.

### 13.1 Auto-derived from flows

Cilium Hubble UI, Datadog NPM, Pixie, Akvorado all produce a per-service / per-pod / per-pair connection map.

### 13.2 Why it matters

- **Surprise dependencies.** A service the architecture diagram doesn't show.
- **Compliance.** Should the payments service be talking directly to the analytics DB?
- **Capacity.** Top-talker pairs drive cross-AZ cost.
- **Security.** Anomalous destinations.

### 13.3 The dependency drift signal

Compare the talk map this quarter to last:

- New edges → architecture creep.
- Removed edges → deprecated dependency.
- Changed traffic volume → capacity or feature change.

Quarterly review surfaces drift before it becomes a problem.

---

## 14. Pcap-on-Demand and Packet-Level Debugging

The deep-debug tier.

### 14.1 When you need it

Some bugs require packet-level visibility:
- Specific TLS handshake failures.
- Wire-protocol incompatibility.
- Encrypted-payload investigations (with keys).
- Quirky middlebox behavior.

### 14.2 The implementations

- **kubectl debug + tcpdump** sidecar.
- **Pcap-on-demand** services (Wireshark Cloudshark; vendor offerings).
- **Cilium's monitor** for k8s.
- **eBPF pcap** with attribution.

### 14.3 The privacy / compliance constraint

Pcaps capture full payloads (encrypted or not). This is *data collection*. Storage, access control, retention all need governance. Don't enable cluster-wide pcap; it's a legal liability.

The pattern: on-demand, time-limited, scoped to a specific pod, with audit logging.

---

## 15. Anti-Patterns

1. **No network telemetry at all.** Network bugs misattributed to apps.
2. **No retransmit alert.** Lossy paths invisible.
3. **No conntrack monitoring.** Silent connection failures.
4. **No DNS observability.** Cross-service slowness un-localized.
5. **No flow logs.** Cross-AZ cost surprises; topology blind.
6. **eBPF as a black box.** Vendor lock-in; can't debug.
7. **L7 only.** NIC and kernel issues invisible.
8. **L4 only.** Application-level errors invisible.
9. **No LB-side metrics.** "App is fine, LB is slow" is mysterious.
10. **No cross-region cost dashboard.** Bills shock finance.
11. **Cluster-wide pcap.** Compliance violation.
12. **No talk-map review.** Architecture drift unmonitored.
13. **No DNS SLO.** Cascade failures.
14. **No NIC offload tracking.** Driver regressions ship undetected.
15. **No multi-tenant attribution.** Charge-back impossible.

---

## 16. Worked Example: Diagnosing a Phantom Packet Loss

A real-shape investigation.

### 16.1 The symptom

Application team reports intermittent 1-second hangs in calls between two services. Traces show a gap in the span where "no work was happening." Restarting pods doesn't help.

### 16.2 The investigation

1. Check app-level retries → only 0.5% of requests retry.
2. Check service mesh metrics → nothing anomalous.
3. **Check L4: retransmit rate.** `rate(node_netstat_Tcp_RetransSegs[5m])` shows 2% on one specific node.
4. The affected pods are scheduled on that node disproportionately.
5. Check NIC errors: `node_network_receive_errs_total{device}` → non-zero on the same node.
6. Check `ethtool -S`: NIC reports CRC errors.
7. Drain the node; replace the NIC. Problem disappears.

### 16.3 The lesson

Without L4 + NIC observability, this would have been a multi-day mystery. With it, root cause in 30 minutes.

### 16.4 The follow-up

- Add an alert: `node_network_receive_errs_total{device} > 0` per node.
- Add an alert: `tcp_retransmit_rate{node} > 1%`.
- Schedule periodic NIC error dashboard review.
- Document the diagnostic flow in the runbook.

---

## 17. Pitfalls

1. **Attributing network bugs to apps.** Postmortems fail to find root cause.
2. **No retransmit / RTT signals.** Lossy paths invisible.
3. **No conntrack monitoring.** Silent connection failures.
4. **DNS unobserved.** Cross-service slowness mystified.
5. **No flow logs.** Cross-AZ cost a surprise.
6. **Single-layer telemetry.** L4-only or L7-only; missing complement.
7. **No LB metrics.** Queue at LB looks like origin slowness.
8. **eBPF without expertise.** Misuse, blind spots.
9. **No NIC error monitoring.** Hardware degradations cascade.
10. **MTU mismatch undetected.** Fragmentation, performance issues.
11. **No multi-tenant attribution.** Cost / blame attribution impossible.
12. **No talk-map review.** Architecture drift.
13. **Pcap without governance.** Compliance violation.
14. **No cross-region SLO.** Cross-region traffic invisibly fragile.
15. **No baseline.** Anomalies undetectable without prior history.

---

## 18. Mental Models

> **The network is a layer the application doesn't see. Network observability makes it visible.**

> **Retransmits, RTT, conntrack — three metrics every node should expose.**

> **eBPF turned network observability from triage to engineering.**

> **DNS is everywhere; DNS observability is rarely set up. Fix that.**

> **L4 and L7 are complements. Both are necessary.**

> **Flow logs surface cross-AZ cost and topology. Enable them.**

> **Conntrack overflow is a silent failure. Always alert.**

> **The talk map drifts. Quarterly review.**

> **NIC errors cascade upward. Alert on them.**

> **Pcap is powerful and dangerous. Time-limit, scope, audit.**

Now go to `doc 25` (streaming and Kafka observability) — the async data plane that the request-response model misses.
