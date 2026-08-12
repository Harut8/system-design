# Appendix E — Deployment, pipelines, and compute cost

> **Prerequisites:** [`appendix-d-doc-processing-benchmarks.md`](appendix-d-doc-processing-benchmarks.md)
> (which tools to run — this appendix is about where they run and what that costs),
> [`02-chunking-and-document-processing.md`](02-chunking-and-document-processing.md) (the pipeline
> model; §3's parsing tiers are the single biggest input to the cost model in §9),
> [`03-indexing-and-vector-stores.md`](03-indexing-and-vector-stores.md) (index internals — the
> vector store is the one stateful component and it constrains the whole topology).
>
> **Feeds into:** [`15-ingestion-pipelines-and-freshness.md`](15-ingestion-pipelines-and-freshness.md)
> (the chapter this appendix front-runs: §8's pipeline taxonomy and §8.4's DAG are its skeleton),
> [`11-token-accounting-and-cost.md`](11-token-accounting-and-cost.md) (per-request cost
> attribution — §9 gives the infrastructure side of the same ledger),
> [`12-serving-latency-and-caching.md`](12-serving-latency-and-caching.md) (§7.2's prefill math is
> where RAG latency budgets actually get spent).
>
> **Do not rewrite the substrate.** Kubernetes mechanics, Compose mechanics, autoscaling internals,
> GPU telemetry and FinOps patterns already exist in this repo at depth — §11 maps every one of them.
> This appendix writes only the RAG-specific delta on top.
>
> **THESIS:** the deployment question people ask is "Kubernetes or Docker Compose?" That is the
> wrong question and it has a boring answer (§3: Compose until one box stops being enough, which is
> later than you think). The questions that decide whether a RAG system is operable are: **is the
> write path separated from the read path** (§2), **what actually needs a GPU and how many** (§7),
> **which of the four different things called "the pipeline" are you building** (§8), and **which
> component dominates your bill** (§9 — it is almost never the one you're optimizing). A RAG system
> that gets those four right runs fine on Compose. One that gets them wrong is not rescued by
> Kubernetes; it is the same mistake with a control plane in front of it.
>
> **Rung note.** Every figure here is **rung 3 — sourced or derived**, not measured by me. Where a
> number is derived, the formula is printed next to it so you can redo the arithmetic with your own
> inputs. Vendor prices are a July 2026 snapshot and move monthly. §14 is the part with a shelf life.

---

## Contents

1. [How to use this appendix](#1-how-to-use-this-appendix)
2. [What a RAG system actually is, deployment-wise](#2-what-a-rag-system-actually-is-deployment-wise)
3. [The deployment ladder — Compose to Kubernetes](#3-the-deployment-ladder--compose-to-kubernetes)
4. [Docker Compose in production](#4-docker-compose-in-production)
5. [Kubernetes topology for RAG](#5-kubernetes-topology-for-rag)
6. [The model-serving layer](#6-the-model-serving-layer)
7. [GPU: what you need, how many, and how to schedule it](#7-gpu-what-you-need-how-many-and-how-to-schedule-it)
8. [Where pipelines get built](#8-where-pipelines-get-built)
9. [Compute cost](#9-compute-cost)
10. [Managed and serverless alternatives](#10-managed-and-serverless-alternatives)
11. [Reference architectures](#11-reference-architectures)
12. [Release and rollout — the RAG-specific hazards](#12-release-and-rollout--the-rag-specific-hazards)
13. [Anti-patterns](#13-anti-patterns)
14. [Mental models — the compressed set](#14-mental-models--the-compressed-set)
15. [Cross-reference map](#15-cross-reference-map)

---

## 1. How to use this appendix

Read §2 always — it is the decomposition everything else depends on, and it is where most
architectures go wrong. Then jump:

| If you are… | Read |
|---|---|
| Deciding Compose vs Kubernetes | §3, §4 |
| Sizing hardware / writing a budget | §7.2–§7.4, §9 |
| Being asked "how many GPUs do we need" | §7.2, §7.3 — there is a formula, use it |
| Building the ingestion pipeline | §8 |
| Choosing an orchestrator | §8.2, §8.3 |
| Cutting the bill | §9.6 (ranked by leverage, not by how satisfying the work is) |
| Wondering whether to buy instead | §10 |
| Copying a starting point | §11 |

**The numbers are inputs to arithmetic, not conclusions.** Every cost figure in §9 is produced by a
printed formula with stated assumptions. Substitute your corpus size, your token counts, and your
negotiated rates; the ordering of magnitudes will survive, the absolute values will not.

---

## 2. What a RAG system actually is, deployment-wise

A RAG system is not one service. It is **four paths with four different scaling laws, four
different failure modes, and four different hardware profiles**, which people deploy as one thing
and then cannot operate.

| Path | What it does | Traffic shape | Latency SLO | Hardware | Scales with |
|---|---|---|---|---|---|
| **Write path** (ingest) | parse → chunk → embed → upsert | Batch spikes; bursty; hours-long | None (throughput SLO instead) | CPU-heavy, GPU-optional, preemptible-friendly | Corpus size × change rate |
| **Read path** (query) | embed query → retrieve → rerank → generate | Interactive; diurnal; spiky | P95 in seconds | Small GPU or none; API-bound | QPS |
| **Model path** | embedding / reranker / generator inference | Called by both other paths | Mixed | GPU, expensive, slow to start | Tokens/sec |
| **State path** | vector index, BM25 index, metadata DB, object store | Continuous | ms | Memory + disk, stateful | Vectors × dimensions |

### 2.1 The one architectural rule

**Never let the write path and the read path share a compute pool.** This is the most common
production incident in RAG systems and it has one shape: a backfill starts, embedding jobs saturate
the workers or the GPU, and query latency goes from 800 ms to 40 s while nothing is technically
"down." Every autoscaler reacts to the wrong signal because average utilization looks healthy.

Separation means: distinct deployments, distinct node pools or GPU pools, distinct queues, distinct
autoscaling signals, distinct priority classes. On Compose that means separate services with
explicit CPU/memory limits. On Kubernetes it means separate node pools with taints and a
`PriorityClass` that lets the read path preempt the write path (§7.5).

The corollary: **they share exactly one thing — the index — and that shared thing is where all the
interesting concurrency problems live** (§12.1).

### 2.2 Statefulness, ranked

Deployment difficulty is a function of how much state a component owns:

| Component | State | Deployment difficulty |
|---|---|---|
| API / retrieval service | none | trivial — stateless replicas |
| Embedding / reranker server | model weights (read-only, large) | easy — the only problem is startup time (§6.3) |
| Generator (self-hosted) | model weights + KV cache (ephemeral) | medium — VRAM sizing, cold start |
| Ingestion workers | in-flight work only, if you made it idempotent (§8.4) | easy *if* idempotent, awful if not |
| Object store / raw docs | durable, large, immutable | easy — it's S3 |
| Metadata DB | durable, transactional | standard — it's Postgres |
| **Vector index** | **durable, large, rebuild-expensive** | **hard — this is the load-bearing wall** |

Corollary that drives most of §11: the fastest route to a boring deployment is to **make the vector
index someone else's problem** — managed Postgres with pgvector, or a hosted vector DB — until
you have a measured reason not to. Self-hosting a sharded vector database is a platform project,
not a deployment task.

---

## 3. The deployment ladder — Compose to Kubernetes

The honest framing: these are **tiers on a ladder, and you climb it when a specific constraint
binds — not on schedule and not on aesthetics.** Each rung below names the constraint that pushes
you off it.

| Tier | Shape | Fits | What pushes you off it |
|---|---|---|---|
| **0 — Laptop / single process** | Chroma or LanceDB embedded, everything in one Python process | Prototype, ≤100k chunks | Anyone else needs to use it |
| **1 — One box, Docker Compose** | Compose: API + workers + Postgres/pgvector + Redis; models via API | Internal tools, ≤5M vectors, ≤50 QPS, single team | The box can't hold the index in RAM, or you need HA, or a GPU pool with more than one consumer |
| **2 — Managed PaaS + managed data** | Containers on ECS/Fly/Cloud Run/App Runner; managed Postgres; managed vector DB; models via API | Most B2B SaaS RAG. **This is the sweet spot and it is under-used.** | Self-hosted GPU inference, strict data residency, or per-tenant isolation you can't express in the PaaS |
| **3 — Kubernetes** | Deployments + StatefulSets + Jobs + GPU node pools + an orchestrator | Multiple teams, self-hosted models, multi-tenant, GPU fleet | Nothing — this is the top. The cost is a platform team. |
| **3′ — Kubernetes + a serving framework** | Tier 3 plus KServe / Ray Serve / llm-d | Many models, canaries per model, scale-to-zero across a GPU fleet | — |

### 3.1 The three constraints that actually force Kubernetes

Not "we're growing." These:

1. **A GPU fleet with more than one consumer.** The moment two teams, or two workload classes
   (online + batch), contend for the same GPUs, you need quota, queueing and preemption. That is
   Kubernetes + Kueue (§7.5), and there is no good Compose answer.
2. **Independent scaling of components with wildly different cost.** Your reranker needs 4 replicas
   at peak and 0 at night; your API needs 20; your index needs 3 and never restarts. Encoding that
   on one box means over-provisioning to the max of everything.
3. **HA on a stateful index.** Multi-replica vector search with rolling upgrades and no read
   downtime is a StatefulSet-and-PVC problem. Compose has no story for it.

If none of those bind, **Tier 1 or Tier 2 is the correct production answer** and choosing Tier 3
anyway buys you a control plane, a service mesh, an ingress controller, cert rotation, and a
YAML surface area that will consume more engineering time than the RAG system itself.

### 3.2 The counter-argument, stated fairly

If your organization *already runs* Kubernetes — the platform team exists, GitOps exists, the
observability stack exists — then Tier 3 is cheaper than Tier 1 for you, because Tier 1 means
building a second, bespoke operational surface that nobody else knows how to debug. **The right
tier is a property of your organization, not of your workload.** The workload constraints in §3.1
tell you when Kubernetes becomes necessary; existing organizational investment tells you when it
was already free.

---

## 4. Docker Compose in production

Compose in production is a legitimate, under-defended choice. It is also frequently done badly in
ways that make people conclude it "doesn't scale" when what didn't scale was the configuration.

Mechanics are in [`../kubernetes/41-docker-compose-deep-dive.md`](../kubernetes/41-docker-compose-deep-dive.md)
and the comparison in [`../kubernetes/42-compose-vs-swarm-vs-kubernetes.md`](../kubernetes/42-compose-vs-swarm-vs-kubernetes.md).
The RAG-specific delta:

### 4.1 What a production Compose RAG deployment must have

| Requirement | Why RAG specifically |
|---|---|
| **Hard `mem_limit` on ingestion workers** | A parser meeting a 400 MB scanned PDF will take the whole box down with it and kill your query path. This is the #1 Compose RAG outage. |
| **`cpus` limits on parse workers** | Tier-2 parsers (§9.3) saturate every core they can see and starve the API process. |
| **Separate worker service, `restart: unless-stopped`, bounded concurrency** | Ingestion is the thing that crashes. It must crash alone. |
| **A real queue (Redis/RabbitMQ/Postgres `SKIP LOCKED`), not in-process threads** | Restarting the API must not lose in-flight ingestion. |
| **Named volumes on fast local NVMe for the index, plus a backup job** | An HNSW index rebuild is hours. Losing the volume is an outage measured in hours, not minutes. Snapshot it. |
| **Healthchecks that check the index, not the port** | A vector DB that answers TCP but has an unloaded collection will happily serve zero results. Health-check a known query. |
| **Pinned image digests** | Model server images change default behavior between minor versions; a silent tokenizer change is a silent recall regression. |
| **Log/metric shipping off the box** | If it's one box, the box's death takes the evidence with it. |

### 4.2 What Compose genuinely cannot do

- **No bin-packing across hosts.** One box is your ceiling; vertical scaling is your only lever.
- **No rolling update with health gating.** `docker compose up -d` replaces containers; there is a
  gap. For a query API behind a load balancer you can work around this with two Compose projects
  and a proxy swap. For a stateful index you cannot.
- **No GPU scheduling.** You can pass `--gpus`, but two containers requesting the same GPU is an
  unmanaged race. There is no quota, no queue, no preemption.
- **No pod-level autoscaling.** Ingestion burst handling becomes "over-provision the worker count."
- **No secret rotation, no multi-tenancy primitives.**

### 4.3 Compose ceiling, quantified

Rough working limits before the constraint bites — treat as orders of magnitude:

| Resource | Practical single-box ceiling | Binding factor |
|---|---|---|
| Vectors (pgvector, HNSW, 1024-dim) | ~5–10M | RAM: index wants to be resident. ~4 KB/vector at fp32 + graph overhead ⇒ 10M ≈ 40 GB+ |
| Vectors with int8/binary quantization | ~40M+ | 4× / 32× reduction — see `03` on the recall cost |
| Query QPS (retrieval + rerank, API generation) | ~50–100 | CPU cores for BM25 + reranker |
| Ingestion throughput | Whatever the box's cores give you | See §9.3 — Tier-2 parsing is the wall |

Past those, climb to Tier 2 (managed) before Tier 3 (Kubernetes). Managed Postgres with pgvector at
50M vectors is a smaller change than adopting Kubernetes.

---

## 5. Kubernetes topology for RAG

Assume Tier 3. This section is the workload-by-workload mapping — the part a generic Kubernetes
guide won't give you.

### 5.1 Component → workload object

| Component | Object | Key configuration |
|---|---|---|
| Query/API service | `Deployment` + HPA | Scale on **in-flight requests or queue depth**, never CPU. Generation is I/O-wait; CPU stays flat while latency explodes. |
| Retrieval service | `Deployment` + HPA | Co-locate with the index (same zone) — cross-AZ hops are pure latency tax on a 10 ms query. |
| Reranker | `Deployment` on GPU pool, KEDA-scaled | Scale on request queue depth. Sizing in §7.3. |
| Embedding server | `Deployment` on GPU pool | **Two of them**: one small for online query embedding (latency SLO), one batch-configured for ingestion (throughput). Same model, different tuning. |
| Generator (self-hosted) | `Deployment` or KServe `InferenceService` | Long `terminationGracePeriodSeconds` (drain in-flight generations, 120s+), `readinessProbe` only after weights load. |
| Ingestion workers | `Deployment` (steady) or KEDA `ScaledJob` (bursty) | Bounded concurrency, `PriorityClass` **below** the read path. |
| Scheduled backfill / reindex | `Job` / `CronJob`, or the orchestrator's executor (§8) | `activeDeadlineSeconds`, `backoffLimit`, and idempotency (§8.4) — not retries alone. |
| Vector DB (self-hosted) | `StatefulSet` + PVC | Local NVMe StorageClass, `podAntiAffinity` across nodes, PDB with `maxUnavailable: 1`. |
| Postgres/metadata | Managed service, or an operator (CloudNativePG) | Don't hand-roll it. |
| Object store (raw docs) | External (S3/GCS/MinIO) | Raw documents are the source of truth; the index is derived and rebuildable. Keep it that way. |

### 5.2 Node pools

Three pools minimum. This is the topology that makes §2.1's separation real:

| Pool | Taint | Instances | Workloads |
|---|---|---|---|
| `general` | none | Standard CPU, on-demand | API, retrieval, orchestrator, control plane bits |
| `gpu-online` | `workload=gpu-online:NoSchedule` | L4 / L40S / A10G, **on-demand** | Reranker, online embedding, generator. Latency SLO ⇒ never spot. |
| `gpu-batch` | `workload=gpu-batch:NoSchedule` | L4 / A100, **spot/preemptible**, scale-to-zero | Bulk embedding, Tier-2/3 parsing, reindex |
| `cpu-batch` (optional) | `workload=batch:NoSchedule` | High-core, spot | Tier-1/Tier-2 CPU parsing — usually the cheapest place to parse (§9.3) |

Scale-to-zero on the batch pools is where the money is. A `gpu-batch` pool that only exists during
a nightly backfill costs 4 hours a day, not 24.

### 5.3 The autoscaling signal, which everyone gets wrong

CPU utilization is meaningless for every RAG component:

| Component | Wrong signal | Right signal |
|---|---|---|
| API / generation proxy | CPU | In-flight requests; concurrency; token queue |
| Reranker | CPU | Pending requests (KEDA on queue length or a custom Prometheus metric) |
| Generator (vLLM) | GPU util % | `vllm:num_requests_waiting` — GPU util reads high during a single slow decode |
| Ingestion workers | CPU | Kafka consumer lag / queue depth / unindexed-document count |

KEDA is the standard answer for all four; HPA with a custom Prometheus adapter is the alternative.
Internals in [`../kubernetes/22-autoscaling.md`](../kubernetes/22-autoscaling.md).

```yaml
# KEDA: scale ingestion workers on backlog, with a hard ceiling that protects the index
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: { name: ingest-workers }
spec:
  scaleTargetRef: { name: ingest-worker }
  minReplicaCount: 0
  maxReplicaCount: 40          # ceiling = what the vector DB can absorb, not what the queue wants
  cooldownPeriod: 300
  triggers:
    - type: redis
      metadata: { listName: ingest:pending, listLength: "50" }   # 50 docs backlog per replica
```

The `maxReplicaCount` comment is the load-bearing part: **the ingestion autoscaler's ceiling is set
by write capacity of the index, not by the size of the backlog.** Scaling 200 embedding workers
against a vector DB that accepts 5k upserts/sec produces timeouts, retries, and duplicate work.

### 5.4 The index as a StatefulSet

If you self-host Qdrant/Weaviate/Milvus:

- **Local NVMe, not network storage.** ANN search is random-read-bound; EBS gp3 will show up as P99
  latency you cannot explain.
- **PVC sizing = vectors × dims × bytes × (1 + graph overhead ≈ 0.5–1.0) × replication.** Then
  double it: HNSW rebuilds and compaction need headroom.
- **PodDisruptionBudget** — otherwise a node drain during a cluster upgrade takes your index down.
- **Backups are snapshots of the volume plus the ability to rebuild from object storage.** Test the
  rebuild path; the first time you test it should not be during the incident.
- Deep dive: [`../kubernetes/13-statefulset-deep-dive.md`](../kubernetes/13-statefulset-deep-dive.md),
  [`../kubernetes/19-storage-csi-pv-pvc.md`](../kubernetes/19-storage-csi-pv-pvc.md).

---

## 6. The model-serving layer

Three or four models are in a RAG system's hot path: embedding, reranking, generation, and
sometimes a Tier-2/3 parser model. Each can be an API call or a served process, and mixing is
normal and correct.

### 6.1 The serving stack, disambiguated

The most common confusion is treating these as competitors. They are layers:

| Layer | What it is | Examples |
|---|---|---|
| **Engine** | Loads weights, runs forward passes, batches | vLLM, TGI, TensorRT-LLM, TEI, llama.cpp, ONNX Runtime |
| **Server** | HTTP/gRPC, model repository, multi-framework | Triton, BentoML, LitServe, Infinity |
| **Orchestrator** | Kubernetes CRD, autoscaling, canary, scale-to-zero, multi-model | KServe, Ray Serve, Seldon, llm-d |

vLLM *and* KServe is the normal answer, not vLLM *or* KServe. Deploying KServe with a default
HuggingFace runtime when you should be using vLLM gets you the orchestration benefits while leaving
3–5× throughput on the table.

### 6.2 Choosing per workload

| Model | Default | When to change |
|---|---|---|
| **Generator, self-hosted** | vLLM (continuous batching, PagedAttention, prefix caching) | TensorRT-LLM if you'll pay compile time for the last 20–30%; llm-d if the model exceeds one 8-GPU node |
| **Generator, hosted** | Provider API | Break-even math in §9.4 — it usually favors the API until you can fill a GPU |
| **Embeddings, self-hosted** | **TEI** (Rust, Flash Attention, token-budget batching) | Infinity if you need multimodal or many model types from one server; vLLM's pooling mode if you're already running vLLM and want one less component |
| **Reranker, self-hosted** | TEI (supports cross-encoder/sequence-classification) or ONNX Runtime on CPU for small models | GPU only once §7.3's math says you need it |
| **Tier-2 parser** | Plain HTTP worker (Docling/Marker in a container) — not a "model server" | Triton if you're batching the layout model at real volume |
| **Dev/laptop** | Ollama, llama.cpp | Never in production for concurrent serving — no continuous batching |

Comparative throughput claims in this space are vendor-marketed and move fast (BEI, Arctic
Inference and others each publish multiples over TEI/vLLM on their own harnesses). Treat all of
them as "worth benchmarking," none as settled.

### 6.3 Cold start is the thing that ruins scale-to-zero

Scale-to-zero on GPU pods reduces idle GPU spend dramatically — the commonly cited figure is up to
~70% in dev/staging — and it is the single most misconfigured feature in the stack, because
**the first request after scale-up eats the full model-load time**.

Cold start budget, roughly:

| Stage | Typical | Fix |
|---|---|---|
| Node provisioning (if pool at zero) | 60–180 s | Keep one warm node, or accept it for batch only |
| Image pull (a vLLM image is 5–15 GB) | 30–300 s | Pre-pull via DaemonSet; slim images; a registry pull-through cache in-cluster |
| Weights download from object store | 60–600 s for a 70B | **Bake into the image, or pre-populate a `ReadOnlyMany` PVC / node-local cache** |
| Load to VRAM + CUDA graph capture | 15–90 s | Unavoidable; measure it |

So the realistic answer:

- **Online components (reranker, query embedding, generator): `minReplicas: 1`.** Scale-to-zero is
  a latency incident waiting for the first user of the morning.
- **Batch components: scale to zero aggressively.** Nobody notices a 3-minute cold start on a
  backfill.
- If you want scale-to-zero on an online path, you need weights on a node-local cache and a
  measured cold start under your SLO — measure it before enabling it, not after.

---

## 7. GPU: what you need, how many, and how to schedule it

This is the section people actually need and the one most guides skip in favor of listing GPU SKUs.

### 7.1 First: does this workload need a GPU at all?

Be ruthless here. Most RAG systems need **zero GPUs**, and the ones that need some need them for
components people don't expect.

| Workload | Needs GPU? | Reasoning |
|---|---|---|
| **Query embedding (online)** | **No** | One query ≈ 20–100 tokens. A 0.5B encoder on CPU does this in 10–30 ms. GPU only above ~100 QPS. |
| **Bulk embedding (ingest)** | **Yes, if the corpus is large** | Volume, not latency. §9.3 gives the break-even; below ~100M tokens an API is cheaper than owning a GPU. |
| **Reranker (online)** | **Often, and it surprises people** | Top-50 × 512 tokens = ~25k tokens *per query*, through a cross-encoder. This is 100–1000× the query-embedding work. See §7.3. |
| **Tier-1 parser (PyMuPDF)** | **No** | Sub-millisecond per page on one core. |
| **Tier-2 parser (Docling/Marker)** | **Optional — and CPU is often cheaper** | GPU buys wall-clock, not cost-efficiency (§9.3). |
| **Tier-3 VLM parser** | Yes, or an API | 7B+ vision model per page. |
| **Generation** | **Only if self-hosting** | §9.4. For most teams, the API wins until volume is large and sustained. |

The default RAG deployment in 2026 is: **APIs for embedding and generation, a CPU reranker or a
reranking API, and no GPUs at all.** Introduce a GPU when a printed calculation says to.

### 7.2 Sizing math

Two formulas. Everything else is bookkeeping.

**(a) VRAM.**

```
VRAM = weights + KV_cache + activations/overhead

weights_GB      ≈ params_B × bytes_per_param      (FP16 = 2, FP8/INT8 = 1, INT4 ≈ 0.5)
KV_per_token_B  = 2 × n_layers × n_kv_heads × head_dim × bytes_per_element
KV_cache_GB     = KV_per_token_B × ctx_len × concurrency / 1e9
overhead        ≈ 15–25% of (weights + KV)
```

**(b) Replica count** — Little's Law, applied at your latency SLO, not at max throughput:

```
required_concurrency = peak_QPS × P95_latency_seconds
replicas             = ceil(required_concurrency / concurrency_per_replica_at_SLO) × (1 + headroom)
```

`concurrency_per_replica_at_SLO` is the number you must **measure** — it is where per-request
latency crosses your SLO as batch size grows, which is well below the throughput-maximizing batch
size. Headroom of 30–50% covers failover and autoscaler lag.

### 7.2.1 The RAG-specific delta on VRAM: KV cache is bigger than you think

General LLM-serving guides assume short prompts. **RAG prompts are 2k–8k input tokens because you
stuffed retrieved context into them**, and KV cache scales linearly with context length × batch.

Worked: Llama-3-70B-class model, 80 layers, 8 KV heads, head_dim 128, FP16 KV.

```
KV_per_token = 2 × 80 × 8 × 128 × 2 B = 327,680 B ≈ 0.33 MB/token
```

| Context per request | Concurrency 8 | Concurrency 32 | Concurrency 64 |
|---|---|---|---|
| 2k tokens | 5.4 GB | 21 GB | 43 GB |
| 8k tokens | 21 GB | 86 GB | 172 GB |
| 32k tokens | 86 GB | 344 GB | 688 GB |

With FP16 weights at 140 GB, a 2×H100 (160 GB) node has ~20 GB spare — **enough for concurrency 8
at 2k context, and nothing else.** The fixes, in order of preference:

1. **FP8 weights** (70 GB) — frees 70 GB of KV headroom, minimal quality cost for most models. Measure it.
2. **FP8/INT8 KV cache** — halves the table above. This is the highest-leverage knob for RAG.
3. **Cap `max_model_len`** to what your prompts actually use. Serving 128k context "just in case"
   reserves KV budget you never use and silently caps concurrency.
4. Fewer, longer chunks in the prompt — a retrieval decision with a hardware consequence.

> **The rule: for RAG, choose the GPU by KV-cache budget, not by weight size.** An L40S (48 GB) often
> beats an A100 40 GB for a mid-size model precisely because the extra 8 GB is all KV headroom.

### 7.2.2 The other RAG-specific delta: you are prefill-bound

RAG has a high input:output ratio — typically 4000 in / 500 out. That inverts the usual serving
intuitions:

```
prefill_FLOPs ≈ 2 × params × input_tokens
```

For 70B at 4000 input tokens: `2 × 70e9 × 4000 = 5.6e14` FLOP ≈ 560 TFLOP. On 2×H100 at a realistic
~400 TFLOPS each of achieved dense FP16, that is **~0.7 s of pure prefill before the first token
appears** — your TTFT floor, before queueing, retrieval, or reranking.

Consequences that only apply to RAG:

- **Buy FLOPs, not just memory bandwidth.** Decode-bound serving is bandwidth-bound; RAG's prefill
  is compute-bound. This changes the GPU choice.
- **Enable chunked prefill.** Without it, one 8k-token prefill blocks every in-flight decode and
  your P99 inter-token latency goes to pieces.
- **Prefix caching helps less than advertised.** It caches shared prefixes — your system prompt.
  Retrieved context is different per query by construction, so the majority of your prefill is
  uncacheable. Put the system prompt *first* and the retrieved context *after* it, so the cacheable
  part is a prefix. Ordering the prompt the other way makes prefix caching worthless.
- **Fewer retrieved chunks is a latency lever with a quality cost** — that trade is measurable on
  your eval harness, and it is one of the few knobs that moves cost *and* latency *and* quality at
  once.

### 7.3 Worked sizing examples

**A — Internal docs assistant.** 200k pages, 50 users, ~2k queries/day, peak 5 QPS. Generation via API.

| Component | Calculation | Answer |
|---|---|---|
| Query embedding | 5 QPS × ~50 tokens, 0.5B encoder | CPU. 2 cores. |
| Reranker (top-50 × 512 tok, 278M cross-encoder) | `2 × 278e6 × 25,600 ≈ 14 TFLOP/query`; L4 ≈ 55 TFLOPS achieved ⇒ **~0.26 s/query ⇒ ~4 QPS/GPU** | 2× L4 (1 + headroom) — **or** use a reranking API and buy zero GPUs |
| Generation | API | 0 |
| **Total** | | **0–2 small GPUs. Compose on one box.** |

That reranker line is the point: **it is the only component whose GPU need is non-obvious, and it
is the one people forget to size.** Appendix D §10 correctly says reranking is cheap *as an API*;
self-hosted, it is the largest online GPU consumer in a typical RAG system.

**B — Customer-facing SaaS.** 5M pages, monthly full re-embed, 50 QPS peak, generation via API.

| Component | Calculation | Answer |
|---|---|---|
| Bulk embedding | 5M pages × ~600 tok = **3B tokens**/month. TEI + BGE-M3 on L4 ≈ 8–15k tok/s ⇒ 55–105 GPU-hours | 1× L4 spot, ~3 days/month — or an embedding API (§9.3 says they cost about the same) |
| Reranker | 50 QPS ÷ 4 QPS-per-L4 = 12.5 | 13–16× L4, KEDA-scaled 4→16 on diurnal traffic |
| Query embedding | 50 QPS × 50 tok | Shares the reranker pool, or CPU |
| **Total** | | **~16 online L4 + a spot batch pool.** Kubernetes is now justified (§3.1 #1 and #2). |

**C — Self-hosted everything, regulated.** 5M pages, 50 QPS, 70B generator on-prem.

| Component | Calculation | Answer |
|---|---|---|
| Generator | 4k in / 500 out. Prefill 560 TFLOP/query ⇒ ~1.4 GPU-seconds/query on H100-class. 50 QPS × 1.4 = **70 GPU-seconds/second of prefill alone** | **~8–10× H100** for prefill, plus decode capacity and KV headroom ⇒ 2 nodes of 8×H100, TP=2 per replica |
| Reranker + embedding | as B | 16× L4 |
| **Total** | | 16× H100 + 16× L4. This is a **$1M+/year** infrastructure decision — §9.4 exists to make sure it was made deliberately. |

The gradient across A→B→C is the real lesson: **the generator is what makes RAG expensive, and it
is the component you are least likely to need to self-host.**

### 7.4 Distributing work across GPUs

Five distinct mechanisms, frequently conflated:

| Mechanism | What it splits | Use when |
|---|---|---|
| **Data parallel (replicas)** | Requests across identical model copies | **The default.** Model fits on one GPU. Scale by adding replicas behind a queue-depth autoscaler. |
| **Tensor parallel (TP)** | One model's layers across GPUs *within a node* | Weights don't fit on one GPU. Needs NVLink — TP across PCIe or across nodes is a latency disaster. |
| **Pipeline parallel (PP)** | Model's layers into sequential stages | Model doesn't fit in one node. Adds bubble overhead; prefer FP8 or a smaller model first. |
| **Prefill/decode disaggregation** | Prefill and decode onto separate pools (llm-d) | Frontier models at high volume where prefill and decode have different optimal hardware. Genuinely promising for RAG's prefill-heavy shape, but early — 2026 adopters are AI-native shops. |
| **GPU sharing (MIG / time-slicing / MPS)** | One GPU across several *small* models | Reranker + embedding + a parser model on one L40S. See below. |

**GPU sharing, decided:**

| Mode | Isolation | Failure blast radius | Use for |
|---|---|---|---|
| **MIG** (A100/H100 only) | Hardware — separate memory and SM partitions | One slice | Multi-tenant; production online workloads sharing a big GPU. Up to 7 slices. |
| **Time-slicing** | None — context switching, shared memory | Whole GPU (one OOM kills all) | Dev/staging, bursty low-priority batch. Cheap and simple. |
| **MPS** | Process-level, shared memory | **Whole GPU — a fatal CUDA error in one client kills the MPS server and every client** | Trusted, same-team, high-throughput concurrent inference |
| **Whole GPU** | Total | Itself | Any latency-SLO online workload where you can fill the GPU |

Hybrid (MIG slices, time-sliced within a slice) gives the best density and is what large clusters
converge on. For a RAG system specifically: **MIG your online GPU pool so the reranker and the
embedding server share hardware without sharing fate; time-slice your batch pool.**

Mechanics: NVIDIA GPU Operator, `nvidia.com/gpu.shared` resources, MIG manager. See
[`../k8s-learn/gpu-platform-tasks.md`](../k8s-learn/gpu-platform-tasks.md).

### 7.5 Scheduling

The default Kubernetes scheduler treats a GPU as an indivisible integer resource and knows nothing
about queues, fairness, or all-or-nothing placement. Without a layer on top, GPU clusters idle at
**25–35% utilization**; with queueing and quota they reach **60–85%**. That is the whole business
case for this subsection.

**The stack:**

| Layer | Tool | Provides |
|---|---|---|
| Node setup | **NVIDIA GPU Operator** | Drivers, container runtime, device plugin, MIG manager, DCGM exporter. Mandatory. |
| Admission & quota | **Kueue** (CNCF) | ClusterQueue/LocalQueue quotas, borrowing between teams, preemption, gang semantics, ResourceFlavors for heterogeneous GPU types |
| Scheduling policy | **Volcano** or KAI, optional | Gang scheduling, DRF fairness, NVLink/PCIe topology awareness |
| Node provisioning | Karpenter / cluster-autoscaler | Scale GPU pools to zero when idle |
| Resource modeling | **DRA** (Dynamic Resource Allocation) | Structured GPU requests beyond integer counts — beta since 1.32, GA in 1.34; check your cluster version |

**The RAG-specific scheduling policy:**

```yaml
# Read path outranks write path. This is §2.1 expressed as a scheduler rule.
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata: { name: rag-online }
value: 1000000
preemptionPolicy: PreemptLowerPriority
---
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata: { name: rag-batch }
value: 1000
preemptionPolicy: Never          # batch never preempts; it waits
```

```yaml
# Kueue: cap what ingestion can ever take, so a backfill cannot eat the serving fleet
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata: { name: rag-batch }
spec:
  namespaceSelector: {}
  resourceGroups:
    - coveredResources: ["nvidia.com/gpu", "cpu", "memory"]
      flavors:
        - name: l4-spot
          resources:
            - name: "nvidia.com/gpu"
              nominalQuota: 8
              borrowingLimit: 8     # may borrow idle online GPUs, and be preempted off them
```

Notes that matter in practice:

- **Gang scheduling only matters for multi-GPU single jobs** (TP/PP inference, distributed
  training). Bulk embedding is embarrassingly parallel and needs queueing, not gang semantics.
  Don't adopt Volcano for a workload that doesn't need it.
- **Topology awareness matters when TP > 1.** A cluster with 40 free GPUs can still fail to place
  a job needing 8 on one node. Requesting 2 GPUs that land on different NUMA/PCIe roots gives you
  TP over PCIe and a fraction of expected throughput.
- **Spot for batch, on-demand for online, always.** Preempted embedding work is a retry (if you did
  §8.4); a preempted generator is a user-visible error.
- **Set requests == limits for GPU pods.** GPUs cannot be overcommitted; a Burstable GPU pod is a
  configuration error.
- Depth: [`../k8s-learn/scheduling-constraints-tasks.md`](../k8s-learn/scheduling-constraints-tasks.md),
  [`../kubernetes/09-kube-scheduler-internals.md`](../kubernetes/09-kube-scheduler-internals.md).

### 7.6 What to monitor so the sizing stays honest

Sizing decays. These are the metrics that tell you it has:

| Metric | Source | What it tells you |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | DCGM exporter | **Necessary but deeply misleading** — reads ~100% during one slow decode. Never autoscale on it. |
| `DCGM_FI_DEV_FB_USED` | DCGM | Actual VRAM headroom — your §7.2 math, validated |
| `vllm:num_requests_waiting` | vLLM | **The real saturation signal.** Autoscale on this. |
| `vllm:gpu_cache_usage_perc` | vLLM | KV pressure. Sustained >90% ⇒ you are concurrency-capped by §7.2.1. |
| `vllm:time_to_first_token_seconds` | vLLM | Prefill health; regression here is usually §7.2.2 |
| Queue depth / consumer lag | Redis/Kafka | Write path health, and the ingestion autoscaler's input |
| Cost per 1k queries | Your own | The only metric that composes with everything above |

Deep coverage exists: [`../gpu-observability/14-llm-inference-observability.md`](../gpu-observability/14-llm-inference-observability.md),
[`../gpu-observability/05-gpu-allocation-and-utilization-efficiency.md`](../gpu-observability/05-gpu-allocation-and-utilization-efficiency.md),
[`../gpu-observability/02-dcgm-exporter-deep-dive.md`](../gpu-observability/02-dcgm-exporter-deep-dive.md).

---

## 8. Where pipelines get built

### 8.1 There are four things called "the pipeline"

Conflating them is why RAG ingestion architectures end up strange. They have different tools.

| # | Pipeline | Trigger | Latency target | Runs for | Right tool |
|---|---|---|---|---|---|
| **1** | **Bulk backfill** | Manual / one-off | Hours–days | Hours | Ray Data, Spark, Argo Workflows, or a well-parallelized `Job` |
| **2** | **Incremental ingest** | Schedule or event | Minutes–hours | Minutes | **Airflow / Dagster / Prefect** |
| **3** | **Streaming ingest** | CDC / message | Seconds | Continuous | Kafka + consumers, Flink, or KEDA-scaled workers |
| **4** | **Online request path** | HTTP request | Milliseconds | Milliseconds | Your application code. **Not an orchestrator.** |

The most common mistake in each:

1. Backfill written as 5M invocations of a per-document DAG — the scheduler becomes the bottleneck
   and you get 5M rows of task metadata. Backfill is a **data-parallel batch job**, not a DAG with
   5M nodes.
2. Incremental ingest written as a cron that re-processes everything — see §8.4.
3. Streaming adopted before anyone asked what the freshness requirement is. Most corpora do not
   change every second; "within an hour" is a very different, much cheaper system.
4. **An orchestrator in the request path.** Airflow's task latency is seconds; it is not a
   request-serving framework. If you want durable multi-step *per-request* execution — long agentic
   flows with retries and human-in-the-loop — that is Temporal's job, not Airflow's.

### 8.2 The orchestrator field

| Tool | Model | Strengths for RAG | Weaknesses | Ops load |
|---|---|---|---|---|
| **Airflow 3.x** | Task DAGs; now with assets, DAG versioning, event-driven scheduling, Edge Executor | The default. Enormous connector ecosystem; every data team already knows it; proven at scale on GenAI workloads | Task-oriented, so "the embeddings table" is a side effect of task #7 rather than a modeled thing; heaviest to operate | ~1–2 days/month |
| **Dagster** | **Software-defined assets** with lineage, partitions, freshness policies | **The best conceptual fit.** "Chunks for corpus X, partition 2026-08" is an asset with a freshness policy — which is precisely the RAG freshness problem. Partitions make incremental reindex a first-class concept, not a hand-rolled bookkeeping layer | Smaller ecosystem; asset model is a real learning curve | ~0.5–1 day/month |
| **Prefect** | Python-native flows, dynamic | Lowest friction from "scripts that run in order" to orchestrated; excellent for dynamic fan-out over documents | Weakest lineage story — you'll build your own answer to "why is this chunk stale?" | ~0.5–1 day/month |
| **Temporal** | Durable execution / workflows-as-code | **Different category.** Correct for long-running per-document or per-request workflows with retries, timeouts, compensation, human approval. Excellent for agentic pipelines | Not a data orchestrator — no scheduling UI for analysts, no lineage, no partitions | Medium; needs a cluster |
| **Argo Workflows** | Kubernetes-native container DAGs | If you're already on Kubernetes, no extra runtime; each step is a pod; trivially scales fan-out | YAML-first authoring; no data awareness at all | Low if you have Kubernetes |
| **Ray Data** | Streaming distributed dataset over heterogeneous CPU/GPU | **The right tool for bulk embedding and bulk parsing.** Streams so it doesn't need the corpus in memory; pipelines CPU parse and GPU embed stages so the GPU stays fed. Reported 2–17× over Spark/SageMaker on batch inference (vendor-published; verify on your data) | A cluster to run; not a scheduler | Medium |
| **Spark** | JVM batch | Right if your corpus already lives in a lakehouse and your team runs Spark | Awkward for GPU model inference; JVM↔Python boundary | Existing |
| **Flink / Kafka Streams** | Stream processing | Real sub-minute freshness with exactly-once semantics | Heavy; only justified by a real freshness SLO | High |

### 8.3 Decision matrix

| Situation | Choose | Why |
|---|---|---|
| Already run Airflow | **Airflow 3** | The integration cost of a second orchestrator exceeds the benefit of a better model |
| Greenfield, data-platform mindset, freshness matters | **Dagster** | Assets + partitions + freshness policies map 1:1 onto the RAG ingestion problem |
| Small team, want it working this week | **Prefect** | Least ceremony; decorate your existing functions |
| Already on Kubernetes, don't want another control plane | **Argo Workflows** | Reuses everything you have |
| Per-document workflows with approvals, or agentic multi-step flows | **Temporal** | Durable execution is a different problem and this is the tool for it |
| One-time backfill of millions of documents | **Ray Data** (or Spark if you have it) | Data-parallel batch, not a DAG |
| Freshness SLO under a minute | **Kafka + CDC + workers** | Nothing schedule-based will hit it |
| Freshness SLO is "within an hour" | **Any of the above on a schedule** | Don't build streaming for this |

**A pattern worth naming:** Dagster or Airflow *orchestrating* Ray Data or Argo *executing* is the
common mature shape. The orchestrator owns scheduling, lineage, retries and observability; the
execution engine owns the parallelism. Don't make the orchestrator do the data plane's job.

### 8.4 The reference ingestion pipeline

The DAG shape that survives contact with production. Every stage is content-addressed and
idempotent, which is what makes retries, backfills and partial failures survivable.

```
  discover ─► fetch ─► identify ─► parse ─► normalize ─► chunk ─► embed ─► upsert ─► verify
     │          │         │          │                              │         │
     │          │         │          └─ failures ─► dead-letter ────┘         │
     │          │         └─ content_hash unchanged ─► SKIP (the whole point) │
     │          └─ raw bytes ─► object store (source of truth, immutable)     │
     └─ source listing ─► manifest (what exists, when it changed)             └─ index alias
```

| Stage | Contract | Failure handling |
|---|---|---|
| **discover** | Emit `(doc_id, source_uri, source_version, last_modified)` for every document in the source | Partial listings are fine; the manifest is a diff, not a truth |
| **fetch** | Write raw bytes to object store keyed by `sha256(bytes)`. Never overwrite | Retry with backoff; source outages must not corrupt the manifest |
| **identify** | Compute `content_hash`. **If unchanged, stop.** | This one line is the difference between a 20-minute incremental run and a 9-hour one |
| **parse** | `(content_hash, parser_version) → parsed doc`. Cache on that key | Poison documents go to a dead-letter queue with the raw bytes attached — never retried forever, never silently dropped |
| **normalize** | Deterministic text cleanup | Pure function; failures are bugs |
| **chunk** | `(parsed, chunker_version, params) → chunk[]` with stable `chunk_id` | Stable IDs make upsert idempotent |
| **embed** | `(chunk_text, model_id, model_version) → vector` | Batch. Retry on transient. **Never** silently substitute a different model |
| **upsert** | Idempotent write keyed by `chunk_id`; **delete chunks that no longer exist** | The forgotten half: documents shrink, and orphaned chunks are the classic "why is it citing a deleted paragraph" bug |
| **verify** | Count chunks, sample-query, check the eval golden set | Fail the run, don't publish the alias |

Five properties to insist on:

1. **Content-hash short-circuit.** Most incremental runs should do almost nothing. If your nightly
   job takes as long as your backfill, you don't have an incremental pipeline.
2. **Version every stage's output by the code+config that made it.** `parser_version`,
   `chunker_version`, `model_id`. This is what makes selective reprocessing possible: bump the
   chunker, reprocess chunking onward, don't re-parse 5M pages.
3. **Idempotent by `chunk_id`.** Then at-least-once delivery is sufficient and you never need
   exactly-once, which you were not going to get anyway.
4. **Dead-letter queue with the raw bytes.** A 0.3% parse failure rate on 5M documents is 15,000
   documents. They need a queue, an owner, and a dashboard — not a log line.
5. **Deletion propagation.** Source deletes must reach the index. GDPR/DSR requests make this a
   compliance requirement, not a nicety.

### 8.5 Freshness: choosing the trigger

| Freshness SLO | Mechanism | Cost |
|---|---|---|
| Days | Scheduled full reprocess | Simplest; fine for static corpora |
| Hours | Scheduled incremental with content-hash diff (§8.4) | **The default. Covers most real requirements.** |
| ~Minutes | Event-driven: source webhooks / S3 events / object-store notifications → queue → KEDA-scaled workers | Moderate |
| Seconds | CDC (Debezium reading the WAL) → Kafka → stream consumers | High. Justified only by a real requirement |

CDC deserves the specific note that it reads the database write-ahead log rather than polling, so
it captures every insert/update/delete without loading the primary — and Kafka's replay makes
index rebuilds possible without re-reading the source. That is genuinely the right architecture for
sub-minute freshness. It is also an entire subsystem. **Write down the freshness SLO before
choosing; most teams discover theirs is "an hour" and save themselves a Kafka cluster.**

---

## 9. Compute cost

### 9.1 The four cost centers

| Center | Nature | Scales with | Typical share |
|---|---|---|---|
| **Ingestion compute** | One-time + incremental | Corpus size × change rate × **parser tier** | 1%–95% (§9.3 — the variance is the finding) |
| **Storage** | Continuous | Vectors × dims × bytes × replicas | Usually <5% |
| **Online inference** | Per query | QPS × tokens | Usually the largest steady-state line |
| **Idle capacity** | Continuous | Whatever you provisioned and didn't use | **Frequently 40–70% of GPU spend** |

The last row is the one nobody puts in the budget and it is the one that decides the bill.

### 9.2 GPU price reference (July 2026 snapshot)

Normalized to **per-GPU-hour**. Verify before quoting — these moved materially during 2025–26.

| GPU | VRAM | Hyperscaler on-demand | Specialist cloud | Marketplace / spot |
|---|---|---|---|---|
| **H100 SXM** | 80 GB | $6.88 AWS P5 · $11.06 GCP · $12.29 Azure | $2.99 RunPod Secure · $3.09 Together (reserved) · $3.99 Lambda · $6.16 CoreWeave | $1.49–$1.99 (Vast.ai, RunPod Community) |
| **A100** | 40/80 GB | ~$1.48 AWS Capacity Blocks · ~$3.67 GCP a2 | from $1.99 Lambda | ~$1.20–$1.60 |
| **L40S** | 48 GB | — | ~$1.50 Crusoe | ~$0.80–$1.20 |
| **L4** | 24 GB | ~$0.70–$0.85 GCP g2 | — | ~$0.40 |
| **A10G** | 24 GB | ~$1.00 AWS g5.xlarge (whole instance) | — | — |
| **T4** | 16 GB | ~$0.35–$0.53 | ~$0.59 Modal · ~$0.81 Replicate (per-second) | — |

Three structural facts that outlive the numbers:

1. **The spread on identical hardware is ~4×.** Same H100, $1.85 to $7.20+ depending on where you
   rent it. Provider choice is a bigger cost lever than most engineering optimizations.
2. **Hyperscalers charge a 2–4× premium** over specialist clouds. You are paying for the rest of
   the platform (VPC, IAM, data locality, the compliance story). Sometimes worth it — decide
   deliberately.
3. **Per-second billing (Modal, Replicate, RunPod serverless) is the right shape for bursty
   inference** and the wrong shape for sustained load. Reserved capacity wins above roughly
   40–50% duty cycle.

### 9.3 Ingesting 1M pages — the finding

Assumptions, stated: 1M pages, ~600 tokens/page (600M tokens), CPU at ~$0.04/vCPU-hour,
L4 at $0.70/GPU-hour, throughputs from Appendix D §2.1 and the lab's measured parse speeds.

| Stage | Option | Rate | Compute | **Cost** |
|---|---|---|---|---|
| **Parse** | Tier 1 — PyMuPDF | ~0.5–10 ms/page | 0.15–28 CPU-hours | **$0.01 – $1** |
| | Tier 2 — Docling on CPU | ~1 page/s/core | 278 CPU-hours | **~$11** |
| | Tier 2 — Docling on L4 | ~8 pages/s | 35 GPU-hours | **~$25** |
| | Tier 3 — VLM API | $0.01–0.05/page | — | **$10,000 – $50,000** |
| **Chunk** | any | ~10k pages/s | negligible | **<$1** |
| **Embed** | API @ $0.02/1M tok | — | 600M tokens | **~$12** |
| | TEI + BGE-M3 on L4 @ ~10k tok/s | — | ~17 GPU-hours | **~$12** |
| **Upsert + store** | pgvector, 1024-dim fp32, ~6M chunks | ~25 GB + index | monthly | **~$5–15/mo** |

**The finding: parser tier moves total ingestion cost by three to four orders of magnitude, and
everything else is rounding error.** Tier 1 → Tier 2 is $1 → $25. Tier 2 → Tier 3 is $25 → $25,000.

Three consequences:

- **Optimizing embedding cost is almost always misdirected effort.** At $12 per million pages it
  cannot be your problem. (The corollary from Appendix D §8.3 holds: self-hosted and API embedding
  cost roughly the same here, so choose on operational grounds, not price.)
- **A VLM parser is only defensible selectively.** Route: Tier 1 for born-digital text, Tier 2 for
  layout-complex, Tier 3 **only** for the pages that fail a quality gate. A 2% Tier-3 routing rate
  turns $25,000 into $500 and keeps most of the accuracy. Build the router; it pays for itself on
  the first corpus.
- **For Tier-2 parsing, CPU is usually cheaper and GPU buys wall-clock.** $11 over 12 hours on 24
  cores, vs $25 over 4 hours on one L4. Pick based on whether you have a deadline or a budget.

### 9.4 Serving: self-host vs API break-even

The formula:

```
monthly_API_cost   = queries × (in_tok × $_in + out_tok × $_out) / 1e6
monthly_self_host  = GPUs × $/GPU-hr × 730 × (1 / utilization_you_actually_achieve)
                     + engineer_months × loaded_cost
```

Worked, illustratively: 4000 input / 500 output tokens per query; API at $3/M in, $15/M out;
self-hosted 70B on 2×H100 at $3/GPU-hr.

```
API per query      = 4000×3/1e6 + 500×15/1e6 = $0.0195
Self-host per month = 2 × $3 × 730           = $4,380
Break-even          = 4380 / 0.0195          ≈ 225,000 queries/month  (≈ 0.09 QPS average)
```

That number looks temptingly low, and three caveats destroy most of its appeal:

1. **It compares different models.** A self-hosted 70B is not a frontier API model. The comparison
   is only valid if the open model passes *your* eval (`08`). Against a *budget* open-weight API
   ($0.14–$0.50/M tokens), self-hosting's break-even runs into billions of tokens/month and
   essentially never arrives.
2. **It assumes you can fill the GPUs.** At 0.09 QPS your utilization is a fraction of a percent.
   Per-token, self-hosting is roughly 20× cheaper than a frontier API — *if the GPUs are busy*.
   Published guidance converges on needing ~60%+ sustained utilization for the math to hold. Model
   three utilization scenarios, not one.
3. **It omits the engineer.** One engineer maintaining the inference stack is $150k+/year, which is
   3× the GPU bill in this example.

**Therefore:** self-host generation when volume is *large and sustained* (roughly 2–5M tokens/day
on reserved capacity, at 60%+ utilization), or when data residency makes the API impossible. Not
because per-token math looked good at 3 a.m.

### 9.5 Idle is the dominant cost

```
effective_cost_per_query = (GPU_hourly × 730) / (QPS_avg × 2.628e6 seconds)
```

For 1× L4 at $0.70/hr = $511/month:

| Average QPS | Queries/month | Effective cost/query |
|---|---|---|
| 0.1 | 263k | $0.0019 |
| 1 | 2.6M | $0.00019 |
| 10 | 26M | $0.000019 |
| 40 (saturated) | 105M | $0.0000049 |

**A 400× swing on identical hardware, driven entirely by utilization.** Which is why §5.3
(autoscale on the right signal), §5.2 (scale batch pools to zero), and §7.4 (share GPUs) are cost
sections disguised as architecture sections. Diurnal traffic with a fixed fleet is the standard
way to run at 15% utilization while believing you have a cost-efficient deployment.

### 9.6 Cost levers, ranked by leverage

1. **Route parser tiers by document class** (§9.3). 10–1000×. Nothing else is close.
2. **Don't self-host generation until §9.4's math says so.** 2–10×.
3. **Autoscale on the correct signal and scale batch pools to zero** (§5.3, §5.2). 2–5×.
4. **Buy GPUs from a specialist cloud instead of a hyperscaler** (§9.2). 2–4×, for a procurement
   conversation rather than an engineering project.
5. **Spot for all batch work** (§7.5). 2–3× on the batch line.
6. **Quantize: FP8 weights and KV cache** (§7.2.1). 2×, by raising concurrency per GPU.
7. **Prompt-cache the system prefix; order the prompt so the cacheable part is a prefix** (§7.2.2).
   Up to 90% off repeat input tokens with hosted context caching.
8. **Quantize vectors (int8/binary)** (`03`). 4–32× on storage — which was <5% of the bill, so this
   is a scaling lever, not a cost lever, despite how often it's pitched as one.
9. **Retrieve fewer chunks.** Cuts prefill cost, TTFT, and generation cost together. Has a quality
   cost; measure it on the eval harness.

Note the shape of that list: **the top items are architecture and procurement decisions; the
satisfying technical optimizations are at the bottom.**

FinOps patterns for attributing all of this per-tenant:
[`../sre-observability/31-finops-for-observability.md`](../sre-observability/31-finops-for-observability.md),
[`../gpu-observability/12-capacity-planning-and-cost-optimization.md`](../gpu-observability/12-capacity-planning-and-cost-optimization.md).

---

## 10. Managed and serverless alternatives

Building the whole pipeline is not automatically correct. What you get and what you give up:

| Option | You get | You give up | Cost shape |
|---|---|---|---|
| **AWS Bedrock Knowledge Bases** | Ingestion, chunking, embedding, retrieval, generation, all wired | Chunking/parser control; portability | Default OpenSearch Serverless has a **2-OCU floor ≈ $345/month at zero traffic**; S3 Vectors (GA Dec 2025) cuts storage cost sharply; parsing via Bedrock Data Automation ~$0.010/page. Moderate workloads land $50–$500/mo |
| **Vertex AI RAG Engine** | Same, on GCP; strong context caching (~90% off cached input) | Same | Per-token + storage |
| **Azure AI Search** | Best-in-class hybrid + security trimming + enterprise connectors | Cost floor | **S1 ≈ $250/mo** is the realistic enterprise entry; teams provisioning S2/S3 upfront routinely find search costs exceed model costs in year one |
| **Serverless GPU (Modal, RunPod Serverless, Replicate, Baseten)** | Per-second GPU billing, no cluster | Cold starts; less control | Excellent for bursty batch parsing/embedding. Genuinely the best answer for a monthly reindex job |
| **Managed vector DB (Pinecone, Qdrant Cloud, Zilliz)** | The hard stateful component, operated | Some cost premium | Removes §2.2's only genuinely hard component |

**When buying wins:** small-to-medium corpus, standard document types, no unusual parsing needs, no
GPU fleet, and a team that should be building product. Managed RAG at $200–500/month against half
an engineer's time is not close.

**When building wins:** the parser is your differentiator (Appendix D §2 — and if your documents
are hard, it is); you need chunking control the platform doesn't expose; data residency;
per-tenant isolation the platform can't express; or volume where the managed premium exceeds a
platform engineer.

**The trap:** managed platforms hide the chunking and parsing decisions, which Appendix D
establishes as the ceiling on retrieval quality. You get a working system quickly and then hit a
quality wall you cannot debug because the failing stage is not exposed. Budget for the possibility
that you will need to take the ingestion half back in-house while keeping the serving half managed
— and keep raw documents in **your** object store from day one so that migration is possible.

---

## 11. Reference architectures

### 11.1 Tier 1 — internal tool

*Fits: ≤5M vectors, ≤50 QPS, one team, no GPU.*

| Layer | Choice |
|---|---|
| Runtime | Docker Compose on one 16-core / 64 GB box (+ a standby) |
| Ingestion | Python workers + Redis queue; cron-triggered incremental (§8.4) |
| Parse/chunk | PyMuPDF or Docling on CPU, bounded concurrency, hard memory limits |
| Embeddings | API |
| Index | Postgres + pgvector + `pg_search` for BM25 |
| Reranker | FlashRank / MiniLM cross-encoder on CPU, or a reranking API |
| Generation | API |
| Observability | OTEL → hosted backend. Off the box. |
| **Cost** | **$100–400/month all-in** |

**Swap when:** the index no longer fits in RAM → managed Postgres (Tier 2), not Kubernetes.

### 11.2 Tier 2 — customer-facing SaaS

*Fits: 5–50M vectors, 50–500 QPS, multi-tenant, GPU for reranking only.*

| Layer | Choice |
|---|---|
| Runtime | Kubernetes (managed control plane), 3 node pools (§5.2) |
| Orchestration | Dagster (assets + partitions + freshness) or Airflow 3 if it's already there |
| Bulk backfill | Ray Data on a spot GPU pool |
| Parse | Tier-routed: PyMuPDF → Docling → VLM API on quality-gate failure (§9.3) |
| Embeddings | API for online; TEI on spot L4 for bulk |
| Index | Managed Qdrant/Weaviate, or pgvector on managed Postgres if <10M |
| Reranker | TEI on `gpu-online` L4 pool, KEDA-scaled on queue depth |
| Generation | API, with a smaller model routed for easy queries |
| Freshness | Event-driven (object-store notifications → queue → KEDA workers) |
| Observability | OTEL GenAI semconv + DCGM + vLLM/TEI metrics; cost per tenant |
| **Cost** | **$3k–15k/month**, dominated by online GPU and generation API |

**Swap when:** generation API spend passes §9.4's break-even *at sustained utilization* → self-host
one model, keep the API as fallback.

### 11.3 Tier 3 — regulated / on-prem

*Fits: everything self-hosted, data cannot leave, audit requirements.*

| Layer | Choice |
|---|---|
| Runtime | Kubernetes on owned or dedicated hardware |
| GPU platform | NVIDIA GPU Operator + Kueue (quota/preemption) + MIG on the online pool |
| Serving | KServe wrapping vLLM (generation) and TEI (embed/rerank); canary per model |
| Orchestration | Airflow/Dagster on-cluster; Temporal for per-document workflows needing approval |
| Parse | Docling / Marker self-hosted (license-checked — Appendix D §13.6), VLM self-hosted for the routed tail |
| Index | Qdrant or Milvus StatefulSet on local NVMe, replicated, PDB, tested restore |
| Generation | Self-hosted 70B-class, TP=2, FP8 weights + FP8 KV (§7.2.1) |
| Governance | Per-tenant namespaces, network policy, audit log of every retrieval |
| **Cost** | **$50k–150k+/month.** The generator is most of it (§7.3 example C). |

**Swap when:** you can't keep the generator above ~60% utilization → consolidate models, add
request routing, or revisit whether the residency requirement truly covers generation.

---

## 12. Release and rollout — the RAG-specific hazards

Standard deployment practice (blue/green, canary, GitOps) transfers unchanged; see
[`../kubernetes/31-gitops-helm-kustomize.md`](../kubernetes/31-gitops-helm-kustomize.md). These
four hazards do not exist outside RAG and cause most of the bad days.

### 12.1 The embedding model and the index are one artifact

Changing the embedding model invalidates **every vector in the index**. Not degrades — invalidates.
Vectors from two models are not comparable, and if the dimensions happen to match, you get no
error at all: just silently garbage retrieval.

The only safe procedure:

1. Build a **new** index (new collection/table/alias target) with the new model.
2. Backfill it fully, offline, from the object store — never in place.
3. Run the eval golden set against **both**, side by side.
4. Swap the alias atomically. Keep the old index until you're sure.
5. Roll back = swap the alias back.

**Therefore: address the index by alias from day one, and pin `model_id` + `model_version` into
every stored chunk's metadata.** A row whose vector's provenance you can't determine is a row you
have to rebuild.

### 12.2 Chunker and parser changes are the same class of problem

Smaller blast radius — you can reprocess from cached parses (§8.4's stage versioning) instead of
re-fetching — but the same rule applies: new index, eval both, swap. This is exactly why §8.4
insists on versioning each stage's output by the code that produced it.

### 12.3 Canary on retrieval quality, not just error rate

A retrieval regression produces **zero errors and normal latency**. It returns confident, wrong
answers. HTTP-level canary analysis is blind to it.

The gate that works: run the golden set (`08`) against the canary before shifting traffic, and fail
the rollout on recall@k / nDCG regression beyond a threshold. This is the deployment-time payoff
for building P0 first, and the concrete reason the project ladder is ordered the way it is.

### 12.4 Draining a generator takes minutes

A pod serving a 60-second streaming generation needs `terminationGracePeriodSeconds` long enough to
finish in-flight work — 120 s or more — plus a `preStop` hook that stops accepting new requests
first. The default 30 s truncates user responses mid-sentence during every deploy, which surfaces
as "the model gets cut off sometimes" and gets debugged as a model problem for weeks.

---

## 13. Anti-patterns

1. **Kubernetes because it's production.** §3.1 lists the three constraints that force it. If none
   bind, you have adopted a platform, not solved a problem. Compose on one box with limits,
   healthchecks and off-box telemetry is a legitimate production deployment.

2. **One compute pool for ingestion and serving.** §2.1. A backfill starts, query P95 goes to 40 s,
   nothing alerts because average utilization looks fine. Separate pools, separate autoscalers,
   separate priority classes.

3. **Autoscaling on CPU.** Every RAG component is I/O- or GPU-bound. CPU utilization stays flat
   while the queue explodes. Scale on queue depth, in-flight requests, or `num_requests_waiting`.

4. **Scale-to-zero on an online GPU path without measuring cold start.** 45 seconds of model
   loading arrives as a 45-second P99 for the first user after a quiet period. Scale-to-zero
   belongs on batch pools (§6.3).

5. **Self-hosting the generator on per-token math alone.** §9.4. The per-token comparison ignores
   utilization (you will not fill the GPU), model parity (a 70B is not a frontier model), and the
   engineer (3× the GPU bill in the worked example).

6. **Optimizing embedding cost.** §9.3: $12 per million pages. Meanwhile the VLM parser next to it
   costs $25,000. Optimize the line item that is large, not the one that is easy to measure.

7. **A VLM parser on the entire corpus.** Route by document class with a quality gate; reserve
   Tier 3 for the pages that fail it. 2% routing keeps most of the accuracy for 2% of the cost.

8. **An orchestrator in the request path.** Airflow's task latency is measured in seconds. If you
   need durable multi-step execution per request, that is Temporal, and it is a different tool for
   a different problem.

9. **Re-processing everything on every run.** Without the content-hash short-circuit (§8.4) your
   "incremental" pipeline is a full backfill on a cron, and its cost scales with corpus size rather
   than change rate.

10. **Changing the embedding model in place.** §12.1. Dimensions may match; nothing will error;
    retrieval will be garbage. New index, eval both, alias swap.

11. **Forgetting deletion propagation.** Documents get deleted at the source and their chunks live
    forever in the index, getting cited. It is a correctness bug and, under GDPR/DSR, a compliance
    one.

12. **Provisioning for peak and calling it capacity planning.** §9.5: a fixed fleet against diurnal
    traffic runs at 15% utilization and costs 6× what it should. Utilization is the cost metric.

13. **Sizing a generator by weights and forgetting KV cache.** §7.2.1. The model "fits" and then
    serves four concurrent requests because RAG's 8k-token prompts ate the VRAM.

14. **Buying managed RAG and keeping no copy of the raw documents.** The platform hides parsing and
    chunking — Appendix D's ceiling — and without your own object store you cannot leave when you
    hit the quality wall.

---

## 14. Mental models — the compressed set

- **A RAG system is four paths, not one service.** Write, read, model, state. Different scaling
  laws, different hardware, different failure modes. Deploying them as one thing is the root cause
  of most RAG incidents.

- **The write path must never share compute with the read path.** If you take one thing from this
  appendix, take this.

- **Kubernetes is forced by a GPU fleet with multiple consumers, by independently-scaling
  components, or by HA on a stateful index — not by growth.** And it is nearly free if your
  organization already runs it. The right tier is a property of your org, not your workload.

- **Most RAG systems need zero GPUs.** The one that surprises people is the reranker: ~25k tokens
  through a cross-encoder *per query*, which is 100–1000× the query-embedding work.

- **For RAG, size GPUs by KV cache, not by weights.** Retrieved context makes prompts long, and KV
  scales with context × concurrency. FP8 KV cache is the highest-leverage knob you have.

- **RAG serving is prefill-bound, not decode-bound.** Buy FLOPs, enable chunked prefill, and put
  the system prompt before the retrieved context so prefix caching has something to cache.

- **There are four different things called "the pipeline."** Bulk backfill, incremental ingest,
  streaming ingest, and the request path. Different tools. Conflating them produces every strange
  ingestion architecture you have ever seen.

- **Content-hash short-circuit, versioned stages, idempotent upsert keyed by chunk_id.** These
  three properties turn ingestion from a fragile job into a restartable one, and they are what make
  "reprocess only the chunking step" possible.

- **Parser tier dominates ingestion cost by three to four orders of magnitude.** Everything else —
  embedding, chunking, storage — is rounding error. Route by document class.

- **Idle is the dominant GPU cost.** A 400× swing in cost-per-query on identical hardware, decided
  entirely by utilization. Autoscaling and scale-to-zero are cost architecture.

- **The embedding model and the index are one versioned artifact.** Changing one without rebuilding
  the other fails silently, which is the worst way for anything to fail.

- **Canary on retrieval quality.** A retrieval regression has a normal error rate and normal
  latency. Only the golden set catches it, which is why the eval harness is P0.

---

## 15. Cross-reference map

**Do not rewrite any of this.** Read it in place; this appendix is only the RAG-specific delta.

> The repo README flags `../kubernetes/` as the most expensive directory to read. Enter through
> `../k8s-learn/` — the task files are the short path — and open a `../kubernetes/` file only when
> you need the internals of something you are actually debugging.

| Need | Read |
|---|---|
| Compose mechanics, production hardening | [`../kubernetes/41-docker-compose-deep-dive.md`](../kubernetes/41-docker-compose-deep-dive.md) |
| Compose vs Swarm vs Kubernetes, decided | [`../kubernetes/42-compose-vs-swarm-vs-kubernetes.md`](../kubernetes/42-compose-vs-swarm-vs-kubernetes.md) |
| GPU on Kubernetes — hands-on | [`../k8s-learn/gpu-platform-tasks.md`](../k8s-learn/gpu-platform-tasks.md) |
| Scheduling constraints — hands-on | [`../k8s-learn/scheduling-constraints-tasks.md`](../k8s-learn/scheduling-constraints-tasks.md) |
| Requests/limits/QoS — hands-on | [`../k8s-learn/resources-tasks.md`](../k8s-learn/resources-tasks.md), [`../kubernetes/21-resource-management-and-qos.md`](../kubernetes/21-resource-management-and-qos.md) |
| HPA/VPA/KEDA internals | [`../kubernetes/22-autoscaling.md`](../kubernetes/22-autoscaling.md) |
| Scheduler internals (why your GPU pod is Pending) | [`../kubernetes/09-kube-scheduler-internals.md`](../kubernetes/09-kube-scheduler-internals.md), [`../kubernetes/34-custom-schedulers-and-scheduler-framework.md`](../kubernetes/34-custom-schedulers-and-scheduler-framework.md) |
| StatefulSets and storage for the index | [`../kubernetes/13-statefulset-deep-dive.md`](../kubernetes/13-statefulset-deep-dive.md), [`../kubernetes/19-storage-csi-pv-pvc.md`](../kubernetes/19-storage-csi-pv-pvc.md) |
| Workload controllers, Jobs and CronJobs | [`../kubernetes/12-workload-controllers.md`](../kubernetes/12-workload-controllers.md) |
| Multi-tenancy primitives | [`../kubernetes/25-multi-tenancy.md`](../kubernetes/25-multi-tenancy.md) |
| GitOps, Helm, Kustomize | [`../kubernetes/31-gitops-helm-kustomize.md`](../kubernetes/31-gitops-helm-kustomize.md) |
| Container images — size, layers, cold start | [`../kubernetes/39-dockerfile-staff-level-best-practices.md`](../kubernetes/39-dockerfile-staff-level-best-practices.md), [`../kubernetes/43-python-containers-with-uv-performance-and-cold-start.md`](../kubernetes/43-python-containers-with-uv-performance-and-cold-start.md) |
| Anti-patterns in container config | [`../kubernetes/40-docker-anti-patterns-and-bad-configs.md`](../kubernetes/40-docker-anti-patterns-and-bad-configs.md) |
| GPU telemetry — DCGM, what the metrics mean | [`../gpu-observability/02-dcgm-exporter-deep-dive.md`](../gpu-observability/02-dcgm-exporter-deep-dive.md), [`../gpu-observability/03-k8s-gpu-cluster-observability.md`](../gpu-observability/03-k8s-gpu-cluster-observability.md) |
| Allocation vs utilization — the semantics behind §9.5 | [`../gpu-observability/05-gpu-allocation-and-utilization-efficiency.md`](../gpu-observability/05-gpu-allocation-and-utilization-efficiency.md) |
| Batch vs stateless GPU workloads | [`../gpu-observability/04-batch-vs-stateless-workloads.md`](../gpu-observability/04-batch-vs-stateless-workloads.md) |
| LLM inference observability — TTFT, ITL, queueing | [`../gpu-observability/14-llm-inference-observability.md`](../gpu-observability/14-llm-inference-observability.md) |
| GPU capacity planning and cost | [`../gpu-observability/12-capacity-planning-and-cost-optimization.md`](../gpu-observability/12-capacity-planning-and-cost-optimization.md) |
| LLM/AI observability end to end | [`../sre-observability/26-llm-and-ai-observability.md`](../sre-observability/26-llm-and-ai-observability.md) |
| Cost attribution patterns, reused for tokens | [`../sre-observability/31-finops-for-observability.md`](../sre-observability/31-finops-for-observability.md) |
| Pipeline reliability — the ingest side must not lose data | [`../sre-observability/28-telemetry-pipeline-reliability.md`](../sre-observability/28-telemetry-pipeline-reliability.md) |
| SLO engineering — for the freshness and retrieval-quality SLOs | [`../sre-observability/13-slo-engineering.md`](../sre-observability/13-slo-engineering.md) |
| Bounded concurrency, backpressure, cancellation in the workers | [`../python-mastery/29-async-patterns-and-pitfalls.md`](../python-mastery/29-async-patterns-and-pitfalls.md) |
| Measurement methodology — for every number you replace here | [`../python-mastery/31-measurement-methodology.md`](../python-mastery/31-measurement-methodology.md) |

---

## Sources

Landscape and pricing figures in §6, §7.5, §9.2, §9.4 and §10 are a July–August 2026 snapshot from:

- [AI/ML on Kubernetes 2026: Production Stack Guide](https://kubernetesguru.com/ai-ml-on-kubernetes-2026-stack-guide/) — vLLM/Kueue/KServe/Ray/llm-d stack, GPU Operator, DCGM metrics, utilization figures
- [vLLM vs Triton vs KServe: Model Serving on Kubernetes](https://www.kubenatives.com/p/vllm-vs-triton-vs-kserve-kubernetes) — engine/server/orchestrator layering, cold-start caveats
- [RAG in Production: Deployment Strategies](https://coralogix.com/ai-blog/rag-in-production-deployment-strategies-and-practical-considerations/) — write/read path separation, event-driven ingestion
- [RAG Pipeline on Kubernetes: Production Architecture Guide](https://lucaberton.com/blog/rag-pipeline-kubernetes-production-architecture-2026/)
- [Orchestration Showdown: Dagster vs Prefect vs Airflow](https://www.zenml.io/blog/orchestration-showdown-dagster-vs-prefect-vs-airflow) and [Airflow vs Prefect vs Dagster (2026)](https://dev.to/datastackx/airflow-vs-prefect-vs-dagster-picking-the-right-orchestrator-in-2026-1ifb) — orchestrator comparison and ops-load estimates
- [Temporal vs Airflow vs Prefect vs Dagster](https://futurepicker.com/en/temporal-airflow-prefect-dagster-workflow-2026/) — durable execution vs data orchestration
- [Kueue: Kubernetes-Native AI Workload Scheduling](https://www.coreweave.com/blog/kueue-a-kubernetes-native-system-for-ai-training-workloads) and [Kueue Topology Aware Scheduling](https://kueue.sigs.k8s.io/docs/concepts/topology_aware_scheduling/)
- [GPU Sharing in Kubernetes: MIG vs MPS vs Time-Slicing](https://scaleops.com/blog/kubernetes-gpu-sharing/) and [NVIDIA GPU Operator: Time-Slicing](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html)
- [Kubernetes AI Infrastructure in 2026: GPU Scheduling](https://www.cloudoptimo.com/blog/kubernetes-ai-infrastructure-in-2026-gpu-scheduling-and-production-realities/)
- [GPU Pricing 2026 (17 vendors, vendor-neutral)](https://gpucloudcost.com/) and [H100 Rental Prices Compared](https://intuitionlabs.ai/articles/h100-rental-prices-cloud-comparison)
- [KV Cache Memory Calculation for LLMs](https://lyceum.technology/magazine/kv-cache-memory-calculation-llm/) and [GPU Memory Sizing Guide for LLM Inference](https://www.runpod.io/articles/guides/gpu-memory-sizing-guide-for-llm-inference)
- [Self-Hosting an LLM vs. API: Real Cost Math (2026)](https://cloudzy.com/blog/self-hosting-open-weight-llm-gpu-vps-cost/) and [LLM Inference Cost 2026](https://packet.ai/blog/llm-inference-cost)
- [Comparing Embedding Inference Solutions: TEI, Infinity, FastEmbed](https://filipmakraduli.substack.com/p/comparing-embedding-inference-solutions)
- [Embedding Inference at Scale with Ray Data and Milvus](https://zilliz.com/blog/embedding-inference-at-scale-for-RAG-app-with-ray-data-and-milvus) and [Ray Data comparisons](https://docs.ray.io/en/latest/data/comparisons.html)
- [Amazon Bedrock pricing in 2026](https://www.cloudzero.com/blog/amazon-bedrock-pricing/) and [Azure AI Search: Enterprise Retrieval & RAG Guide (2026)](https://www.signisys.com/blog/azure-ai-search-the-complete-guide-to-enterprise-retrieval-and-rag-on-azure/)
- [Real-Time Search Indexing with CDC](https://risingwave.com/blog/cdc-search-indexing-debezium-elasticsearch-risingwave/)
