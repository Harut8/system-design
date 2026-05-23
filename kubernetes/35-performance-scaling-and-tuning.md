# Performance, Scaling, and Tuning: Kubernetes at 5,000–15,000 Nodes

Every Kubernetes cluster works fine until it doesn't. At 50 nodes the defaults are luxurious. At 500 nodes nothing looks broken but the dashboards drift. At 5,000 nodes — the officially supported ceiling — every component is one mistuned knob away from a control-plane stampede. At 15,000 nodes you are off the supported path and everything you do is informed engineering judgement: which APF FlowSchemas to add, when to defrag, how many etcd members can tolerate which RTT, which CRDs are quietly eating watch-cache memory, and what your apiserver does on a Tuesday at 9am when a CI controller wakes up and lists every Pod in every namespace.

This chapter is the operational counterpart to chapters 04 (etcd), 05 (kube-apiserver and APF), 09 (kube-scheduler), 10 (kubelet), and 14 (services / kube-proxy). Those chapters explain *how* the components work; this one explains *where they break under load and what to do about it*. The audience is the staff engineer who has been handed a cluster bigger than they have run before, told the SLO is "fast", and is now staring at `apiserver_request_duration_seconds_bucket{verb="LIST",resource="pods"}` wondering which knob to turn first.

Prerequisites: ch 03 (architecture), ch 04 (etcd internals — Raft, MVCC, compaction, defrag), ch 05 (apiserver — watch cache, APF), ch 08 (client-go — informers and the workqueue), ch 09 (scheduler framework and percentageOfNodesToScore), ch 10 (kubelet syncLoop and PLEG), ch 14 (kube-proxy modes), ch 18 (CoreDNS). We will reference these constantly; this chapter is the place where the tradeoffs from each of them collide on the same control-plane.

References we will return to throughout:
- The Kubernetes scalability SIG charter and SLO definitions (`kubernetes/community/sig-scalability`)
- The official scalability test framework (`kubernetes/perf-tests`, especially `clusterloader2`)
- `cloud-bulldozer/kube-burner` for synthetic load generation
- `kubernetes/kubernetes/staging/src/k8s.io/apiserver/pkg/util/flowcontrol/...` (APF)
- The etcd operations manual (`etcd-io/etcd/Documentation/op-guide/`)
- `perf-dash.k8s.io` for the upstream scalability dashboards

---

## Table of Contents

