# System Design Notes

Working notes on systems engineering, written while learning each topic from
primary sources rather than summaries. They go from CPU and OS primitives up
through storage engines, distributed systems, Kubernetes, observability, and
retrieval-augmented generation.

Most chapters end with lab exercises, and several have runnable code in the
repository alongside them.

!!! note "How to read these"

    Each topic directory is ordered numerically and meant to be read in
    sequence -- later chapters assume the earlier ones. Use the navigation on
    the left to follow a track, or the search box (press ++slash++) to jump
    straight to a concept.

## Tracks

<div class="grid cards" markdown>

-   :material-language-python:{ .lg .middle } __Python & Systems Internals__

    ---

    CPU execution model, caches and memory ordering, virtual memory,
    allocators, syscalls and IO, then CPython itself: refcounting, the eval
    loop, GC, the GIL, free-threading, and asyncio internals.

    [:octicons-arrow-right-24: 30 chapters](python-mastery/README.md)

-   :material-database:{ .lg .middle } __Databases & Storage__

    ---

    Storage engine fundamentals, encoding formats, access methods, query
    engines, transactions and concurrency control, B-trees and LSM-trees,
    write-ahead logging, and vector search internals.

    [:octicons-arrow-right-24: 24 chapters](databases/MENTAL_MODEL.md)

-   :material-lan:{ .lg .middle } __Distributed Systems__

    ---

    Consensus, replication, failure detection, and coordination -- the
    staff-level roadmap and its reading map.

    [:octicons-arrow-right-24: Roadmap](distributed-systems/README.md)

-   :material-kubernetes:{ .lg .middle } __Kubernetes & Containers__

    ---

    From Linux namespaces and cgroups up through etcd, the API server,
    scheduler, and kubelet internals, then CNI, Cilium and eBPF, CSI,
    operators, multi-tenancy, and supply-chain security.

    [:octicons-arrow-right-24: 46 chapters](kubernetes/ROADMAP.md)

-   :material-chart-timeline-variant:{ .lg .middle } __SRE & Observability__

    ---

    OpenTelemetry, instrumentation, collection and transport, storage for
    metrics/logs/traces, query layers, SLO engineering, on-call and incident
    response, cardinality and cost control.

    [:octicons-arrow-right-24: 47 chapters](sre-observability/ROADMAP.md)

-   :material-expansion-card-variant:{ .lg .middle } __GPU Observability__

    ---

    DCGM exporter internals, GPU cluster telemetry on Kubernetes, allocation
    and utilization efficiency, hardware failure detection, and observability
    for LLM inference and distributed training.

    [:octicons-arrow-right-24: 23 chapters](gpu-observability/README.md)

-   :material-vector-triangle:{ .lg .middle } __AI & RAG__

    ---

    Embeddings and representation, chunking and document processing, vector
    indexes, hybrid retrieval and reranking, and evaluation methodology --
    with document-processing and golden-set labs.

    [:octicons-arrow-right-24: Reading map](ai-rag/README.md)

-   :material-hammer-wrench:{ .lg .middle } __Design Practice__

    ---

    Design tasks stated at four scale tiers (10k → 10m), worked solutions, and
    reference implementations for each tier: Twitter search, Instagram feed,
    a distributed counter, and FastAPI RBAC.

    [:octicons-arrow-right-24: Start with the tasks](tasks/twitter-search.md)

</div>

## Also here

- [__System Design Guide__](SYSTEM-DESIGN-GUIDE.md) — the cross-cutting
  reference that ties the tracks together.
- [__Kubernetes Labs__](k8s-learn/README.md) — hands-on task sheets with
  manifests, separate from the Kubernetes theory track.

---

The source lives at
[github.com/Harut8/system-design](https://github.com/Harut8/system-design).
Corrections are welcome via issues or pull requests.