1. [The Scalability SIG SLOs and What "Supported" Means](#1-the-scalability-sig-slos-and-what-supported-means)
2. [What Scales Linearly, What Doesn't, What is Quadratic](#2-what-scales-linearly-what-doesnt-what-is-quadratic)
3. [The Money Diagram: a 5,000-Node Cluster Reference Architecture](#3-the-money-diagram-a-5000-node-cluster-reference-architecture)
4. [etcd at Scale](#4-etcd-at-scale)
5. [The etcd Defrag Killer Story](#5-the-etcd-defrag-killer-story)
6. [The Watch Cache](#6-the-watch-cache)
7. [API Priority and Fairness: Recap and Tuning](#7-api-priority-and-fairness-recap-and-tuning)
8. [APF at Scale: a Working Recipe](#8-apf-at-scale-a-working-recipe)
9. [kube-proxy at Scale](#9-kube-proxy-at-scale)
10. [Scheduler Throughput](#10-scheduler-throughput)
11. [Controller-Manager and the Workqueue](#11-controller-manager-and-the-workqueue)
12. [Informer Memory: The Hidden Tax](#12-informer-memory-the-hidden-tax)
13. [Network Programming at Scale](#13-network-programming-at-scale)
14. [DNS at Scale](#14-dns-at-scale)
15. [The Pod Churn Problem](#15-the-pod-churn-problem)
16. [Audit at Scale](#16-audit-at-scale)
17. [Profiling: pprof for the Control Plane](#17-profiling-pprof-for-the-control-plane)
18. [Identifying a Noisy Controller](#18-identifying-a-noisy-controller)
19. [The Noisy Neighbor at the Apiserver: an Investigation Playbook](#19-the-noisy-neighbor-at-the-apiserver-an-investigation-playbook)
20. [Backpressure Mechanisms End-to-End](#20-backpressure-mechanisms-end-to-end)
21. [Single-Tenant vs Multi-Tenant Scaling Math](#21-single-tenant-vs-multi-tenant-scaling-math)
22. [Custom CRDs: Scaling Pitfalls](#22-custom-crds-scaling-pitfalls)
23. [Operator Throughput](#23-operator-throughput)
24. [Memory Tuning of apiserver and etcd](#24-memory-tuning-of-apiserver-and-etcd)
25. [The Hot Pod Scheduling Pattern](#25-the-hot-pod-scheduling-pattern)
26. [The Single-Namespace 100k-Objects Anti-Pattern](#26-the-single-namespace-100k-objects-anti-pattern)
27. [The Huge Object Anti-Pattern](#27-the-huge-object-anti-pattern)
28. [Benchmarking and Load Testing](#28-benchmarking-and-load-testing)
29. [Continuous Capacity Planning](#29-continuous-capacity-planning)
30. [Multi-Apiserver Fan-Out and Watch Catch-Up](#30-multi-apiserver-fan-out-and-watch-catch-up)
31. [Kernel and OS Tuning on Busy Nodes](#31-kernel-and-os-tuning-on-busy-nodes)
32. [CPU Pinning, IRQ Affinity, Topology Manager](#32-cpu-pinning-irq-affinity-topology-manager)
33. [Tools and Dashboards](#33-tools-and-dashboards)
34. [Pitfalls and Anti-Patterns](#34-pitfalls-and-anti-patterns)
35. [TL;DR](#35-tldr)

---

## 1. The Scalability SIG SLOs and What "Supported" Means

The first question every staff engineer asks is: "what does Kubernetes *officially* support?" The answer is documented by SIG Scalability and the published thresholds are the limits that the upstream `kubernetes/perf-tests` test suite verifies on every release. They are not arbitrary; every number below corresponds to an actual nightly job somewhere that fails if the next release regresses past it.

### 1.1 The Headline Numbers

```
┌──────────────────────────────────────────────────────────────────────┐
│  KUBERNETES "OFFICIALLY SUPPORTED" SCALABILITY ENVELOPE              │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   Nodes per cluster.................. 5,000                          │
│   Pods per cluster................... 150,000                        │
│   Containers per cluster............. 300,000                        │
│   Pods per node...................... 110                            │
│   Services per cluster............... 10,000                         │
│   Backends per Service (Endpoints)... 5,000                          │
│   Secrets/ConfigMaps per cluster..... 150,000                        │
│   Namespaces per cluster............. 10,000                         │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Two things to understand about these numbers:

1. **They are envelopes, not boundaries.** A cluster at 4,999 nodes is "supported"; at 5,001 nodes you are explicitly off the path. Many real production clusters run 8k, 10k, even 15k nodes — but at that point you have left the warm embrace of upstream testing and every behavior is your problem.

2. **They are *simultaneous* maxima, not independent.** "5,000 nodes" assumes you are *also* at ≤150k pods, ≤300k containers, ≤10k services. A 1,000-node cluster with 1 million pods is not "5x under the limit" — it is *way* outside, because the per-pod overheads (watch events, endpoint slices, scheduler queue depth, informer caches) dominate node count.

The hint everyone misses: the SIG documentation phrases these as "no more than this without expert tuning." That word "expert" is doing a lot of work. The defaults are tuned for the *median* cluster of about 100–500 nodes. Above 1,000 nodes the defaults are wrong; above 5,000 the defaults are wrong in ways that take down the control plane.

### 1.2 The SLO Thresholds

The other half of "supported" is the latency the cluster must maintain at maximum scale. These are the SLOs the perf-tests suite asserts:

```
┌────────────────────────────────────────────────────────────────────────┐
│ SCALABILITY SIG SLOs (asserted by clusterloader2 + density tests)     │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│  (A) Pod startup latency, P99   .................. < 5  s              │
│        Measured from Pod creation to all containers Running            │
│        (Stateless pods; exclusive of image pull on cold node)          │
│                                                                        │
│  (B) API call latency, P99                                             │
│        Namespaced resources, mutating verbs ...... < 1  s              │
│        Cluster-scoped resources, mutating verbs .. < 30 s              │
│        (Reads are similar; the long pole is mutations)                 │
│                                                                        │
│  (C) In-cluster network programming latency, P99                       │
│        Service IP → endpoint reachability ........ < 1  s              │
│        (Time from EndpointSlice update to all                          │
│         kube-proxies reflecting the change)                            │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

These three numbers are the cluster's *contract* with workloads. If you violate (A), batch jobs and short-lived workloads (CI, Knative, KEDA) suffer. If you violate (B), every controller in the cluster slows down because every reconcile loop is API-bound. If you violate (C), users see "I deployed but the LB still points at the old pod" symptoms.

The "30 s" allowance for cluster-scoped mutating writes (e.g., create a Namespace, update a CRD definition) is generous on purpose: those writes are rare, expensive, and not on the request hot path.

### 1.3 The Implicit SLOs Nobody Writes Down

A few that are not officially called out but are de facto:

```
- etcd request latency P99 ............ < 100 ms (writes); < 25 ms (reads)
- Scheduler scheduling-attempt latency.. < 100 ms P99 in steady state
- kubelet syncLoop iteration time ...... < 1 s
- PLEG relist latency .................. < 1 s
- Controller workqueue depth ........... bounded (steady-state ~0)
- DNS lookup latency, in-cluster ....... < 10 ms P99
```

When any of these creeps up, the official SLOs start to follow. Treat them as leading indicators.

### 1.4 What "Expert Tuning" Actually Means

If you internalize one thing in this chapter: "supported" assumes default config. To run a cluster at 5k nodes you will have changed, at minimum:

- APF: added 5–15 custom FlowSchemas for your own controllers and tenant traffic
- etcd: raised quota from 2 GiB → 8 GiB; tuned heartbeat/election; scheduled rolling defrag
- watch cache: raised cache size for Pod, Lease, EndpointSlice
- apiserver: more replicas (5–7), GOGC=80, audit at Metadata level
- scheduler: percentageOfNodesToScore tuned, parallelism raised
- controller-manager: per-controller concurrent-syncs raised
- kube-proxy: replaced with eBPF (Cilium) or moved to IPVS/nftables
- DNS: CoreDNS HPA + NodeLocalDNS DaemonSet
- Node kernel: conntrack, somaxconn, pid_max, file-max raised

That is the "expert tuning" hint, expanded. Every section below picks one of these and goes deep.

---

## 2. What Scales Linearly, What Doesn't, What is Quadratic

Before tuning anything, internalize the asymptotic behavior of every component. Most ops decisions follow from these.

### 2.1 The Scaling Cheat Sheet

```
┌───────────────────────────────────────────────────────────────────────────┐
│                       KUBERNETES SCALING COMPLEXITIES                     │
├───────────────────────────────────────────────────────────────────────────┤
│                                                                           │
│  LINEAR — O(N) — fine for the published envelope                          │
│   - kubelet ↔ apiserver: 1 watch + N node-local pods                      │
│   - Pods per node up to 110 (Linux PID, cgroup, veth scaling)             │
│   - etcd disk usage: ~1–3 KiB encoded per object                          │
│   - Scheduler binds/sec when nodes ≤ 5000 and parallel binds enabled      │
│   - DNS queries vs pods (when NodeLocalDNS used)                          │
│                                                                           │
│  SUPERLINEAR — O(N log N), O(N · S) — manageable, watch closely           │
│   - Endpoint propagation: O(services × endpoints / slice-size)            │
│   - Scheduler score across nodes (capped by percentageOfNodesToScore)     │
│   - Watch fan-out: O(watchers × events)                                   │
│                                                                           │
│  NON-LINEAR — O(N²) regions — must architect around                       │
│   - kube-proxy iptables rule sync: O(services × endpoints)                │
│     - 5k services × 100 endpoints ≈ sec-to-min per sync                   │
│   - Pod inter-pod affinity / anti-affinity: O(P²) on Pod count            │
│   - PodTopologySpread on a large domain: ~O(P · N)                        │
│   - LIST without label selector on huge resource: O(N) per request,       │
│     × clients ⇒ O(C·N) memory transients                                  │
│                                                                           │
│  QUADRATIC OR WORSE — avoid                                               │
│   - Pod anti-affinity at cluster level with many pods                     │
│   - Watch without resourceVersion semantics → repeated LIST storm         │
│   - Conversion webhook on hot read path × number of objects               │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Linear: the Sweet Spots

The Kubernetes design *deliberately* makes the most common operations linear. The kubelet runs one watch on `/api/v1/pods?fieldSelector=spec.nodeName=$NODE`; the apiserver returns only the pods on that node. There is no broadcasting. Per-node cost stays roughly constant as the cluster grows.

Similarly, pods-per-node scales linearly up to 110 because every per-pod resource (network namespace, cgroup hierarchy, conntrack entries, veth pairs, PID slots) is linear and bounded. The 110 limit exists because *above* that, three things break: the kubelet's PLEG relist starts taking > 1 s; conntrack table fills; CNI plugin scan times grow.

### 2.3 Non-Linear: Where Defaults Break

The two killers are **kube-proxy iptables** and **pod anti-affinity**.

**kube-proxy iptables.** Each service VIP becomes a KUBE-SVC chain that hashes packets into one of N KUBE-SEP chains. Per packet, matching is O(N) (with the chain lookup). Worse, `iptables-restore` rewrites every rule for every reconcile, and the rewrite cost is O(M) where M is total rules ≈ services × endpoints. At 5k services × ~10 endpoints each, a single sync touches ~50k rules. The kernel iptables-restore on a busy node can take 5–15 seconds. During that window, no service update propagates on that node. SLO (C) is now at risk.

The fix is structural, not a tuning parameter: switch the data plane to IPVS (kernel hash table, O(1) lookup), nftables (1.31+, also O(1)), or replace kube-proxy entirely with eBPF (Cilium / kpng). §9 covers the tradeoffs in depth.

**Pod anti-affinity.** Default scheduler plugin `InterPodAffinity` runs a check that, for each candidate node, evaluates affinity/anti-affinity terms against every pod that has a matching label. In the worst case this is O(P × N) per scheduling attempt, and P can be tens of thousands. The scheduler then does this for every newly created pod — turning the *scheduler* into a P² engine when many pods of the same controller arrive at once.

The fix is also structural: prefer `topologySpreadConstraints` (which the scheduler can index more cheaply), and avoid cluster-scoped anti-affinity altogether at scale. Use zone-level or rack-level spread instead.

### 2.4 The Pluggable Knobs: percentageOfNodesToScore

The scheduler has one explicit lever to fight non-linearity: `percentageOfNodesToScore`. After Filter eliminates infeasible nodes, Score normally evaluates every feasible node. At 5,000 nodes that is expensive. With `percentageOfNodesToScore: 30`, only 30% of feasible nodes (with a floor of 100) are scored, the rest are randomly skipped. The decision quality drops slightly; the throughput goes up dramatically.

```
SCHEDULER SCORE LATENCY vs CLUSTER SIZE
(synthetic measurement, log scale; from perf-dash.k8s.io style runs)

       │
  100ms│                                              ╱─── 100% score
       │                                         ╱╱╱
   30ms│                                    ╱╱╱╱╱
       │                               ╱╱╱╱╱
   10ms│                          ╱╱╱╱╱           ╱──── 50% score
       │                     ╱╱╱╱╱            ╱╱╱╱
    3ms│                ╱╱╱╱╱             ╱╱╱╱
       │           ╱╱╱╱╱              ╱╱╱╱           ╱── 10% score
    1ms│      ╱╱╱╱╱               ╱╱╱╱           ╱╱╱
       │ ╱╱╱╱╱                ╱╱╱╱           ╱╱╱╱
       └────────────────────────────────────────────────
        100        500       1000      2500      5000  nodes
```

The curve is roughly linear in nodes when scoring 100%, with each percentage knob cutting the slope. The default 50% capped at 100 is a sane starting point; at 5k nodes drop it to 30%, at 10k to 10–15%.

---

## 3. The Money Diagram: a 5,000-Node Cluster Reference Architecture

Before drilling into individual components, here is the topology that this chapter assumes. Every later section is "what you must tune in this diagram to make it work."

```
                          ┌───────────────────────────────────┐
                          │   Cloud / hardware Load Balancer  │
                          │  (TCP, health checks /readyz)     │
                          └───────────────┬───────────────────┘
                                          │ HTTPS :6443
                          ┌───────────────┴───────────────────┐
                          │                                   │
              ┌───────────▼──────┐               ┌────────────▼───┐
              │ kube-apiserver  ×5 (one per control-plane node)  │
              │   32 vCPU, 128 GiB RAM, OS disk + tmp disk only  │
              │   GOGC=80   GOMAXPROCS=32                        │
              │   --max-requests-inflight=3000                   │
              │   --max-mutating-requests-inflight=1000          │
              │   --watch-cache-sizes=pods#3000,leases#5000,     │
              │                       endpointslices#2000        │
              │   --audit-policy=Metadata (default level)        │
              │   --etcd-servers=https://etcd-{0..4}:2379        │
              └───────┬───────────────────────────────────────────┘
                      │ mTLS gRPC :2379
              ┌───────▼──────────────────────────────────────────┐
              │   External etcd cluster   × 5 members            │
              │   16 vCPU, 64 GiB RAM                            │
              │   NVMe data disk (separate from OS)              │
              │   --wal-dir on its own NVMe partition            │
              │   --quota-backend-bytes=8589934592   (8 GiB)     │
              │   --heartbeat-interval=250ms                     │
              │   --election-timeout=2500ms                      │
              │   --snapshot-count=10000                         │
              │   --auto-compaction-retention=5m                 │
              │   Rolling defrag: one member, every 24h          │
              │   Members in same region, 3 AZs, ≤8 ms RTT       │
              └───────────────────────────────────────────────────┘

         kube-controller-manager × 3   |   kube-scheduler × 3
         (leader-elected, the leader   |   (leader-elected;
          actually works; others idle) |    profiles tuned)

                          ┌──────────────────┐
                          │ CoreDNS Deployment│
                          │  HPA min=10 max=50│
                          │  + NodeLocalDNS DS│
                          │    on every node  │
                          └──────────────────┘

                          ┌────────────────────────────────────┐
                          │ Cilium DaemonSet (kube-proxy off)  │
                          │  - eBPF socket LB                  │
                          │  - eBPF host routing                │
                          │  - Hubble for visibility            │
                          └────────────────────────────────────┘

                          ┌────────────────────────────────────┐
                          │ Observability                       │
                          │  - kube-state-metrics SHARDED ×8    │
                          │  - Prometheus + Thanos              │
                          │  - perf-dashboard equivalent        │
                          └────────────────────────────────────┘

         WORKER NODES × 5000
          - Each runs: kubelet, containerd, Cilium agent,
            node-local-dns, fluent-bit (logs), node-exporter,
            ~30–80 pods (well below the 110 limit)
          - sysctl tuned (see §31)
          - cgroup v2; systemd cgroup driver
```

We will return to this diagram by component throughout the chapter. The numbers above are *starting points*, not gospel; the rest of this chapter is the calibration manual.

---

## 4. etcd at Scale

etcd (ch 04) is the heart of every Kubernetes cluster, and at scale it is *the* component you will spend the most time tuning. Every byte stored in Kubernetes is in etcd; every write goes through Raft replication; every watch is an open gRPC stream. The defaults are conservative; at 5k nodes you must change them.

### 4.1 What etcd Holds, by Volume

```
APPROXIMATE etcd CONTENT AT 5,000 NODES (rough, scales linearly)

  Object kind        Count          Size each      Subtotal
  ───────────────    ──────────     ──────────     ────────
  Pods               150,000        4–6 KiB        ~750 MiB
  Endpoints/Slice    50,000         2–4 KiB        ~150 MiB
  ConfigMaps         50,000         1–500 KiB      ~500 MiB
  Secrets            30,000         1–10 KiB       ~150 MiB
  Events (TTL=1h)    ~200,000       0.5 KiB        ~100 MiB
  Leases             ~10,000        0.5 KiB        ~5   MiB
  Nodes              5,000          5–10 KiB       ~40  MiB
  Custom resources   varies         varies         varies
                                                   ────────
                                                   ~1.5–2 GiB live
```

The "live" size matters; on top of it sits MVCC history (every modification creates a new revision), which compaction shrinks but defrag is what physically reclaims. The bbolt database file on disk can easily be 4–8 GiB even when live data is ~2 GiB.

### 4.2 The Quota: Default 2 GiB is Too Small

The single most common etcd misconfiguration at scale is leaving `--quota-backend-bytes` at its default of 2 GiB. When the bbolt database hits the quota, etcd raises an alarm `NOSPACE` and **rejects all writes cluster-wide** until you defrag or clear the alarm. Symptoms: every `kubectl apply` returns `etcdserver: mvcc: database space exceeded`; the control plane appears frozen.

Set explicitly:

```bash
# etcd startup flag (do this in static pod manifest or systemd unit)
--quota-backend-bytes=8589934592   # 8 GiB

# verify current value
ETCDCTL_API=3 etcdctl --endpoints=https://etcd-0:2379 \
  --cacert=/etc/etcd/ca.crt --cert=/etc/etcd/client.crt \
  --key=/etc/etcd/client.key endpoint status -w table
```

At 8 GiB you have ~4× headroom over typical 5k-node live data. Above 8 GiB the in-memory state (mvcc index, watcher fanout) grows; etcd's official recommendation is "stay below 8 GiB, escalate to >8 only with explicit testing." If you genuinely need more, you have an architecture problem to solve (CRDs storing too much, configmaps too large) before adding more storage.

### 4.3 Auto-compaction

etcd keeps every revision of every key. Without compaction, the disk grows forever. Kubernetes asks the apiserver to compact at a configurable interval, but the apiserver itself only requests compaction; etcd does the work.

```bash
# In etcd, configure auto-compaction:
--auto-compaction-mode=periodic
--auto-compaction-retention=5m       # keep 5 minutes of history

# Or by revision count (cluster-size independent):
--auto-compaction-mode=revision
--auto-compaction-retention=1000     # keep 1000 revisions
```

Periodic is the default in kubeadm setups. Five minutes is sane; less than 5m and you risk breaking long-running watchers that resume by `resourceVersion`. More than 30m and the bbolt file grows.

Verify compaction is happening:

```bash
# Look at the metric:
curl -s --cacert ca.crt --cert client.crt --key client.key \
  https://etcd-0:2379/metrics | grep etcd_debugging_mvcc_db_compaction_keys_total

# Or, in etcdctl:
etcdctl endpoint status -w table
# The DB SIZE column should be stable, not monotonically growing.
```

### 4.4 Heartbeat and Election Timeouts

The defaults (`--heartbeat-interval=100ms`, `--election-timeout=1000ms`) assume a sub-millisecond network between members. In real clusters, especially across AZs, the RTT is 1–5 ms and occasional GC pauses or kernel jitter push it higher. A heartbeat miss → spurious leader election → write stall.

Recommended at scale:

```bash
--heartbeat-interval=250ms     # 2.5× default
--election-timeout=2500ms      # 10× heartbeat (etcd requires)
```

The rule of thumb in the etcd docs: **election timeout ≥ 10 × heartbeat interval ≥ 10 × max expected network RTT**. With 250 ms / 2500 ms you tolerate ~25 ms RTT, which is generous for a same-region cluster and survives most GC pauses.

If your members are across regions (don't), election timeout must go higher (5–10 s). At that point you should not be running multi-region etcd; use a single region and replicate at the application layer.

### 4.5 Snapshot Count

`--snapshot-count=N` controls how often etcd snapshots its in-memory state to disk; on snapshot it truncates the Raft log. Default 100,000. At high write rates this fires often, and each snapshot blocks Raft commits briefly. Counterintuitively, lowering snapshot-count can help — more, smaller snapshots smooth the load.

Recommended:

```bash
--snapshot-count=10000   # snapshot every 10k Raft entries
```

At ~100 writes/sec in a busy 5k-node cluster, this is roughly every 100 seconds.

### 4.6 Storage Hardware: NVMe and Separate WAL

etcd is fsync-heavy. Every Raft commit fsyncs the WAL. SSD/SATA latency (~0.5–1 ms fsync) caps your write rate; NVMe (~50–200 µs fsync) gives you headroom.

Best-practice layout on each etcd node:

```
Disk 0  (OS):           /                        ext4
Disk 1  (data, NVMe):   /var/lib/etcd            ext4, mounted noatime
Disk 2  (WAL, NVMe):    /var/lib/etcd/member/wal mount (--wal-dir)
                          - separate physical device
                          - or at minimum a separate LUN/partition
                          - eliminates contention between WAL fsyncs
                            and snapshot/db writes
```

The `--wal-dir` flag pulls the WAL out of the data directory. This is the single biggest disk-side improvement for write-heavy clusters; the data file is large and random-access, the WAL is small and sequential, and they have different fsync patterns. Co-locating them on the same device causes pathological queueing under load.

### 4.7 Network Between Members

```
ETCD MEMBER PLACEMENT GUIDELINES

  ✓ Same region                       (mandatory)
  ✓ ≤ 8 ms RTT between any two       (recommended)
  ✓ Different failure domains          (AZ, rack)
  ✓ Dedicated NIC or dedicated VLAN    (no contention from
                                         workload pods)
  ✗ Across regions                    (don't — election storms)
  ✗ Behind an overlay network         (don't — extra latency)
  ✗ Shared with worker pods           (don't — noisy neighbors)
```

If etcd runs as static pods on dedicated control-plane nodes (the kubeadm pattern), make sure those nodes are tainted so no workload pod lands on them. Five control-plane nodes that *also* run cluster add-ons is asking for noisy-neighbor pain.

### 4.8 Memory Provisioning

etcd's RSS is dominated by three things:

1. The mvcc index (an in-memory btree mapping keys → revision metadata)
2. The watcher fanout state (each watch has a per-key tracking structure)
3. The bbolt mmap (the entire backend file is mmap'd; the page cache reads pages on demand)

Rough sizing: `RSS ≈ 1.5–3 × db_size`. For an 8 GiB db, plan 16–24 GiB RSS, and provide 64 GiB of node memory so the kernel page cache caches the whole file. With less memory, page faults during reads start dominating latency.

Metric to alert on:

```promql
# etcd backend physical size (b — bytes on disk)
etcd_mvcc_db_total_size_in_bytes

# etcd backend "in-use" (after compaction)
etcd_mvcc_db_total_size_in_use_in_bytes
```

The ratio `total_size_in_bytes / total_size_in_use_in_bytes` is your **fragmentation ratio**. When it exceeds ~1.5, defrag is needed.

### 4.9 Useful etcd Metrics Cheat Sheet

```promql
# Raft / commit health
etcd_disk_wal_fsync_duration_seconds        # WAL fsync histogram; P99 > 25 ms is bad
etcd_disk_backend_commit_duration_seconds   # bbolt commit; P99 > 25 ms is bad
etcd_server_leader_changes_seen_total       # should be ~0 in steady state
etcd_server_proposals_failed_total          # failures = leader changes, txn aborts

# Throughput
etcd_server_proposals_committed_total       # rate() ≈ writes/s
grpc_server_handled_total{grpc_method="Range"}    # reads/s

# Storage
etcd_mvcc_db_total_size_in_bytes
etcd_mvcc_db_total_size_in_use_in_bytes
etcd_debugging_mvcc_keys_total              # number of live keys

# Watch
etcd_debugging_mvcc_watcher_total           # open watchers
etcd_debugging_mvcc_events_total            # event rate

# Alarms
etcd_server_has_leader                      # 1 = healthy
etcd_server_health_failures                 # nonzero = bad
```

A starter Prometheus rule set for etcd:

```yaml
groups:
- name: etcd
  rules:
  - alert: EtcdNoLeader
    expr: etcd_server_has_leader == 0
    for: 1m
    labels: { severity: page }

  - alert: EtcdHighFsyncLatency
    expr: |
      histogram_quantile(0.99,
        rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])) > 0.025
    for: 5m
    labels: { severity: page }

  - alert: EtcdDbSizeApproachingQuota
    expr: |
      etcd_mvcc_db_total_size_in_bytes
      / on(instance) etcd_server_quota_backend_bytes > 0.75
    for: 10m
    labels: { severity: page }

  - alert: EtcdHighFragmentation
    expr: |
      etcd_mvcc_db_total_size_in_bytes
      / etcd_mvcc_db_total_size_in_use_in_bytes > 1.5
    for: 1h
    labels: { severity: warn }
    # → time to defrag

  - alert: EtcdLeaderFlapping
    expr: rate(etcd_server_leader_changes_seen_total[10m]) > 0
    for: 10m
    labels: { severity: page }
```

---

## 5. The etcd Defrag Killer Story

This deserves its own section because it is the single most common scalability surprise in Kubernetes operations, and it has taken down clusters belonging to companies you have heard of.

### 5.1 What Defrag Is

bbolt (etcd's storage engine) is a copy-on-write B+tree. Every update writes new pages; old pages are marked free. Compaction (§4.3) tells bbolt that revisions before N are no longer needed, releasing those pages to the free list. But the file on disk does not shrink. Free pages live inside the file, fragmenting it.

Defrag reclaims the free space by rewriting the database file with only live pages. The result is a smaller, contiguous file.

```
BEFORE DEFRAG (8 GiB file, 60% live)             AFTER DEFRAG (4.8 GiB file)

   ┌───────────────────────────────────────┐    ┌──────────────────────┐
   │■■░░■■■░░░■■░■■■░░░■■■░░■■■░░░■■■░■░│    │■■■■■■■■■■■■■■■■■■■■■■│
   │■■░░■■■░░░■■░■■■░░░■■■░░■■■░░░■■■░■░│    │■■■■■■■■■■■■■■■■■■■■■■│
   │■■░░■■■░░░■■░■■■░░░■■■░░■■■░░░■■■░■░│    │■■■■■■■■■■■■■■■■■■■■■■│
   │■■░░■■■░░░■■░■■■░░░■■■░░■■■░░░■■■░■░│    └──────────────────────┘
   └───────────────────────────────────────┘
    ■ = live page    ░ = free page (after compaction but inside file)
```

### 5.2 Why You Have to Do It

Without defrag, the file grows toward the quota, hits `NOSPACE`, and the cluster freezes. You will defrag *eventually*; the question is whether you do it on your schedule or at 3am during an outage.

### 5.3 The Killer Property: Defrag Blocks Writes on That Member

Defrag is a `defragment` RPC on the etcd member. The member rewrites its bbolt file, which can take 30 seconds to 5+ minutes depending on size. **During that time the member does not serve reads or process Raft commits.** Other members continue if a quorum exists; the defragmenting member rejoins when done.

This is fine — *if you defrag one member at a time*.

Defrag two members simultaneously on a 3-member etcd cluster, and you have killed quorum. The cluster is unavailable. Defrag two on a 5-member cluster, and you still have 3 — quorum survives, but you have lost the safety margin (one more failure ⇒ outage). Defrag *all* members at once, and the cluster is down for the duration of defrag.

### 5.4 The War Story

The pattern (which the author has personally witnessed at three different companies, with the names redacted):

```
2:47 PM   Engineer: "etcd_mvcc_db_total_size_in_bytes is at 75% of quota.
                     Let's defrag."
2:48 PM   Engineer runs:
              for ep in etcd-0 etcd-1 etcd-2 etcd-3 etcd-4; do
                  etcdctl --endpoints=https://$ep:2379 defrag &
              done

2:48:30   etcd-0 starts defragging, stops responding.
2:48:31   etcd-1 starts defragging, stops responding.
2:48:32   etcd-2 starts defragging, stops responding.
2:48:32   Quorum lost. apiserver requests start returning 503.
2:48:33   Every kubelet's heartbeat fails. Node status starts
          going Unknown.
2:48:40   The 5 control-plane nodes scream into Prometheus.
          Pager goes off.

2:53     etcd-0 finishes defrag (small db, ~5 min). Rejoins.
2:54     etcd-1 finishes. Quorum restored.
2:54-3:10   Wave of "Node Not Ready" events as kubelets reconnect.
          Some pods evicted. Endpoint thrash. Service traffic
          temporarily drops 30% of traffic to backends that are
          actually fine.

Total user-visible outage: ~10 minutes.
Total time engineer's morning was ruined: rest of the day.
```

### 5.5 The Right Way: Rolling Defrag

```
ROLLING DEFRAG SCHEDULE — 5-member etcd, daily

  Day  Time       Member       Other members
  ───  ────       ──────       ─────────────
  Mon  03:00      etcd-0       etcd-1..4 healthy
                  (wait until member rejoins +
                   wait 15 min stabilization)
  Mon  03:30      etcd-1       etcd-0,2,3,4 healthy
  Mon  04:00      etcd-2       …
  Mon  04:30      etcd-3
  Mon  05:00      etcd-4
  Tue  …          repeat next day

  Constraints:
  - Never two members defragging at once
  - Defrag only when cluster has quorum WITHOUT the target
  - Wait for the defragged member to fully catch up before
    moving to the next
  - Schedule during low-traffic window if possible
```

A simple bash automation, suitable for a cron job or a tiny operator:

```bash
#!/usr/bin/env bash
set -euo pipefail

MEMBERS=("etcd-0" "etcd-1" "etcd-2" "etcd-3" "etcd-4")
ETCDCTL="etcdctl --cacert=/etc/etcd/ca.crt \
                 --cert=/etc/etcd/client.crt \
                 --key=/etc/etcd/client.key"

for m in "${MEMBERS[@]}"; do
  echo "=== Defragging ${m} ==="
  # 1. Confirm quorum without this member
  $ETCDCTL --endpoints=https://"${m}":2379 endpoint health
  $ETCDCTL endpoint status -w table

  # 2. Run defrag with a long timeout
  $ETCDCTL --endpoints=https://"${m}":2379 defrag --command-timeout=10m

  # 3. Verify member health
  for retry in {1..30}; do
    if $ETCDCTL --endpoints=https://"${m}":2379 endpoint health; then
      break
    fi
    sleep 10
  done

  # 4. Confirm DB size dropped
  $ETCDCTL --endpoints=https://"${m}":2379 endpoint status -w table

  # 5. Stabilization window
  sleep 900   # 15 minutes before next member
done

echo "Rolling defrag complete."
```

Production-grade implementations exist (e.g., `etcd-defrag` from the etcd project, or the `etcd-operator` controllers). Use one of those if you can; the script above is illustrative.

### 5.6 The NOSPACE Recovery Procedure

If you hit NOSPACE before the next scheduled defrag:

```bash
# 1. Compact to a recent revision (skip if recently compacted)
REV=$($ETCDCTL endpoint status --write-out="json" | jq -r '.[0].Status.header.revision')
$ETCDCTL compact "$REV"

# 2. Defrag, one member at a time (as in the script above)

# 3. Clear the NOSPACE alarm
$ETCDCTL alarm disarm

# 4. Verify writes succeed
kubectl create configmap test-recovery --from-literal=ok=yes
kubectl delete configmap test-recovery
```

Critical: do *not* `alarm disarm` before the defrag completes — you'll re-trigger NOSPACE within seconds.

### 5.7 Why You Can't Just "Make Defrag Online"

People ask: why doesn't etcd defrag online? Why does it block?

bbolt is mmap'd. Defrag has to rewrite the file *and* swap the mmap atomically. Doing this without stopping reads/writes is essentially building a new storage engine. The etcd team has discussed it; nothing has shipped. Live with the rolling-defrag discipline.

---

## 6. The Watch Cache

The watch cache (covered structurally in ch 05) is an in-memory ring buffer per resource per apiserver, holding the last N events. When a client opens a watch with `resourceVersion=K`, the apiserver tries to serve from the cache; only if the requested version has fallen off the buffer does it fall back to etcd. This is the single biggest read-amplification mitigation in Kubernetes.

### 6.1 The Default and Why It Hurts at Scale

```
DEFAULT WATCH CACHE SIZES (per resource)

  Pods                100 events
  Endpoints/Slice     100 events
  Nodes               100 events
  Events              100 events
  (most others)       100 events
```

At 100 events, a busy cluster with high pod churn will overflow the buffer in milliseconds. Every watch that lags behind (due to network blip, slow client) misses the cache and re-LISTs from etcd — turning each lagging client into a heavyweight LIST request that scans the entire keyspace.

### 6.2 The Memory Math

For each cached event you store:
- The new object (4–10 KiB for a Pod)
- The previous object (for diff watch payloads, also 4–10 KiB)
- A few hundred bytes of metadata (resourceVersion, type)

So an event is ~10–20 KiB on the heap. A 1000-event cache for Pods uses **10–20 MiB per apiserver per resource**. Across all resources, the watch cache is the dominant chunk of apiserver heap.

### 6.3 Tuning at Scale

```bash
# kube-apiserver startup flag
--watch-cache-sizes=pods#3000,endpointslices#2000,leases#5000,nodes#1000,events#500

# Disable cache entirely (don't, ever)
--watch-cache=false
```

Sizing rule of thumb:

```
   watch_cache_size  ≈  (event_rate_per_second_for_resource)
                        × (max_acceptable_client_lag_seconds)
                        × (safety_factor 2x)
```

For Pods in a 5k-node cluster: ~30–100 events/s sustained, with bursts to 500. Max acceptable lag is on the order of 30 s (controllers should reconnect faster). Cache size ≈ 100 × 30 × 2 = 6000 events. We use 3000 in the reference architecture to balance memory; if you have headroom, go higher.

Verify with metrics:

```promql
# Cache hit / miss rate
apiserver_watch_cache_capacity{resource="pods"}
apiserver_watch_cache_size{resource="pods"}
rate(apiserver_watch_cache_events_received_total[5m])

# When a watch falls off cache and resorts to etcd:
rate(apiserver_watch_events_sizes_total{resource="pods"}[5m])
```

Alert on a low hit ratio:

```yaml
- alert: ApiserverWatchCacheChurning
  expr: |
    (apiserver_watch_cache_size{} /
     apiserver_watch_cache_capacity{}) > 0.98
  for: 15m
  labels: { severity: warn }
  annotations:
    summary: "Watch cache for {{$labels.resource}} is at capacity"
```

### 6.4 The Watch Cache Flow

```
WATCH CACHE FLOW (per resource, per apiserver replica)

   ┌─────────┐    1 watch    ┌────────────────────────────┐
   │  etcd   │──────────────▶│  Reflector inside apiserver│
   └─────────┘  (mvcc stream)│  (translates etcd events   │
                             │   to apiserver events)     │
                             └──────────┬─────────────────┘
                                        │ append
                                        ▼
                            ┌──────────────────────────┐
                            │  Watch Cache             │
                            │  (ring buffer, default   │
                            │   100, tuned to 3000)    │
                            │                          │
                            │  [evN-2999 .. evN]       │
                            └──────────┬───────────────┘
                                       │ fan-out
                ┌──────────────────────┼──────────────────────┐
                ▼                      ▼                      ▼
         client A                client B                client C
         (controller)          (kubelet)              (operator)
         watch RV=K1           watch RV=K2            watch RV=K3
            │                     │                     │
            │ if K1 ∈ cache:      │ if K2 < oldest:     │
            │  stream from cache  │  CACHE MISS         │
            │                     │  → LIST from etcd   │
            │                     │  → catch up         │
                                  │  → resume watch
```

### 6.5 The "Disabled Watch Cache" Footgun

`--watch-cache=false` exists. Don't. Every watch becomes an etcd watch passthrough; the apiserver loses its protection against slow clients; etcd's watcher load multiplies by the number of apiserver clients. This is one of those flags that "fixes" a symptom (memory pressure) by introducing a catastrophe (etcd CPU melts). If apiserver memory is the problem, raise apiserver memory, do not disable the cache.

---

## 7. API Priority and Fairness: Recap and Tuning

APF (ch 05) is the kernel scheduler for kube-apiserver requests: it queues incoming work into priority levels, distributes seats fairly via shuffle sharding, and returns `429 Too Many Requests + Retry-After` when overloaded. Pre-APF (`--max-requests-inflight=400 --max-mutating-requests-inflight=200`) was a single global token bucket; one noisy client could starve everyone. APF replaces that with proportional, isolated queues.

### 7.1 The Two Object Types

```
APF = FlowSchema + PriorityLevelConfiguration

   FlowSchema:                 PriorityLevelConfiguration:
   ┌─────────────────┐         ┌──────────────────────────┐
   │ "Who is this    │         │ "How much capacity does  │
   │  request from?  │   ───▶  │  this priority level get?│
   │  Which priority │         │  How many queues?        │
   │  does it go to?"│         │  Hand-size for shuffle   │
   └─────────────────┘         │  sharding?"              │
                               └──────────────────────────┘
```

FlowSchemas match requests by user/SA/groups/resource/verb; each matched request gets a `flowDistinguisher` (e.g., username) used for shuffle sharding inside the priority level.

### 7.2 The Built-In Priority Levels

Kubernetes ships with these PriorityLevelConfigurations, each tuned for a role:

```
  Priority Level             Concurrency Shares    Notes
  ───────────────────────────────────────────────────────────────────
  exempt                     ∞                     System bypass
  system                     30                    System masters
  node-high                  40                    kubelet status
  leader-election            10                    Election leases
  workload-high              40                    Critical workloads
  workload-low               100                   Normal workloads
  global-default             20                    Catch-all
  catch-all                  5                     Last resort
```

"Concurrency shares" are *proportional*. Total seats = `--max-requests-inflight` + `--max-mutating-requests-inflight`. Each priority level gets `shares / total_shares × total_seats`. Under contention, a level with double the shares gets double the seats.

When *uncontended*, a level can burst beyond its share — up to the global cap. That's the "fair when busy, free when not" property.

### 7.3 Shuffle Sharding: How Fairness Survives a Noisy Tenant

Within a priority level, requests with the same `flowDistinguisher` (typically username) hash into a fixed subset of queues (`handSize` of them). A noisy user can saturate *their* subset, but not the whole level — the probability that two noisy users collide on all their queues is `C(handSize, queueCount)^-1`, which is tiny.

```
SHUFFLE SHARDING (handSize=8, queueCount=128)

  User "alice"     ─hash→  queues {3, 17, 31, 44, 60, 75, 92, 110}
  User "bob"       ─hash→  queues {7, 19, 33, 50, 66, 81, 95, 121}
  User "ci-bot"    ─hash→  queues {3, 22, 39, 50, 71, 85, 102, 119}

  Even if alice + ci-bot share queues {3, 50}, they each have 6 other
  queues to work through. The level stays responsive for bob and
  everyone else.
```

The math: with handSize=8 and 128 queues, the probability of *any* two flows sharing more than 3 queues is < 0.01%. APF makes "one bad tenant DOSes the apiserver" essentially impossible — *if* you have set up your FlowSchemas correctly.

### 7.4 The Knobs

For each PriorityLevelConfiguration:

```yaml
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: PriorityLevelConfiguration
metadata:
  name: workload-high
spec:
  type: Limited
  limited:
    nominalConcurrencyShares: 40    # the share weight
    limitResponse:
      type: Queue                    # alternative: Reject (immediate 429)
      queuing:
        queues: 128                  # number of queues
        handSize: 8                  # shuffle-sharding hand
        queueLengthLimit: 100        # per-queue depth before reject
```

For each FlowSchema:

```yaml
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata:
  name: kube-controller-manager
spec:
  priorityLevelConfiguration:
    name: workload-high
  matchingPrecedence: 800     # lower number = higher priority of match
  distinguisherMethod:
    type: ByUser              # or ByNamespace
  rules:
    - subjects:
        - kind: ServiceAccount
          serviceAccount:
            name: kube-controller-manager
            namespace: kube-system
      resourceRules:
        - verbs: ["*"]
          apiGroups: ["*"]
          resources: ["*"]
```

### 7.5 Useful APF Metrics

```promql
# How saturated is each priority level?
apiserver_flowcontrol_request_concurrency_in_use{priority_level=""}
apiserver_flowcontrol_dispatched_seats_total

# Are we queueing?
apiserver_flowcontrol_current_inqueue_requests{priority_level=""}

# Are we rejecting?
rate(apiserver_flowcontrol_rejected_requests_total[5m])

# Wait time at each level (P99)
histogram_quantile(0.99,
  rate(apiserver_flowcontrol_request_wait_duration_seconds_bucket[5m]))
```

Alert on rejection:

```yaml
- alert: ApiserverApfRejections
  expr: rate(apiserver_flowcontrol_rejected_requests_total[5m]) > 0
  for: 5m
  labels: { severity: warn }
  annotations:
    summary: APF rejecting at {{$labels.priority_level}} / {{$labels.flow_schema}}
```

Reject *should* be zero in steady state. Spikes during incidents are expected and protective; sustained nonzero means a controller has gone rogue or your shares are wrong.

---

## 8. APF at Scale: a Working Recipe

The default FlowSchemas mostly work. At scale you should add several. The pattern: every meaningful traffic source gets its own FlowSchema, mapped to a priority level appropriate to its criticality.

### 8.1 The Recipe

```yaml
# 1. Per-kubelet: every kubelet (lots of them at 5k nodes) should be
#    its own flow, distinguished by ServiceAccount/node identity.
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata:
  name: kubelets-by-node
spec:
  priorityLevelConfiguration: { name: node-high }
  matchingPrecedence: 500
  distinguisherMethod: { type: ByUser }
  rules:
    - subjects:
        - kind: Group
          group: { name: "system:nodes" }
      resourceRules:
        - verbs: ["*"]
          apiGroups: ["*"]
          resources: ["*"]
        - verbs: ["*"]
          nonResourceURLs: ["*"]

# 2. kube-controller-manager: high priority, dedicated level
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata:
  name: kube-controller-manager
spec:
  priorityLevelConfiguration: { name: workload-high }
  matchingPrecedence: 700
  distinguisherMethod: { type: ByUser }
  rules:
    - subjects:
        - kind: ServiceAccount
          serviceAccount: { name: kube-controller-manager, namespace: kube-system }
      resourceRules: [{ verbs: ["*"], apiGroups: ["*"], resources: ["*"] }]

# 3. kube-scheduler: high priority, dedicated level
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata:
  name: kube-scheduler
spec:
  priorityLevelConfiguration: { name: workload-high }
  matchingPrecedence: 700
  distinguisherMethod: { type: ByUser }
  rules:
    - subjects:
        - kind: ServiceAccount
          serviceAccount: { name: kube-scheduler, namespace: kube-system }
      resourceRules: [{ verbs: ["*"], apiGroups: ["*"], resources: ["*"] }]

# 4. Tenant operator workloads: isolate by namespace
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: PriorityLevelConfiguration
metadata: { name: tenant-operators }
spec:
  type: Limited
  limited:
    nominalConcurrencyShares: 30
    limitResponse:
      type: Queue
      queuing: { queues: 128, handSize: 8, queueLengthLimit: 50 }
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata: { name: tenant-operators }
spec:
  priorityLevelConfiguration: { name: tenant-operators }
  matchingPrecedence: 1000
  distinguisherMethod: { type: ByNamespace }
  rules:
    - subjects:
        - kind: Group
          group: { name: "system:serviceaccounts" }
      resourceRules:
        - verbs: ["*"]
          apiGroups: ["*"]
          resources: ["*"]
          namespaces: ["tenant-*"]      # the per-tenant convention

# 5. The CI bot (or any noisy thing) gets its own rate-limited bucket
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: PriorityLevelConfiguration
metadata: { name: ci-bots }
spec:
  type: Limited
  limited:
    nominalConcurrencyShares: 5
    limitResponse:
      type: Reject     # don't even queue — fail fast
---
apiVersion: flowcontrol.apiserver.k8s.io/v1
kind: FlowSchema
metadata: { name: ci-bots }
spec:
  priorityLevelConfiguration: { name: ci-bots }
  matchingPrecedence: 900
  distinguisherMethod: { type: ByUser }
  rules:
    - subjects:
        - kind: User
          user: { name: "system:serviceaccount:ci:jenkins" }
      resourceRules: [{ verbs: ["*"], apiGroups: ["*"], resources: ["*"] }]
```

The point: **noisy controllers cannot starve kubelets.** With these FlowSchemas, a CI controller that LISTs every Pod every 10 seconds is contained to its ci-bots level; the kubelets, scheduler, controller-manager all live in different levels with reserved share.

### 8.2 The APF Flow Assignment Diagram

```
INCOMING REQUEST ─▶ ┌─────────────────────────────────────────────┐
                    │  FlowSchemas (evaluated in matchingPrecedence│
                    │   order, lower = checked first)              │
                    └─────────────────────────────────────────────┘
                                          │
            ┌─────────────────┬───────────┴────────┬─────────────────┐
            ▼                 ▼                    ▼                 ▼
       ┌────────┐       ┌──────────┐        ┌──────────────┐    ┌─────────┐
       │exempt  │       │node-high │        │workload-high │    │ci-bots  │
       │∞ seats │       │40 shares │        │40 shares     │    │5 shares │
       └────────┘       │128 queues│        │128 queues    │    │Reject   │
                        │handSize 8│        │handSize 8    │    │type     │
                        └──────────┘        └──────────────┘    └─────────┘
                            │                     │                  │
                            ▼ shuffle-shard       ▼ shuffle-shard    ▼
                        queue[h(user)]         queue[h(user)]    immediate 429
                            │                     │
                            ▼                     ▼
                        ─── worker dispatch (round-robin over queues) ───
                                          │
                                          ▼
                                     handler chain
                                     (auth → admission → etcd)
```

### 8.3 The "Disable APF" Footgun

`--max-requests-inflight=N --max-mutating-requests-inflight=M` are still accepted flags. They set the *total* concurrency that APF then divides. If you set them high without configuring APF FlowSchemas, the apiserver accepts more work overall but distributes it badly: one noisy client still gets a disproportionate share.

The catastrophe: people raise these flags to "fix" 429s, then disable APF (`--feature-gates=APIPriorityAndFairness=false` on old versions) "to remove queueing overhead." Result: single global token bucket again; one bad client DOSes the whole cluster; you have re-created the pre-APF world.

Do not disable APF. Tune it.

---

## 9. kube-proxy at Scale

Services (ch 14) are stable VIPs implemented by kube-proxy on every node. The implementation choice is the single biggest determinant of cluster networking performance at scale.

### 9.1 The Four Data Planes

```
┌─────────────────┬─────────────┬──────────────┬───────────────────────┐
│ Mode            │ Per-packet  │ Per-sync     │ Scales to             │
├─────────────────┼─────────────┼──────────────┼───────────────────────┤
│ iptables        │ O(N)        │ O(M) rewrite │ ~1–2k services        │
│                 │ (chain walk)│ via iptables-│ before sync time      │
│                 │             │  restore     │ exceeds budget         │
│                 │             │              │                       │
│ IPVS            │ O(1)        │ O(Δ)         │ ~10k services         │
│                 │ (hash table)│ incremental  │ comfortably           │
│                 │             │              │                       │
│ nftables        │ O(1)        │ O(Δ)         │ ~10k services         │
│  (k8s 1.31+)    │ (verdict    │ incremental  │ (becoming the         │
│                 │  maps)      │              │  default replacement) │
│                 │             │              │                       │
│ eBPF (Cilium)   │ O(1)        │ O(Δ)         │ effectively unlimited │
│ (kube-proxy     │ socket-level│              │ (no per-packet        │
│  replaced)      │ LB; bypass  │              │  DNAT lookup; LB at   │
│                 │ iptables    │              │  socket open time)    │
└─────────────────┴─────────────┴──────────────┴───────────────────────┘
```

### 9.2 The iptables Math

Each service generates:

```
KUBE-SERVICES (top-level chain, one rule per service)
   ─▶ KUBE-SVC-XXXX (per-service chain)
        ─▶ probabilistic jump to one of:
           KUBE-SEP-AAAA  (probability 1/N)
           KUBE-SEP-BBBB  (probability 1/(N-1))
           KUBE-SEP-CCCC  ...
                ─▶ DNAT to pod IP
```

For S services with average E endpoints each:
- Top-level chain has S rules
- Per-service chains: S, each with E rules
- Per-endpoint chains: S × E, each with 1–2 rules

Total rules ≈ 2 × S × E + S. For 5k services with 10 endpoints: ~100k rules. iptables-restore rewriting 100k rules: 5–15 seconds in the kernel, with locks held the whole time.

```
KUBE-PROXY iptables SYNC TIME vs SERVICE COUNT

  syncProxyRules duration (P99, seconds)
   30s│                                              ╱
      │                                          ╱╱╱
   10s│                                     ╱╱╱╱╱
      │                                ╱╱╱╱╱
    3s│                          ╱╱╱╱╱
      │                    ╱╱╱╱╱
    1s│              ╱╱╱╱╱
      │       ╱╱╱╱╱╱╱
  300ms│ ╱╱╱╱╱
      └────────────────────────────────────────────
       100     500    1k     2k     5k    10k    services
```

### 9.3 IPVS at Scale

IPVS uses a kernel hash table; lookup is O(1). Rules are managed incrementally (one VS or RS at a time), not via full rewrites. The sync time grows with the number of *changes*, not the total rule count.

Enable:

```yaml
# kube-proxy ConfigMap
apiVersion: kubeproxy.config.k8s.io/v1alpha1
kind: KubeProxyConfiguration
mode: ipvs
ipvs:
  scheduler: rr        # round-robin; alternatives: lc, wlc, sh, dh
  syncPeriod: 30s
  minSyncPeriod: 5s
```

IPVS gotchas: it requires the `ip_vs`, `ip_vs_rr`, `ip_vs_wrr`, `ip_vs_sh`, `nf_conntrack` kernel modules loaded. It still uses iptables for some packet handling (KUBE-MARK-MASQ etc.). Migration from iptables→IPVS on running nodes is bumpy; do it during a maintenance window.

### 9.4 nftables: the New Default

Kubernetes 1.31 introduced `mode: nftables`. nftables shares the kernel network filter framework with iptables but uses verdict maps for O(1) dispatch. It is on track to become the default kube-proxy mode (replacing iptables) by 1.34+.

```yaml
# kube-proxy with nftables mode
mode: nftables
nftables:
  syncPeriod: 30s
  minSyncPeriod: 5s
```

The promise: iptables's correctness and rich tooling, IPVS's performance, no separate kernel modules.

### 9.5 eBPF: Replace kube-proxy Entirely

Cilium (ch 16) replaces kube-proxy with eBPF programs attached at the socket layer. The connect() syscall is intercepted; the destination is rewritten to a backend pod IP *before any packet is sent*. There is no DNAT in the packet path; iptables/IPVS rules for services do not exist.

Enable in Cilium:

```yaml
# Cilium Helm values
kubeProxyReplacement: true
k8sServiceHost: <apiserver-vip>
k8sServicePort: 6443
```

Then disable kube-proxy:

```bash
kubectl -n kube-system delete daemonset kube-proxy
kubectl -n kube-system delete configmap kube-proxy
# Clean up leftover iptables rules on each node:
# iptables-save | grep -v KUBE | iptables-restore
```

This is the only mode that scales gracefully past 10k services. It is also the only mode that gives you socket-level visibility (Hubble, in Cilium's case) for debugging service connectivity. At 5k+ nodes, Cilium kpr is the de facto choice for new clusters.

### 9.6 The Metric to Watch

```promql
# kube-proxy sync time (P99)
histogram_quantile(0.99,
  rate(kubeproxy_sync_proxy_rules_duration_seconds_bucket[5m]))
```

If this exceeds 1 s sustained, you have left iptables territory and need to migrate.

### 9.7 The kube-proxy iptables vs IPVS Scaling Diagram

```
SCALING PROFILE: SAME WORKLOAD, DIFFERENT DATA PLANES

  metric ▲
         │
  10 s   │                                  ↗ iptables
         │                              ↗↗
   3 s   │                          ↗↗
         │                       ↗↗
   1 s   │                   ↗↗
         │                ↗↗
 300 ms  │            ↗↗
         │         ↗↗
 100 ms  │     ↗↗                          ╌╌ IPVS (flat)
         │ ↗↗  ───────────────────────────────────  eBPF (flat, ~constant)
         └────────────────────────────────────▶ services
            100   500   1k    2k    5k    10k
```

The "eBPF flat" line is the headline result. The lookups don't happen per packet; they happen at connect() time. After that, the kernel routes the connection like any other socket. At 10k services there is no per-packet penalty.

---

## 10. Scheduler Throughput

The default scheduler (ch 09) is a single goroutine processing pods from a priority queue, one at a time. Even with parallel Score plugins (added in 1.25+), the *binding cycle* serializes through one goroutine. This caps cluster-wide scheduling throughput at roughly **100 binds/second** on a typical 5k-node setup — meaning a burst of 10,000 new pods takes ~100 seconds to fully schedule.

### 10.1 The Throughput Anatomy

```
SCHEDULER PER-POD COST (rough, on a 5k node cluster)

  ┌─────────────────────────────────────────────────────────┐
  │  PreFilter      ~0.5 ms                                  │
  │  Filter (per feasible node, ~1000 nodes)                 │
  │   ─ each filter plugin called per node                   │
  │   ─ ~1–3 ms per pod total (parallelized internally)      │
  │  PostFilter     ~0 ms (skipped if Filter succeeded)      │
  │  PreScore       ~0.5 ms                                  │
  │  Score (over percentageOfNodesToScore × feasible)        │
  │   ─ ~2–5 ms per pod                                      │
  │  Reserve        ~0.1 ms                                  │
  │  Permit         ~0.1 ms                                  │
  │  PreBind        ~0.5 ms (volume binding if needed)       │
  │  Bind           ~3–8 ms (PATCH spec.nodeName, etcd RTT)  │
  └─────────────────────────────────────────────────────────┘

  Total per pod: 8–18 ms
  → 60–120 binds/sec, single-threaded
```

The Bind step is the main bottleneck: it is an apiserver write that must reach quorum in etcd. You cannot avoid it. You can hide it with parallelism.

### 10.2 percentageOfNodesToScore

Already discussed in §2.4. Recap:

```yaml
# KubeSchedulerConfiguration
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
  - schedulerName: default-scheduler
    pluginConfig: []
percentageOfNodesToScore: 30   # at 5k nodes; default 50 capped at 100 nodes
```

Set lower at larger scales; the floor is 5%. The default's "capped at 100 nodes" means at 5k nodes only 100 feasible nodes are scored regardless of percentage — that's actually a reasonable default for many workloads. Override when you have very heterogeneous nodes and need wider exploration.

### 10.3 Parallelism (1.25+)

The scheduler framework's Score phase now runs plugin invocations across goroutines:

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
parallelism: 16   # default 16 since 1.25
```

This parallelizes Score across nodes within one pod's scheduling cycle. It does not parallelize *different* pods' cycles (those still serialize at Bind). Raising above 16 gives diminishing returns; below 16 can leave CPU on the floor.

### 10.4 The Queue + Backoff Dynamics

Unschedulable pods go to the unschedulable subqueue; they get retried with exponential backoff (base 1s, cap 10s). On certain events (node add, scheduling-gate removed, taint update) they get *moved back* to the active queue.

```
SCHEDULER QUEUE STATES

  ┌─────────────┐     PreFilter/Filter/Score
  │ activeQ     │ ─────────────────────────────▶  Bind ─▶ Scheduled
  └─────┬───────┘                                            ▲
        ▲                                                    │
        │ activate (cluster event)                           │ failure
        │                                                    │
  ┌─────┴───────┐         Filter fails (no fit)              │
  │unschedulable│ ◀────────────────────────────────────────  │
  │  Queue      │                                            │
  └─────┬───────┘                                            │
        │ backoff expired                                    │
        ▼                                                    │
  ┌─────────────┐                                            │
  │ backoffQ    │ ──────────▶ activeQ when backoff timer fires
  └─────────────┘
```

Watch for `scheduler_pending_pods{queue="unschedulable"}` rising — that means real workloads cannot find a home.

### 10.5 Bind-Cycle Parallelization (Limited)

Recent scheduler versions parallelize the Bind cycle for *different* pods within the same scheduling round (when no inter-pod constraint forces ordering). This is invisible from configuration; you observe it in `scheduler_scheduling_attempt_duration_seconds` improving as you upgrade.

### 10.6 Scheduler Profiles

You can run multiple scheduler profiles in one process, each tuned differently:

```yaml
apiVersion: kubescheduler.config.k8s.io/v1
kind: KubeSchedulerConfiguration
profiles:
  - schedulerName: default-scheduler
    plugins:
      score:
        enabled: [{ name: NodeResourcesFit }, { name: ImageLocality }]

  - schedulerName: gang-scheduler
    plugins:
      preFilter:
        enabled: [{ name: CoScheduling }]
      permit:
        enabled: [{ name: CoScheduling }]
```

Pods with `spec.schedulerName: gang-scheduler` use the second profile. This is how Volcano/Yunikorn/Kueue plug in.

### 10.7 Scheduler Metrics

```promql
# Throughput
rate(scheduler_pod_scheduling_attempts_total[5m])
rate(scheduler_pod_scheduling_duration_seconds_count[5m])

# Latency by phase
histogram_quantile(0.99,
  rate(scheduler_scheduling_attempt_duration_seconds_bucket[5m]))
histogram_quantile(0.99,
  rate(scheduler_framework_extension_point_duration_seconds_bucket{
        extension_point="Filter"}[5m]))

# Queue health
scheduler_pending_pods{queue="active"}
scheduler_pending_pods{queue="backoff"}
scheduler_pending_pods{queue="unschedulable"}
```

### 10.8 When Throughput Hurts: The Burst Scenario

A common failure: a CI system creates 10,000 short-lived Pods at once. Scheduler binds them at 100/s = 100 seconds to first-pod-scheduled-everywhere. Meanwhile, normal cluster pods queue behind. SLO (A) violated.

Mitigations:
- Use Job with `parallelism` capped at a manageable number
- Use Kueue or a batch scheduler with admission gating
- Use scheduler profiles to give "batch" pods their own scheduler instance

---

## 11. Controller-Manager and the Workqueue

kube-controller-manager runs ~30 built-in controllers (Deployment, ReplicaSet, Node, Endpoints, GC, Job, CronJob, PV/PVC binder, etc.) each with their own informer and workqueue. The leader controller does the work; others stand by.

### 11.1 The Workqueue Heartbeat

```
controller workqueue (per controller)

  events from informer ─▶ rate-limited workqueue
                                │
                                ▼
                          [ key1, key2, key3, ... ]
                                │ Get()
                                ▼
                          worker pool (N goroutines)
                                │ Reconcile(key)
                                ▼
                          success → Forget(key)
                          failure → AddRateLimited(key)   ← exp backoff
```

The two metrics that tell you everything:

```promql
# Workqueue depth: nonzero sustained = falling behind
workqueue_depth{name="..."}

# Workqueue oldest unfinished work age
workqueue_unfinished_work_seconds{name="..."}

# Re-add rate (= failure rate)
rate(workqueue_retries_total{name="..."}[5m])
```

Healthy controllers show `depth=0` and `unfinished_work_seconds < 1`. Sustained nonzero depth means the controller can't keep up with events.

### 11.2 The Concurrency Knobs

Per-controller goroutine pool sizes:

```bash
# kube-controller-manager flags
--concurrent-deployment-syncs=20       # default 5
--concurrent-replicaset-syncs=20       # default 5
--concurrent-statefulset-syncs=10      # default 5
--concurrent-daemonset-syncs=10        # default 2
--concurrent-job-syncs=20              # default 5
--concurrent-endpoint-syncs=20         # default 5
--concurrent-endpointslice-syncs=20    # default 5
--concurrent-namespace-syncs=20        # default 10
--concurrent-service-syncs=10          # default 1
--concurrent-resource-quota-syncs=10   # default 5
--concurrent-serviceaccount-token-syncs=10
--concurrent-gc-syncs=30               # default 20
--node-monitor-grace-period=40s        # how long before Node Unknown
--node-monitor-period=5s
```

Raising these helps **only** if the bottleneck is controller-side. If the apiserver is the bottleneck (visible: `apiserver_request_duration_seconds` is high, APF rejections spike when you raise concurrency), raising doesn't help — it makes apiserver pressure worse and may make things slower.

The general rule: monitor `workqueue_depth` and `apiserver_request_duration` together. Raise concurrency only when depth is sustained-nonzero *and* apiserver is healthy.

### 11.3 Per-Controller Tuning Recipe at 5k Nodes

```
Controller             concurrent-N  Reasoning
─────────────────────  ────────────  ──────────────────────────────────
deployment             20            many user-driven changes
replicaset             20            fans out to many pod creates
endpoints              20            pod churn implies endpoint churn
endpointslice          20            same as endpoints
daemonset              10            updates every node, parallelizable
job                    20            CI workloads burst
serviceaccount-token   10            secret churn at scale
gc                     30            ownerRef graph traversal
node-monitor           default       don't touch — heartbeat-sensitive
```

### 11.4 The Garbage Collector Specifically

The GC controller (ch 36) builds an in-memory graph of every object's ownerReferences. On a 5k-node cluster with 150k pods + 30k secrets + 50k configmaps, that graph has ~500k nodes and millions of edges. GC enumerates every type via discovery → list → watch on cluster start.

Symptoms of GC under stress:

```promql
# GC's "rest mapper" reload after CRD changes
garbage_collector_rest_mapper_refresh_rate

# GC queue depth
workqueue_depth{name="garbage_collector_dependency_graph_builder"}
workqueue_depth{name="garbage_collector_attempt_to_delete"}
```

If GC queue is growing, an orphaned-finalizer storm or a CRD with a long type chain is likely.

### 11.5 The Leader-Election Quirk

Only one kube-controller-manager replica is leader; the others sleep. Therefore:

- You do not gain throughput by adding replicas (it's only HA)
- Leader transitions are expensive: the new leader re-lists all informers (~30 controllers × ~5–50 MB each = several GB of in-flight LIST traffic)
- Frequent leader changes cause apiserver stampedes

Tune the lease duration in low-traffic clusters; at scale, defaults (15s lease, 10s renew) are fine. Watch:

```promql
leader_election_master_status{name="kube-controller-manager"}
rate(leader_election_slowpath_total[5m])
```

---

## 12. Informer Memory: The Hidden Tax

Every controller using client-go (ch 08) keeps an in-memory cache of *every object it watches*. By default each controller has its own informer factory and its own cache. With dozens of controllers running, the same Pod object is duplicated dozens of times.

### 12.1 The Math

A Pod object in memory is ~10–30 KiB (the struct, status, conditions, container statuses, all the strings). 150k Pods × 20 KiB = 3 GiB *per controller* that watches all pods.

Built-in kube-controller-manager controllers share a SharedInformerFactory (one cache, shared by all controllers in the binary). That's why kube-controller-manager itself uses "only" ~3–5 GiB at 5k nodes — not 30×3 GiB.

External operators do NOT share that factory. Each operator binary builds its own informer factory. If you run 50 operators in the cluster, you may have 50 copies of the relevant objects. At scale this is the dominant memory cost on the control plane.

### 12.2 Mitigations

1. **Per-namespace informer scoping** (preferred). controller-runtime supports `cache.Options.Namespaces`:

```go
mgr, _ := ctrl.NewManager(cfg, ctrl.Options{
  Cache: cache.Options{
    DefaultNamespaces: map[string]cache.Config{
      "my-operator-ns": {},
    },
  },
})
```

The operator now caches only its own namespace's objects, not the cluster.

2. **Label-selected informers**: use `cache.Options.ByObject` to watch only labelled objects:

```go
Cache: cache.Options{
  ByObject: map[client.Object]cache.ByObject{
    &corev1.Pod{}: {
      Label: labels.SelectorFromSet(labels.Set{"app.kubernetes.io/managed-by": "me"}),
    },
  },
},
```

3. **Field-selected informers** (server-side): watch only what you need. The server filters; you transfer less.

4. **Trim TransformFunc**: drop unused fields before they hit the cache:

```go
cache.Options{
  DefaultTransform: func(obj interface{}) (interface{}, error) {
    pod := obj.(*corev1.Pod)
    pod.ManagedFields = nil       // huge in SSA-heavy clusters
    pod.Status.ContainerStatuses = nil  // if you don't need them
    return pod, nil
  },
},
```

### 12.3 The Manage-Fields Killer

`metadata.managedFields` from server-side apply is *huge* — for a busy object that has been touched by 5 controllers, it can be 5–20 KiB by itself, often larger than the spec. Every operator that doesn't strip this in its TransformFunc is doubling its cache memory.

Strip it. Always.

---

## 13. Network Programming at Scale

"Network programming latency" is the SLO from §1.2 (C): the time from a Service/EndpointSlice update to all kube-proxies reflecting the change. Several layers contribute.

### 13.1 EndpointSlice Sharding

Before EndpointSlice, every Service had one Endpoints object that listed every backend. For a Service with 5000 endpoints, that object was ~500 KiB; every change re-sent the whole object to every watcher. At scale this was a watch-event storm.

EndpointSlice fixes this by sharding: each EndpointSlice holds up to 100 endpoints (the default; controlled by `--max-endpoints-per-slice` on the controller-manager). A 5000-endpoint Service becomes 50 EndpointSlices. A single backend change updates only the slice containing it.

```
SERVICE WITH 1,000 BACKENDS

  Without EndpointSlice:
    1 × Endpoints object (~100 KiB)
    Every change re-emits the whole object to every kube-proxy
    Total watch traffic per change: O(N × node_count)

  With EndpointSlice (slice size 100):
    10 × EndpointSlice objects (~10 KiB each)
    A backend change updates 1 slice
    Watch traffic per change: O(1 slice × node_count)
```

This is the reason large Services are even feasible. Verify slicing:

```bash
kubectl get endpointslices.discovery.k8s.io \
  -l kubernetes.io/service-name=my-large-service \
  -o custom-columns=NAME:.metadata.name,ENDPOINTS:.endpoints[*].addresses
```

### 13.2 kube-proxy syncProxyRules Duration

```promql
histogram_quantile(0.99,
  rate(kubeproxy_sync_proxy_rules_duration_seconds_bucket[5m]))
```

If P99 > 1 s, the SLO is at risk. Common causes:
- Too many services for iptables mode (see §9)
- A kube-proxy stuck in `iptables-restore` lock contention with other processes (kured, calico-felix)
- Conntrack table full (see §31)

### 13.3 The Service Programming Pipeline

```
Endpoint change (e.g., pod becomes Ready)
   │
   ▼  T=0  ─ kubelet patches pod.status.podIPs
   │
   ▼  T=~5 ms
[endpointslice-controller in kube-controller-manager]
   notices pod, creates/updates EndpointSlice
   │
   ▼  T=~20 ms ─ apiserver writes EndpointSlice
   │
   ▼  watch event fans out
[kube-proxy on every node receives event]
   │
   ▼  T=~100 ms (eBPF / IPVS / nftables)
   ▼  T=~5 s   (iptables at 5k services)
syncProxyRules executes
   │
   ▼
Packet to Service VIP now reaches new pod
```

The SLO budget is 1 s. eBPF/IPVS/nftables stay well under; iptables blows it past ~2k services.

### 13.4 The "TopologyAwareHints" Optimization

When enabled, EndpointSlices include hints about which zones each endpoint serves. kube-proxy on a node prefers same-zone endpoints, saving cross-zone bandwidth. Useful when:
- You're paying for cross-AZ data transfer
- Replicas are balanced enough across zones

Not useful when:
- Service has few endpoints (the algorithm gives up)
- Endpoints are unbalanced across zones

Enable via:

```yaml
apiVersion: v1
kind: Service
metadata:
  annotations:
    service.kubernetes.io/topology-mode: Auto
```

---

## 14. DNS at Scale

CoreDNS (ch 18) is a single binary per replica. The cluster DNS contract is: every pod can resolve `<svc>.<ns>.svc.cluster.local` and (via search paths) shorter aliases.

### 14.1 Why DNS Becomes a Bottleneck

```
Default CoreDNS deployment: 2 replicas behind kube-dns Service VIP

At 5,000 nodes with 100k pods, average 5–20 DNS queries per pod per second:
  → 500,000–2,000,000 QPS cluster-wide
  → 250,000–1,000,000 QPS per CoreDNS replica
  → CoreDNS replica CPU melts
  → DNS timeouts
  → Apps retry → more queries → meltdown
```

### 14.2 NodeLocalDNS: Mandatory at Scale

NodeLocalDNS is a DaemonSet running a local CoreDNS-like cache on every node, listening on a link-local IP (169.254.20.10 by convention). Pods are configured (via the kubelet's DNS config) to resolve via the local cache first; cache misses go to cluster CoreDNS.

```
WITHOUT NodeLocalDNS                    WITH NodeLocalDNS

  pod ─▶ kube-dns service VIP ─▶         pod ─▶ 169.254.20.10 (local) ─▶
        DNAT in kube-proxy ─▶                  cache hit (~90%) ─▶ return
        CoreDNS replica ─▶                     cache miss ─▶ kube-dns VIP
        cache, then upstream                   ─▶ CoreDNS

  Every query: full conntrack +          Most queries: zero conntrack,
  DNAT + service ─▶ pod                  zero kube-proxy involvement
                                          (link-local, kernel routes locally)
```

The conntrack benefit alone is enormous: at 1M QPS, even short-lived DNS conntrack entries can fill the table.

Deploy NodeLocalDNS via the upstream manifest (`kubernetes/cluster-dns/nodelocaldns/...`); configure kubelet to use it via `--cluster-dns=169.254.20.10`.

### 14.3 CoreDNS Tuning

```
# Corefile
.:53 {
    errors
    health { lameduck 5s }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
        ttl 30                    # default 5; raise to reduce QPS
    }
    prometheus :9153
    forward . /etc/resolv.conf {
        max_concurrent 1000        # parallel upstream queries
    }
    cache 30 {
        success 9984 30            # success cache: 30s TTL
        denial 9984 5              # NXDOMAIN: 5s TTL
        prefetch 10 1m 10%         # prefetch popular records
    }
    loop
    reload
    loadbalance
}
```

Important: `prefetch` keeps hot records fresh so they never expire while in use; reduces tail latency. `cache 30` with the explicit success/denial knobs prevents NXDOMAIN flooding (one missing service should not turn into 1M upstream queries).

### 14.4 HPA CoreDNS

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: coredns, namespace: kube-system }
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: coredns
  minReplicas: 10        # not 2
  maxReplicas: 50
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 60 }
```

In a 5k-node cluster, expect 10–30 CoreDNS replicas in steady state, more if your apps are DNS-chatty.

### 14.5 The ndots=5 Trap

The default DNS config inside pods is `ndots=5`. This means any name with fewer than 5 dots is *first tried against every search-path suffix*. A query for `redis.cache.svc.cluster.local` (4 dots) gets tried as:
- `redis.cache.svc.cluster.local.my-ns.svc.cluster.local`
- `redis.cache.svc.cluster.local.svc.cluster.local`
- `redis.cache.svc.cluster.local.cluster.local`
- `redis.cache.svc.cluster.local`

Four extra queries per lookup. At 1M QPS this becomes 5M QPS. Set `dnsConfig.options.ndots: 2` on pods that query FQDNs, or write FQDNs ending with a dot (`redis.cache.svc.cluster.local.`) which skips search paths.

---

## 15. The Pod Churn Problem

"Pod churn" is the rate at which pods are created and deleted. Steady-state churn comes from: HPA scaling, rolling updates, CI Jobs, CronJobs, KEDA-driven autoscaling. Every pod create or delete is:

- 1 apiserver write (Create / Delete)
- N watch events (one per watcher of pods in that namespace)
- 1 scheduler decision (for creates)
- 1 EndpointSlice update (per matching Service)
- 1 kube-proxy syncProxyRules on every node (per matching Service)
- 2–4 kubelet status patches as the pod transitions
- 1 GC controller processing (for deletes with ownerRefs)

At 100 pod-churns per second, that's easily 1000+ apiserver writes per second, plus 10,000+ watch events. This is the apiserver write workload that defines "busy."

### 15.1 Symptoms

```promql
# Pod creation rate
rate(apiserver_request_total{verb="POST",resource="pods"}[5m])

# Pod deletion rate
rate(apiserver_request_total{verb="DELETE",resource="pods"}[5m])

# Pod patch rate (status updates from kubelet)
rate(apiserver_request_total{verb="PATCH",resource="pods",subresource="status"}[5m])

# Etcd write rate (should track the above sum)
rate(etcd_server_proposals_committed_total[5m])
```

### 15.2 Mitigations

1. **Lower-frequency CronJobs**: a CronJob that runs every minute is rarely necessary. Every 5 minutes saves 80% of churn.

2. **Job over many-Pods**: use a Job with `parallelism=N` instead of creating N separate Pods. The Job controller batches.

3. **Use Kueue**: queue-based admission; bursts level off.

4. **Longer terminationGracePeriodSeconds** on rolling updates: instead of killing pods as fast as possible, give old pods time to drain, smearing the churn over seconds rather than ms.

5. **Tune RS maxSurge/maxUnavailable**: surge=25% rolls 25% of pods at a time, not 100%.

6. **Disable unnecessary status patches**: some custom probes patch pod status every second. That's a thousand-per-second churn at scale.

### 15.3 The "CronJob Thundering Herd"

A CronJob with `concurrencyPolicy: Allow` and a `*/5` schedule will fire 12 times per hour. If 200 CronJobs all have `*/5` schedules, every 5 minutes 200 Jobs all create pods *at the same second*. Stagger:

```yaml
spec:
  schedule: "*/5 * * * *"
  startingDeadlineSeconds: 200      # tolerate stagger
  jobTemplate:
    spec:
      activeDeadlineSeconds: 600
```

Or rotate schedules across the cron minute: `0,5,10,... * * * *` for one set, `1,6,11,... * * * *` for another, etc.

---

## 16. Audit at Scale

Audit log records every API request. Verbose levels (`RequestResponse`) record the request body and response body. At scale, this is gigabytes per minute and a privacy hazard.

### 16.1 The Levels

```
Level             What is logged                       Volume
─────             ──────────────                       ──────
None              nothing                              0
Metadata          who/when/what/verb/resource          ~500 B/req
Request           Metadata + request body              ~5–50 KiB/req
RequestResponse   Request + response body              ~10–500 KiB/req
```

In a 5k-node cluster with ~5000 req/s, `RequestResponse` for every request is ~50 MiB/s = ~4 TiB/day of audit log. Untenable.

### 16.2 The Recipe

Default Metadata for everything, RequestResponse only for sensitive verbs on sensitive resources:

```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: ["RequestReceived"]
rules:
  # 1. Always log creation/modification/deletion of Secrets + RBAC at full level
  - level: RequestResponse
    resources:
      - group: ""
        resources: ["secrets", "configmaps"]
      - group: "rbac.authorization.k8s.io"
        resources: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
    verbs: ["create", "update", "patch", "delete"]

  # 2. Always log changes to admission webhooks (sensitive)
  - level: RequestResponse
    resources:
      - group: "admissionregistration.k8s.io"
        resources: ["*"]

  # 3. Don't log secret READS at all (PII leak + volume)
  - level: None
    resources:
      - group: ""
        resources: ["secrets"]
    verbs: ["get", "list", "watch"]

  # 4. Don't log read-only endpoints/leases (volume only)
  - level: None
    resources:
      - group: ""
        resources: ["events"]
      - group: "coordination.k8s.io"
        resources: ["leases"]

  # 5. Everything else: Metadata only
  - level: Metadata
    omitStages: ["RequestReceived"]
```

### 16.3 Audit Backends

- **File backend** with rotation: simple, but rotation pauses writes.
- **Webhook backend**: ships to an external collector. Failure modes: if the webhook is slow, apiserver requests slow down.

For high-throughput clusters use the **dynamic audit sink** (deprecated; replaced by webhook) or ship to a sidecar collector reading from a file.

Critical: a slow audit webhook will *slow every apiserver request*. Set:

```bash
--audit-webhook-batch-max-wait=30s
--audit-webhook-batch-max-size=400
--audit-webhook-batch-buffer-size=10000
--audit-webhook-batch-throttle-qps=10
```

So that webhook failures don't propagate to user-facing latency.

---

## 17. Profiling: pprof for the Control Plane

Every Go-based control-plane component exposes `/debug/pprof` (if `--profiling=true`, the default). When something is slow, profile.

### 17.1 Endpoints

```
/debug/pprof/             # index
/debug/pprof/profile?seconds=30   # CPU profile, 30s sample
/debug/pprof/heap                 # heap snapshot
/debug/pprof/goroutine            # all goroutine stacks
/debug/pprof/mutex                # contended mutexes (if --mutex-profile-fraction>0)
/debug/pprof/block                # blocking goroutines
/debug/pprof/allocs               # all allocations since start
```

### 17.2 Collecting a CPU Profile

```bash
# from a host with kubectl + go tool pprof
kubectl -n kube-system port-forward kube-apiserver-control-plane-0 6443 &

# Get token for SA with right to access /debug/pprof
TOKEN=$(kubectl create token -n kube-system pprof-reader)

# Profile for 30 seconds
curl -k -H "Authorization: Bearer $TOKEN" \
  https://localhost:6443/debug/pprof/profile?seconds=30 \
  > apiserver-cpu.pb.gz

# Analyze
go tool pprof -http=:8080 apiserver-cpu.pb.gz
```

This opens a browser with the flame graph, top function list, source view.

### 17.3 The "Scheduler is Slow" Diagnosis

```bash
# Step 1: collect 30s CPU profile from scheduler
kubectl -n kube-system port-forward kube-scheduler-cp-0 10259 &
curl -k https://localhost:10259/debug/pprof/profile?seconds=30 > sched.pb.gz

# Step 2: open the flame graph
go tool pprof -http=:8080 sched.pb.gz

# Step 3: look for hot plugins. Typical pattern:
#   - 60% CPU in NodeResourcesFit if many nodes, many requests
#   - 30% CPU in InterPodAffinity if pod anti-affinity is heavy
#   - 20% CPU in PodTopologySpread if many spread constraints
#   - <5% CPU in any one plugin: scheduler is healthy
#
# If one plugin dominates, you've found your tuning target.
```

The same pattern applies to apiserver (look for hot encoders, conversion functions, watch fan-out), controller-manager (look for hot reconcilers), etcd (use `etcdctl check perf` and Prometheus metrics, not pprof — etcd profiling is more limited).

### 17.4 Heap Profile

For memory growth issues:

```bash
curl -k https://localhost:6443/debug/pprof/heap > heap.pb.gz
go tool pprof -http=:8080 heap.pb.gz
# In the UI: View → Top to see who's holding memory.
# Common hits at scale:
#   - watch cache buffers (apiserver)
#   - protobuf encoded objects in conversion (apiserver)
#   - informer caches (controller-manager, operators)
```

### 17.5 Goroutine Leak Detection

```bash
curl -k .../debug/pprof/goroutine?debug=1 > gor.txt
# Look for many goroutines stuck in the same place:
#   - "watch.go:... longRunning" with thousands of entries:
#     watches not being closed → client leak
#   - "syscall.epoll_wait" with thousands:
#     too many file descriptors
```

A healthy apiserver has 200–2000 goroutines. Thousands and rising = leak.

---

## 18. Identifying a Noisy Controller

When the apiserver is hot, identifying *who* is hitting it is the first step.

### 18.1 By User / ServiceAccount

```promql
# Top users by request rate
topk(10, sum by (user_agent) (
  rate(apiserver_request_total[5m])
))

# Top users by request *cost* (latency × rate)
topk(10, sum by (user_agent) (
  rate(apiserver_request_duration_seconds_sum[5m])
))
```

The `user_agent` label is set by client-go to "myoperator/v1.0 (linux/amd64) kubernetes/abcdef" — usually enough to identify the binary. If a user-agent does not appear, check the `username` label (available on some metric sets).

### 18.2 By Verb / Resource

```promql
# LIST is expensive; who is LISTing?
topk(10, sum by (resource, user_agent) (
  rate(apiserver_request_total{verb="LIST"}[5m])
))

# Anything LISTing /pods cluster-wide?
sum by (user_agent) (
  rate(apiserver_request_total{verb="LIST",resource="pods",scope="cluster"}[5m])
)
```

A controller LISTing cluster-wide pods more than once a minute is suspicious. Once every leader transition is fine; sustained is broken.

### 18.3 Open Watches

```promql
# Total watches; should be a few thousand
apiserver_registered_watchers
```

If this is >10,000 in a 5k-node cluster, something is leaking watches. Check `apiserver_longrunning_requests` and split by user-agent.

### 18.4 Goroutine and Memory Growth

```promql
# Apiserver memory
process_resident_memory_bytes{job="apiserver"}

# Apiserver goroutines
go_goroutines{job="apiserver"}
```

Both should be flat in steady state. Sustained growth = leak.

### 18.5 The Audit Log as a Forensic Tool

When metrics aren't enough, parse the audit log:

```bash
# Top users by request count in the last hour
cat /var/log/kubernetes/audit.log | jq -r '.user.username' | \
  sort | uniq -c | sort -rn | head -20

# Top user-agents by LIST count
cat /var/log/kubernetes/audit.log | \
  jq -r 'select(.verb=="list") | .userAgent' | \
  sort | uniq -c | sort -rn | head -20

# Worst-offender LISTs (by response duration)
cat /var/log/kubernetes/audit.log | \
  jq -r 'select(.verb=="list") |
         [(.responseStatus.code), .userAgent, .requestURI,
          (.stageTimestamp | fromdate) - (.requestReceivedTimestamp | fromdate)] |
         @tsv' | sort -k4 -n | tail -20
```

Audit at Metadata level is enough to find offenders; you don't need RequestResponse.

---

## 19. The Noisy Neighbor at the Apiserver: an Investigation Playbook

A concrete playbook for "the apiserver is slow, find out why."

### 19.1 Confirm the Symptom

```promql
# Is P99 actually high?
histogram_quantile(0.99,
  sum by (le, verb, resource) (
    rate(apiserver_request_duration_seconds_bucket[5m])
  ))

# Did it just start? When?
# Open Grafana, eyeball the last 24h.
```

### 19.2 Localize: Which Verb? Which Resource?

```promql
# The slowest 5 verb/resource combos right now
topk(5,
  histogram_quantile(0.99,
    sum by (le, verb, resource) (
      rate(apiserver_request_duration_seconds_bucket[5m])
    )))
```

If LIST on pods is slow: probably someone is doing huge LISTs.
If WATCH is slow: watch cache may be churning (§6).
If POST is slow: etcd write latency (§4).

### 19.3 Check APF

```promql
# Are we rejecting?
sum by (priority_level, flow_schema) (
  rate(apiserver_flowcontrol_rejected_requests_total[5m])
)

# Are we queueing?
apiserver_flowcontrol_current_inqueue_requests

# Are seats saturated?
apiserver_flowcontrol_dispatched_seats_total /
apiserver_flowcontrol_request_concurrency_limit
```

If a level is saturated, the FlowSchema mapped to that level has noisy traffic. Identify the FlowSchema → identify the user/SA → fix.

### 19.4 Check etcd

```promql
histogram_quantile(0.99,
  rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m]))
histogram_quantile(0.99,
  rate(etcd_disk_backend_commit_duration_seconds_bucket[5m]))
rate(etcd_server_proposals_failed_total[5m])
```

If etcd is slow, the apiserver appears slow but the fault is downstream. Common causes:
- DB approaching quota; defrag needed
- Disk contention from another process
- Member network blip; leader changing

### 19.5 Check the Watch Cache

```promql
# Cache hit ratio
1 - rate(apiserver_watch_cache_events_dispatched_total{resource="pods"}[5m])
  / rate(apiserver_request_total{verb="WATCH",resource="pods"}[5m])
```

Low hit ratio = watchers are constantly missing the cache and falling back to etcd.

### 19.6 Identify the User

Already covered in §18. The combination of "FlowSchema X is saturated" + "user_agent Y is the top hitter" usually identifies the offender within minutes.

### 19.7 Mitigate

In order of severity:

1. **Throttle the offender**: create a more aggressive FlowSchema (Reject type) targeting their SA.
2. **Roll back their recent deploy**: usually a controller has gone rogue after a deploy.
3. **Scale apiserver up**: more replicas = more total capacity.
4. **Restart the offender**: a leaked-watch operator can be cured with a restart.

---

## 20. Backpressure Mechanisms End-to-End

Kubernetes has *layers* of backpressure. Each catches a different overload pattern.

```
LAYER                       MECHANISM                       FAILURE MODE
─────                       ─────────                       ─────────────
Client (client-go)          Workqueue rate limiter          Workqueue grows
                            (exponential backoff)            then GC eventually

Client (client-go)          Watch reconnect backoff          Watch reconnect storms

Apiserver request           APF queues + 429s                Client retries

Apiserver request           --max-requests-inflight cap     Hard limit, 429

Apiserver write             etcd write latency               apiserver requests
                                                              queue inside handlers

Apiserver watch             Watch cache ring buffer          Client falls behind
                                                              → LIST re-issue

etcd                        Raft commit serialization        Latency rises

etcd                        Watcher events buffer            Slow watcher disconnect
```

### 20.1 The 429 + Retry-After Contract

When APF rejects, the response is:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 1
Content-Type: application/json

{
  "kind": "Status",
  "status": "Failure",
  "message": "too many requests; please try again later",
  "reason": "TooManyRequests",
  "code": 429
}
```

client-go's rate limiter knows about this and obeys `Retry-After`. Your custom HTTP client should too.

### 20.2 Client-Go Workqueue Backoff

```go
// default exponential backoff
workqueue.NewItemExponentialFailureRateLimiter(5*time.Millisecond, 1000*time.Second)
```

Failed reconciles back off 5ms → 10ms → 20ms → ... → up to 1000s. A persistently failing reconciler retries roughly every minute after the first 10 failures.

### 20.3 Watch Event Coalescing

When a watcher is slow, the apiserver doesn't emit every intermediate state. It coalesces: only the latest state of a given key is sent. This caps the watch traffic at "actual change rate" rather than "intermediate event count."

This is invisible to callers, which is fine for reconcile-based controllers (level-triggered). It breaks edge-triggered consumers (e.g., audit pipelines that need every event) — those should not use watch.

---

## 21. Single-Tenant vs Multi-Tenant Scaling Math

A surprise: **1000 tenants × 100 pods each is harder than 100,000 pods in one tenant**.

### 21.1 The Cost Structure

Per-tenant fixed costs (regardless of pod count):

- 1+ Namespace
- N RBAC objects (Role, RoleBinding, ServiceAccount, possibly ClusterRole bindings)
- M NetworkPolicies (likely at least default-deny + a few allow rules)
- 1+ ResourceQuota
- 1+ LimitRange
- 1+ priority class
- 1+ secret per service account
- An entry in every controller's per-namespace informer (if scoped)

Plus the cost in every informer cache (per-controller, per-operator that's tenant-aware) for the namespace listing.

### 21.2 The Math

```
Per-tenant overhead (rough):
  - 5 RBAC objects × 2 KiB           = 10 KiB
  - 3 NetworkPolicies × 1 KiB         = 3 KiB
  - 1 ResourceQuota × 1 KiB           = 1 KiB
  - 1 LimitRange × 0.5 KiB            = 0.5 KiB
  - 5 Secrets (SA tokens) × 1 KiB     = 5 KiB
  - 1 Namespace × 2 KiB               = 2 KiB
                                        ─────
                                        ~22 KiB in etcd per tenant

  1000 tenants:  22 MiB just in metadata
  10000 tenants: 220 MiB
```

Plus the watch fan-out cost: an operator that watches Namespaces lists all 10,000 of them every leader election; that's a 200 MiB LIST.

### 21.3 Multi-Tenant Tuning

- Aggressive per-namespace informer scoping (§12) for tenant operators
- Push tenants into separate clusters at some breakpoint (often ~500–2000 tenants)
- Use vCluster for soft isolation of small tenants (each vCluster is one Pod in the host)
- Centralize cross-tenant policy (Kyverno/Gatekeeper) so it doesn't multiply per tenant

---

## 22. Custom CRDs: Scaling Pitfalls

CRDs (ch 23) feel free to add. They aren't.

### 22.1 The Storage Cost

CRDs are stored as JSON (or YAML-equivalent unstructured), not protobuf. Built-in types use protobuf for ~2–5× smaller encoding.

```
SAME OBJECT, BUILT-IN vs CRD ENCODING

  A Pod-equivalent CR:
    Protobuf: ~3 KiB
    JSON:    ~12 KiB

  10,000 CRs: 120 MiB in etcd vs 30 MiB for the same data as a built-in.
```

Plus the watch-cache memory (§6) is in the JSON-encoded form. Hot CRDs eat watch cache faster than equivalent built-ins.

### 22.2 Conversion Webhooks

If a CRD has multiple versions and `conversion.strategy: Webhook`, every read of the non-storage version triggers a webhook call. At scale:

```
Workflow:
  1. Operator queries CRDs as v1
  2. Storage version is v2
  3. apiserver fetches v2 from etcd
  4. apiserver calls webhook to convert v2 → v1
  5. webhook returns
  6. apiserver returns v1 to operator

Per-read cost:
  - 1 etcd read
  - 1 webhook call (~5–20 ms typical)

At 1000 reads/s for that CRD: 1000 webhook calls/s,
which means the webhook had better be horizontally scaled.
```

Mitigations:
- Make the storage version match what most clients read
- Use `conversion.strategy: None` if all versions are byte-compatible
- Cache aggressively in webhook clients (but cache invalidation is a problem)

### 22.3 Status-Update Storms

A common CRD anti-pattern: status updates on every reconcile, even when nothing changed. With many objects this turns into a write storm.

```go
// BAD: writes every time
controller.UpdateStatus(ctx, obj)

// GOOD: compare first
if !equality.Semantic.DeepEqual(obj.Status, computedStatus) {
    obj.Status = computedStatus
    controller.UpdateStatus(ctx, obj)
}
```

The patch optimization in controller-runtime (`Patch(ctx, obj, client.MergeFrom(old))`) also reduces traffic — only changed fields are sent.

### 22.4 Watch on a Frequently-Mutated CRD

If your operator watches a CRD that updates its status every 5 seconds (a common debugging mistake), every informer in every operator that *also* watches that CRD pays the cost. At 1000 instances and 5s status updates: 200 events/s × N watchers = quickly saturating.

---

## 23. Operator Throughput

Operators are controllers using `controller-runtime`. Their throughput is determined by:

### 23.1 MaxConcurrentReconciles

```go
err := ctrl.NewControllerManagedBy(mgr).
    For(&v1.MyCR{}).
    WithOptions(controller.Options{
        MaxConcurrentReconciles: 10,    // default 1
    }).
    Complete(r)
```

The default is 1, which means *sequential* reconciles. For any operator with > 100 CRs, raise this. At 1000 CRs with 30s reconcile period, MaxConcurrentReconciles=1 gives you a 30-minute lag.

### 23.2 RateLimiter

The default workqueue rate limiter:
- 5ms → 1000s exponential failure backoff
- 10 QPS / 100 burst overall token bucket

For high-throughput operators raise the bucket:

```go
controller.Options{
    RateLimiter: workqueue.NewMaxOfRateLimiter(
        workqueue.NewItemExponentialFailureRateLimiter(5*time.Millisecond, 1000*time.Second),
        &workqueue.BucketRateLimiter{Limiter: rate.NewLimiter(rate.Limit(100), 1000)},
    ),
}
```

### 23.3 Per-Namespace Cache (Already Covered)

See §12.2 — single largest memory saving for tenant-scoped operators.

### 23.4 Operator Metrics

controller-runtime exposes:

```promql
controller_runtime_reconcile_total{controller=""}
controller_runtime_reconcile_errors_total{controller=""}
controller_runtime_reconcile_time_seconds_bucket{controller=""}
workqueue_depth{name=""}
workqueue_unfinished_work_seconds{name=""}
workqueue_longest_running_processor_seconds{name=""}
```

Standard alerts:

```yaml
- alert: OperatorReconcileBacklog
  expr: workqueue_depth{name=~".*"} > 100
  for: 15m
  labels: { severity: warn }

- alert: OperatorReconcileSlow
  expr: |
    histogram_quantile(0.99,
      rate(controller_runtime_reconcile_time_seconds_bucket[5m])) > 5
  for: 15m
  labels: { severity: warn }
```

---

## 24. Memory Tuning of apiserver and etcd

### 24.1 apiserver

The Go GC controls when collection happens via GOGC. Default 100 means "GC when heap doubles." On a big apiserver with 32 GiB heap, that means *huge* mark-sweep phases that hurt P99 latency.

```bash
# Lower GOGC to reduce variance (more frequent, smaller GCs)
GOGC=80 kube-apiserver ...

# Or use the newer GOMEMLIMIT (1.19+ Go) to set a soft heap ceiling:
GOMEMLIMIT=64GiB kube-apiserver ...
```

GOMEMLIMIT prevents OOM kills by triggering GC as the heap approaches the limit, even when GOGC=100 wouldn't yet fire. Recommended for apiserver: GOMEMLIMIT = 80% of cgroup memory limit.

Watch:

```promql
process_resident_memory_bytes{job="apiserver"}
go_memstats_alloc_bytes{job="apiserver"}
go_gc_duration_seconds{quantile="0.99"}
```

### 24.2 etcd

etcd memory components:

```
RSS ≈ bbolt mmap + watcher state + Go heap

  bbolt mmap: ≈ db file size (the kernel page-caches it; etcd accesses
              via mmap, the kernel decides what to keep in RAM)
  watcher state: ~1 KiB per active watcher key
  Go heap: ~500 MiB to a few GiB
```

Memory tuning flags:

```bash
--max-snapshots=5      # how many .snap files to keep on disk
--max-wals=5           # how many .wal files to keep
--snapshot-count=10000  # smaller snapshots, more frequent
--quota-backend-bytes=8589934592
```

The bbolt mmap is the elephant. There is no way to limit it; mmap is the storage engine. If you can't afford the full file in RAM, plan for slower reads (page-fault servicing from disk).

---

## 25. The Hot Pod Scheduling Pattern

CI systems and batch workloads (Spark, Argo Workflows, Tekton) create thousands of short-lived pods per minute. Each pod create is the workload churn from §15, multiplied.

### 25.1 The Symptoms

- Scheduler queue depth rises in bursts
- Apiserver POST /pods rate spikes
- Etcd write rate spikes
- Endpoint update rate spikes (if pods belong to a Service)
- Audit log saturates

### 25.2 The Architectural Fix: Kueue

Kueue is a batch admission queue:

```
   Submit Job ─▶  ClusterQueue (admits when capacity)
                   │ admitted
                   ▼
                 Workload becomes "active"
                   │
                   ▼
                 Pods created (under quota cap)
                   │ runs
                   ▼
                 Workload finishes; releases quota
```

Kueue caps the number of *concurrent* pods, smoothing the churn over time. A 10,000-pod batch of work gets admitted 500 at a time over 30 minutes, instead of all at once.

### 25.3 The Less-Invasive Fix: Job with Parallelism

For one workload at a time:

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: my-batch }
spec:
  parallelism: 100        # at most 100 pods in flight
  completions: 10000      # total work units
  backoffLimit: 3
  template:
    spec:
      containers: [{ name: work, image: my-job }]
      restartPolicy: Never
```

Job controller manages the 100-at-a-time discipline. The apiserver writes 100 pods at a time, not 10000.

---

## 26. The Single-Namespace 100k-Objects Anti-Pattern

A common surprise: a single namespace with 100,000 ConfigMaps (e.g., one per CI run, never cleaned up) is *much* worse for the cluster than 100k ConfigMaps spread across 1000 namespaces.

### 26.1 Why

- A `LIST configmaps -n bigns` returns all 100k objects in one response
- Encoded size: 100k × ~2 KiB = ~200 MiB
- The apiserver must:
  - LIST from etcd or watch cache (200 MiB read)
  - Decode (CPU)
  - Convert from internal to external version (CPU + GC)
  - Encode back to wire format (CPU)
  - Send (network)
- Each concurrent LIST does the same work; OOM ensues quickly

### 26.2 The Symptom

```promql
# Suddenly: apiserver memory spikes; OOM
process_resident_memory_bytes{job="apiserver"}

# Audit log shows:
#  verb=list resource=configmaps namespace=bigns count=100000
```

### 26.3 The Fix

- **Always paginate**: client-go `ListOptions{Limit: 500, Continue: ...}` returns in pages.
- **Use labelSelector**: clients should subscribe to only what they need.
- **Split the namespace**: rotate per-day/per-week namespaces with TTL-cleanup.
- **TTL controller**: use `TTLAfterFinished` on Jobs to auto-delete completed objects.
- **Inform once**: don't re-LIST per-reconcile; use a properly-warmed informer.

### 26.4 Detection

```promql
# Top namespaces by object count (requires kube-state-metrics)
topk(10, sum by (namespace) (kube_configmap_info))
topk(10, sum by (namespace) (kube_secret_info))
topk(10, sum by (namespace, resource) (
  apiserver_storage_objects))
```

Alert on namespace object count > 10,000:

```yaml
- alert: NamespaceTooManyConfigMaps
  expr: sum by (namespace) (kube_configmap_info) > 10000
  for: 1h
  labels: { severity: warn }
```

---

## 27. The Huge Object Anti-Pattern

Single objects > 1 MiB are pathological. The hard cap is etcd's value-size limit (~1.5 MiB after encoding overhead); objects over this are *rejected* at write time. But objects approaching that size are still bad.

### 27.1 What Goes Wrong

- bbolt pages are 4 KiB; a 1 MiB object spans 256 pages, fragmenting the file
- Watch cache stores N copies; one big object = big memory
- Every PATCH triggers a full re-encoding and re-write of the object
- Conversion (built-in vs CRD) is O(size)

### 27.2 Common Offenders

1. **ConfigMaps stuffed with binary data** (use a different backing store; etcd is not a CDN)
2. **Secrets containing entire certificate chains** (split if possible)
3. **CRDs with hundreds of fields, lots of subresources, deeply nested status**
4. **CRDs with embedded objects** (e.g., a CR that holds a full Pod spec inside its spec)

### 27.3 The Hard Limit

```
gRPC max-recv-msg-size: 1.5 MiB default in apiserver
bbolt page limit:       indirectly enforces ~1 MiB per value
Hard observed reject:    objects > ~1.0 MiB after protobuf encoding
                         → ETCDSERVER: request value too large
```

You can raise the apiserver's max:

```bash
--max-request-body-bytes=3145728   # 3 MiB
```

You can raise etcd:

```bash
--max-request-bytes=1572864        # 1.5 MiB (default)
```

But don't. The right fix is "stop putting binary blobs in etcd."

### 27.4 Detection

```promql
# Largest objects by size (only via kube-apiserver internal metrics)
apiserver_storage_objects{resource="configmaps"}    # count, not size

# More directly:
go tool pprof http://apiserver:6443/debug/pprof/heap
# look for huge allocations in storage
```

Or, brute force:

```bash
kubectl get configmaps --all-namespaces -o json | \
  jq -r '.items[] | [.metadata.namespace, .metadata.name,
                     (.data // {} | to_entries | map(.value|length) | add)] | @tsv' | \
  sort -k3 -n | tail -20
```

---

## 28. Benchmarking and Load Testing

Don't tune by guessing. Measure.

### 28.1 kube-burner (CNCF)

`cloud-bulldozer/kube-burner` is a declarative scenario load generator. You write a YAML describing "create 100k pods over 5 minutes, then delete them"; kube-burner runs the scenario and emits Prometheus metrics.

```yaml
jobs:
  - name: pods-density
    jobIterations: 1
    cleanup: true
    objects:
      - objectTemplate: pod.yaml
        replicas: 100000
    qps: 100
    burst: 200
```

Used widely by Red Hat, IBM Cloud, and many cloud vendors for cluster certification.

### 28.2 perf-tests (kubernetes/perf-tests)

The official scalability test suite. Includes `clusterloader2`, which is the framework that asserts the SIG SLOs from §1.

```bash
# Run a 100-pod density test against your cluster
cd ~/perf-tests/clusterloader2
go run cmd/clusterloader.go \
  --testconfig=testing/density/config.yaml \
  --provider=gce \
  --kubeconfig=~/.kube/config \
  --nodes=100
```

The output includes pass/fail on the SLOs, plus detailed timing for each phase.

### 28.3 k6 for HTTP Testing

For testing your own services exposed via Ingress:

```javascript
import http from 'k6/http';

export const options = {
  stages: [
    { duration: '30s', target: 100 },
    { duration: '1m',  target: 500 },
    { duration: '30s', target: 0 },
  ],
};

export default function () {
  http.get('http://my-svc.cluster/');
}
```

k6 also has Kubernetes operator support (`grafana/k6-operator`) to distribute load across the cluster.

### 28.4 What to Measure

Standard scalability test outputs:

```
Pod startup latency (P99) ............ <target>
API call latency (P99) ............... <target> per verb/resource
Network programming latency (P99) .... <target>
Scheduler throughput (binds/s) ....... <target>
Apiserver CPU at peak ................ <target>
Apiserver memory at peak ............. <target>
Etcd disk write rate ................. <target>
Etcd DB size at peak ................. <target>
```

Run the test before and after every meaningful change. If a parameter improves one metric and worsens another, you have to choose.

---

## 29. Continuous Capacity Planning

Performance regressions are gradual. The cluster works fine for 6 months, then one Tuesday it doesn't. Continuous monitoring of leading indicators catches this.

### 29.1 The Dashboard

```
KUBERNETES CONTROL PLANE CAPACITY DASHBOARD

  ┌───────────────────────────────────────────────────────────────┐
  │ apiserver P99 latency by verb (target: <1s namespaced)       │
  │   [graph: 24h, 7d, 30d trend]                                 │
  ├───────────────────────────────────────────────────────────────┤
  │ etcd db size vs quota                                         │
  │   [graph: gauge + trend, 75%/90% alert lines]                 │
  ├───────────────────────────────────────────────────────────────┤
  │ etcd fragmentation ratio                                      │
  │   [graph: 1.0–2.0, alert at 1.5]                              │
  ├───────────────────────────────────────────────────────────────┤
  │ watch cache hit ratio (per resource)                          │
  │   [graph: 0–100%, alert below 95%]                            │
  ├───────────────────────────────────────────────────────────────┤
  │ scheduler pending pods                                        │
  │   [graph: per queue (active/backoff/unschedulable)]           │
  ├───────────────────────────────────────────────────────────────┤
  │ APF rejections by FlowSchema                                  │
  │   [graph: stacked, alert on any sustained nonzero]            │
  ├───────────────────────────────────────────────────────────────┤
  │ kube-proxy syncProxyRules P99                                 │
  │   [graph: per node, alert > 1s]                               │
  ├───────────────────────────────────────────────────────────────┤
  │ Top apiserver clients (request count, request bytes)          │
  │   [table: top 20 user-agents, sortable]                       │
  ├───────────────────────────────────────────────────────────────┤
  │ Pod startup latency (P99)                                     │
  │   [graph: SLO line at 5s]                                     │
  └───────────────────────────────────────────────────────────────┘
```

### 29.2 The Alerting Rules

Beyond the per-component alerts already shown, the top-level ones:

```yaml
groups:
- name: kubernetes-control-plane-slo
  rules:
  - alert: APIServerP99TooHigh
    expr: |
      histogram_quantile(0.99,
        sum by (le, verb) (
          rate(apiserver_request_duration_seconds_bucket{
            verb=~"GET|LIST|POST|PUT|PATCH|DELETE",
            scope="namespace"
          }[5m])
        )) > 1
    for: 15m
    labels: { severity: page }
    annotations:
      summary: API P99 latency above SLO ({{$value}}s)

  - alert: PodStartupSlow
    expr: |
      histogram_quantile(0.99,
        rate(kubelet_pod_start_duration_seconds_bucket[5m])) > 5
    for: 15m
    labels: { severity: warn }

  - alert: SchedulerBacklog
    expr: scheduler_pending_pods{queue="active"} > 100
    for: 10m
    labels: { severity: warn }

  - alert: EtcdQuotaApproaching
    expr: |
      etcd_mvcc_db_total_size_in_bytes
      / on(instance) etcd_server_quota_backend_bytes > 0.75
    for: 30m
    labels: { severity: page }
```

### 29.3 Trend Monitoring

For each metric track:
- 1h, 1d, 7d, 30d trend
- Day-over-day delta
- Week-over-week delta

A 5% week-over-week growth in etcd DB size means linear growth — in 12 weeks you're at 1.6× current size. Plan for it now.

---

## 30. Multi-Apiserver Fan-Out and Watch Catch-Up

A 5k-node cluster runs 5+ apiserver replicas behind a TCP load balancer. Each replica independently watches etcd. Watch fan-out is per-replica.

### 30.1 What Happens on Rolling Restart

```
apiserver-0 ──▶ killed
   │
   │ N clients reconnect — they hash to one of apiserver-1..4
   │
   ▼
apiserver-1..4 each receive ~N/4 new watches
   │
   │ Each client says: watch from resourceVersion=K
   │
   ▼
Each apiserver: is K still in my watch cache?
   - If yes: stream events from K (fast)
   - If no:  send "too old" → client must LIST + restart watch
              ─▶ LIST storm on that apiserver
```

The mitigation: bigger watch cache (§6) makes "still in cache" more likely. Stagger restarts (only one apiserver down at a time) so the rest absorb load gracefully. Pre-warm caches on a new apiserver by waiting until its `apiserver_storage_objects` count stabilizes before adding it to the LB.

### 30.2 The Bookmark Mechanism

Watches receive periodic "bookmark" events containing the current resourceVersion, so clients can track their position without an actual change. This keeps the client's known resourceVersion fresh, so reconnects after a long quiet period don't fail with "too old."

Verify bookmarks are flowing:

```promql
rate(apiserver_request_total{verb="WATCH"}[5m])
```

Hot resources should have continuous watch traffic; if you see periods of zero, bookmarks may be misconfigured.

### 30.3 Load Balancer Choice

The apiserver LB:
- TCP (L4), not HTTP. Watches are long-lived; L7 LBs often misbehave.
- Health-check via `/readyz` (not `/healthz`; `readyz` returns 200 only when the apiserver is ready to serve)
- Connection drain timeout: 5+ minutes (let long-running watches finish gracefully on shutdown)

---

## 31. Kernel and OS Tuning on Busy Nodes

Each Kubernetes node is a Linux box running ~50–110 pods + a hundred system services. Default kernel parameters are tuned for laptops; servers need adjustment.

### 31.1 Network

```
# /etc/sysctl.d/99-kubernetes.conf

# Backlog of incoming connections
net.core.somaxconn = 65535

# Local port range for outgoing connections (default 32768-60999 = ~28k ports)
net.ipv4.ip_local_port_range = 1024 65535

# TIME_WAIT reuse
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15

# Increase max conntrack entries (default 65536; way too low at scale)
net.netfilter.nf_conntrack_max = 1048576
net.nf_conntrack_max = 1048576

# Conntrack TCP timeout
net.netfilter.nf_conntrack_tcp_timeout_established = 86400

# Increase TCP buffer maxima
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 67108864
net.ipv4.tcp_wmem = 4096 65536 67108864

# TCP keepalive (helps reset stale apiserver connections)
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 6

# IPv4 forwarding (needed for kube-proxy and many CNIs)
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
```

Apply with `sysctl --system`. Verify:

```bash
sysctl net.netfilter.nf_conntrack_max
cat /proc/sys/net/netfilter/nf_conntrack_count
```

A node where `nf_conntrack_count` is > 80% of `nf_conntrack_max` will start dropping packets silently.

### 31.2 File Descriptors

```
# /etc/security/limits.d/kubernetes.conf
* soft nofile 1048576
* hard nofile 1048576
* soft nproc  unlimited
* hard nproc  unlimited

# /etc/systemd/system.conf and user.conf
DefaultLimitNOFILE=1048576
DefaultLimitNPROC=1048576

# /etc/sysctl.d/99-kubernetes.conf (continued)
fs.file-max = 2097152
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 1048576
```

The inotify limits matter for kubelet, which watches every pod log file. Default 128 instances is laughably low on busy nodes.

### 31.3 PIDs

```
kernel.pid_max = 4194304
```

Each container is a process; with 110 pods × 5+ processes each, plus system services, you can easily hit the default 32768.

### 31.4 Memory

```
vm.max_map_count = 262144   # for Elasticsearch, Postgres, anything mmap-heavy
vm.swappiness = 0           # don't swap (kubelet refuses to start with swap on by default)
vm.overcommit_memory = 1    # required for many JVM workloads
```

### 31.5 cgroup v2

Modern Kubernetes (1.25+) prefers cgroup v2 (unified hierarchy). Verify:

```bash
stat -fc %T /sys/fs/cgroup/    # cgroup2fs = good

# Boot with cgroup v2:
# /etc/default/grub:
#   GRUB_CMDLINE_LINUX="systemd.unified_cgroup_hierarchy=1"
```

cgroup v2 gives:
- Proper memory.high vs memory.max separation (soft vs hard limits)
- IO controller with hierarchical accounting
- Per-cgroup PSI metrics (pressure stall information)

### 31.6 Verification

A node-config-check script every operator should have:

```bash
#!/usr/bin/env bash
echo "=== Limits ==="
ulimit -n
sysctl net.netfilter.nf_conntrack_max kernel.pid_max fs.file-max

echo "=== Current usage ==="
cat /proc/sys/net/netfilter/nf_conntrack_count
ps -e --no-headers | wc -l
lsof | wc -l 2>/dev/null || ls /proc/*/fd 2>/dev/null | wc -l

echo "=== cgroup version ==="
stat -fc %T /sys/fs/cgroup/

echo "=== Swap ==="
swapon -s
```

---

## 32. CPU Pinning, IRQ Affinity, Topology Manager

For latency-sensitive workloads (real-time, network-intensive, NUMA-aware), pinning matters.

### 32.1 CPU Manager (Static)

```yaml
# /var/lib/kubelet/config.yaml
cpuManagerPolicy: static
reservedSystemCPUs: "0-3"   # system runs here; pods get 4+
```

Pods with `requests.cpu == limits.cpu` and integer CPU requests are pinned to exclusive cores. The kernel scheduler won't move them. Eliminates jitter from CFS quota throttling.

### 32.2 Memory Manager

```yaml
memoryManagerPolicy: Static
```

Allocates memory from the same NUMA node as the assigned CPUs. Reduces cross-NUMA memory latency from ~150ns to ~80ns — meaningful for latency-sensitive workloads.

### 32.3 Topology Manager

```yaml
topologyManagerPolicy: single-numa-node
```

Coordinates CPU manager + memory manager + device manager (e.g., NIC) so all resources for a pod come from the same NUMA node. Without this, a pod might get a CPU on NUMA-0 and a NIC on NUMA-1, with cross-socket traffic on every packet.

### 32.4 IRQ Affinity

```bash
# Pin NIC IRQs to the same NUMA node as the workload cores
# (Example for a 64-core box with NIC on NUMA-0, workload on NUMA-1)
for irq in $(grep "eth0" /proc/interrupts | awk '{print $1}' | sed 's/://'); do
    echo 0000ff00 > /proc/irq/$irq/smp_affinity   # cores 8-15
done
```

Or use the `irqbalance` daemon with hints.

### 32.5 When to Bother

For 90% of clusters, none of the above matters. For the 10% running latency-critical pods (5G workloads, HFT, real-time pipelines), all of it matters.

---

## 33. Tools and Dashboards

### 33.1 Built-in

```bash
# Componentstatus (deprecated but still works for static-pod control-planes)
kubectl get componentstatus

# Raw apiserver metrics
kubectl get --raw /metrics | grep apiserver_request

# All raw endpoints
kubectl get --raw /debug/pprof/

# Apiserver "long-running requests" snapshot
kubectl get --raw /metrics | grep apiserver_longrunning

# Live etcd status
ETCDCTL_API=3 etcdctl endpoint status -w table --cluster
```

### 33.2 External

- **kube-prometheus-stack**: helm chart with Prometheus, Grafana, Alertmanager, kube-state-metrics, all the dashboards. Includes the official "Kubernetes / Compute Resources" and "Kubernetes / API Server" dashboards.

- **perf-dash.k8s.io**: the upstream scalability dashboard. Shows the latest perf-tests results for every Kubernetes release. Reference for "what does normal look like."

- **kube-state-metrics**: emits Prometheus metrics for every K8s object. At scale, **shard it** (multiple replicas each watching a subset of resources):

  ```yaml
  args:
    - --shard=$(POD_INDEX)
    - --total-shards=8
  ```

- **Thanos / Mimir**: long-term Prometheus storage. At 5k nodes you'll generate gigabytes of metrics per day.

- **Hubble** (Cilium): service map + connection-level diagnostics.

- **k9s**: not a perf tool exactly, but invaluable for "what is this cluster doing right now."

### 33.3 Build Your Own SLO Dashboard

The minimum 9 panels:

```
┌──────────────┬──────────────┬──────────────┐
│ apiserver    │ etcd         │ kubelet      │
│ P99 latency  │ db size %    │ P99 syncLoop │
├──────────────┼──────────────┼──────────────┤
│ APF          │ scheduler    │ kube-proxy   │
│ rejections   │ pending pods │ sync time    │
├──────────────┼──────────────┼──────────────┤
│ top users    │ pod startup  │ ns object    │
│ by req rate  │ P99 latency  │ top 10       │
└──────────────┴──────────────┴──────────────┘
```

---

## 34. Pitfalls and Anti-Patterns

The list every staff engineer running scaled Kubernetes will eventually encounter. Some have already appeared in their own sections; here they are consolidated.

### Control-plane

1. **Default APF not tuned**: single misbehaving operator can DoS the control plane. Always add custom FlowSchemas. → §7, §8.

2. **Disabling APF entirely** (`--max-requests-inflight=N` raised, APF disabled): re-creates the pre-APF noisy-neighbor problem. Don't. → §7.

3. **Etcd quota too small** (`--quota-backend-bytes` default 2 GiB): cluster freezes with `NOSPACE` alarm when the db hits the cap. → §4.2.

4. **Not running defrag**: db file grows even when compaction succeeds; eventually hits quota. → §5.

5. **Simultaneous defrag of multiple etcd members**: kills quorum, takes the cluster down. → §5.

6. **Etcd disk shared with other workloads**: page-cache contention, fsync latency spikes, leader changes. Etcd needs dedicated NVMe. → §4.6.

7. **Etcd RTT > 10 ms between members**: heartbeat misses, election storms. → §4.7.

8. **Restoring etcd from a snapshot smaller than live db**: the cluster comes back missing recent writes; controllers fight to converge. Test backups; never restore without understanding what you lose. → ch 32.

9. **`--max-requests-inflight` raised without APF tuning**: total capacity goes up but distribution gets worse. Always tune together. → §8.

### Networking

10. **kube-proxy iptables at > 5k services**: syncProxyRules takes minutes; SLO (C) is violated. Migrate to IPVS/nftables/eBPF. → §9.

11. **kube-proxy not running** (a real failure mode after CNI changes): services have VIPs but no DNAT rules; connections hang. Verify kube-proxy is healthy on every node. → §9.

12. **CoreDNS at 2 replicas under any real load**: amplification + cache thrash. Min 10 in a 5k-node cluster. → §14.

13. **No NodeLocalDNS**: conntrack overflow at scale; DNS becomes the bottleneck. → §14.

14. **Linux conntrack with large LB IP ranges**: each external client connection takes a conntrack slot; bursts saturate. Raise `nf_conntrack_max`. → §31.

15. **Over-aggressive PodPriority preemption thrashing**: high-priority pod evicts low-priority; low-priority rescheduled, runs briefly, evicted again. Tune priority classes; consider scheduler gates. → §10.

### Apiserver

16. **Audit RequestResponse on every secret read**: PII leak (token contents in logs) + multi-GB/min log volume. Use Metadata for reads. → §16.

17. **`--watch-cache-sizes=0`**: every watch hits etcd; etcd CPU melts. → §6.

18. **Long-running watch leaks**: operator opens watches and never closes them; apiserver goroutines accumulate. Detect via `/debug/pprof/goroutine`. → §17.

19. **In-tree audit webhook on every request**: slow webhook → slow apiserver. Use batching. → §16.

20. **Too few apiserver replicas**: 2 replicas means losing one drops you to 1; LB sees connection storms during rolling restarts. Min 3, ideally 5 at scale. → §3.

### Controllers / clients

21. **Informer not deduplicated** (per-controller copies in one operator process): each controller holds its own copy. Use one SharedInformerFactory. → §12.

22. **LIST without pagination of huge resource**: ConfigMaps in a single big namespace → 200 MiB LIST → apiserver OOM. → §26.

23. **Object > 1 MiB**: bbolt thrashes, watches re-send the whole object. Store blobs elsewhere. → §27.

24. **CRD with conversion webhook on the hot path**: every read = webhook call. Make storage version match read version. → §22.

25. **Resource leak in finalizers**: finalizer never removed → object never deleted → etcd grows. → §4, ch 36.

### Workloads

26. **PDB-blocked node drain stalling autoscaler**: cluster-autoscaler can't remove a node because a PDB protects 100% of its pods. Tune PDBs (allow ≥1 disruption). → ch 22, 32.

27. **Per-tenant burst at 100x burst budget**: one tenant submits 100k Jobs at once. Without Kueue/quota, the cluster melts. → §21, §25.

28. **Bare-metal nodes without NIC offloads**: TCP checksum, segmentation, RSS all in CPU. At 10 Gbps a CPU core melts. Enable hardware offloads. → §31.

### Day-2

29. **Systemd unit FD limits too low on big nodes**: kubelet hits `EMFILE` at scale. Raise `DefaultLimitNOFILE`. → §31.

30. **Treating namespaces as a security boundary**: covered in ROADMAP.md, included here because at scale this also becomes a perf problem (each "tenant" namespace costs informer memory across many controllers). → §21.

---

## 35. TL;DR

```
Kubernetes performance, scaling, and tuning is mostly:
 1. Knowing the SIG SLOs and which leading indicators predict them
 2. Knowing what's linear, non-linear, quadratic — and avoiding the latter
 3. Tuning etcd: quota up, defrag rolling, separate WAL disk
 4. Tuning the apiserver: watch cache, APF FlowSchemas, audit at Metadata
 5. Replacing kube-proxy with eBPF (or moving to IPVS/nftables)
 6. Mandating NodeLocalDNS
 7. Raising controller concurrent-syncs but only when needed
 8. Aggressive informer scoping in operators
 9. Kernel tuning: conntrack, FDs, pid_max, somaxconn
10. Continuous capacity dashboards + alerting on leading indicators
```

The cluster that runs 100 nodes without tuning may run 5,000 nodes if you change one or two flags. The cluster that runs 5,000 nodes will not run 15,000 nodes without architectural changes — different operator design, sharded kube-state-metrics, externalized audit, possibly federation. Every chapter in this folder converges here: the apiserver is the door; etcd is the heart; the kubelet is the limb; everything in between is a controller; and scaling all of it means understanding which one is failing first and how to take the load off it.

The five rules you will repeat to yourself, every time something breaks at scale:

1. **Apiserver is the bottleneck.** Until proven otherwise.
2. **Etcd is the bottleneck behind that.** Until proven otherwise.
3. **A noisy controller is the cause.** Find it via APF metrics + audit log.
4. **iptables doesn't scale.** Replace it.
5. **Defaults are for 100 nodes.** At 5,000, every default is wrong.

Internalize those and the rest is detail.
