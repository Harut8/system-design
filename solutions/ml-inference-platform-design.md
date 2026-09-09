# ML Model Serving & Inference Platform: Design Document

> Solution to [`tasks/ml-inference-platform.md`](../tasks/ml-inference-platform.md).

### Prerequisites and Learning Resources

Before or alongside this document, study these deep-dive chapters from the curriculum:

| Topic | Resource | Why |
|-------|----------|-----|
| GPU compute fundamentals | [`ai-rag/appendix-e-deployment-and-compute.md`](../ai-rag/appendix-e-deployment-and-compute.md) | GPU memory hierarchy, tensor parallelism, inference optimization — foundational for every section here |
| Parallel ML training | [`solutions/parallel-ml-training-design.md`](parallel-ml-training-design.md) | The training platform produces the artifacts this platform serves — understand the handoff |
| LLM gateway | [`solutions/llm-gateway-design.md`](llm-gateway-design.md) | External LLM routing and cost management — the gateway is a *consumer* of this platform's self-hosted model endpoints |
| Kubernetes scheduling | Upstream Kubernetes docs: device plugins, topology manager, scheduler extenders | GPU-aware scheduling, MIG partitioning, topology-aware placement |

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Architecture Overview](#2-architecture-overview)
3. [Model Registry and Artifact Management](#3-model-registry-and-artifact-management)
4. [Serving Runtime Architecture](#4-serving-runtime-architecture)
5. [Dynamic Batching Design](#5-dynamic-batching-design)
6. [LLM-Specific Serving](#6-llm-specific-serving)
7. [Autoscaling Design](#7-autoscaling-design)
8. [Traffic Management](#8-traffic-management)
9. [Multi-Model Composition](#9-multi-model-composition)
10. [Model Optimization Pipeline](#10-model-optimization-pipeline)
11. [Capacity Estimates](#11-capacity-estimates)
12. [Failure Walkthroughs](#12-failure-walkthroughs)
13. [Cost Model](#13-cost-model)
14. [Trade-offs](#14-trade-offs)
15. [Observability](#15-observability)
16. [Evolution Path](#16-evolution-path)
17. [Exercises](#17-exercises)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Does the platform manage the underlying Kubernetes cluster and GPU operator? | No — we assume Kubernetes with the NVIDIA GPU operator and device plugin is already running. We design the ML serving layer on top: the CRDs, controllers, sidecars, and routing mesh that turn "registered model" into "live endpoint." |
| Scope | Does the platform handle feature computation? | No — callers provide fully-formed input features. However, the platform must integrate cleanly with an external feature store (e.g., Feast), and the inference pipeline (section 9) can include a feature-lookup step as a pipeline node. |
| Scope | Are models trained on this platform? | No — models are trained on a separate Parallel ML Training Platform (or external systems like SageMaker). The handoff point is a registered model artifact in the model registry. |
| Multi-tenancy | How are teams isolated from each other? | Namespace-level isolation in Kubernetes. Each team's endpoints run in dedicated namespaces with resource quotas. A bad deployment in team A's namespace cannot starve team B's GPU allocation. Shared infrastructure (routing mesh, autoscaler) runs in a platform namespace. |
| Hardware | What GPU types are in the fleet? | Mix of NVIDIA A100 (80GB) and H100 (80GB). A100s are the majority today; H100s are being rolled out for LLM workloads. CPU-only nodes are available for traditional ML models. |
| Hardware | Is NVIDIA MIG/MPS available? | Yes — A100s support MIG (up to 7 partitions: 7x10GB or mixed profiles). H100s support MIG with improved profiles. MPS is available on both for time-slicing without memory isolation. |
| Latency | Is the 5ms platform overhead P99 inclusive of model load time? | No — it covers only the routing, load-balancing, schema-validation, and telemetry-emission overhead on an already-warm endpoint. Cold-start latency (model load) is a separate budget. |
| Consistency | Can two replicas briefly serve different model versions during a rollout? | Yes, briefly — during canary and blue-green transitions, different replicas intentionally serve different versions. But after a rollout completes or a rollback is triggered, all replicas must converge to the target version within 30 seconds. |
| Streaming | How do callers consume streamed LLM tokens? | Server-Sent Events (SSE) over HTTP/2. The platform terminates the SSE connection at the inference gateway and proxies token-by-token from the vLLM/TensorRT-LLM backend. gRPC streaming is supported as an alternative for internal high-throughput callers. |
| Scale-to-zero | What is the acceptable cold-start latency for scale-from-zero? | Configurable per endpoint. Default budget: 30 seconds for CPU models (image pull + model load), 120 seconds for GPU models (image pull + model download from object store + GPU memory allocation + model load). Callers receive a 202 Accepted with a retry-after header if the cold-start budget is exceeded. |
| Cost | How is GPU usage attributed to teams? | Per-second GPU attribution. Every GPU-second is tagged with (team, endpoint, model_version). Shared GPUs (MIG/MPS bin-packed models) attribute proportionally by partition size or time-slice fraction. |
| Reliability | What is the blast radius of a bad model deployment? | Strictly limited to that endpoint. Model deployments are namespaced, resource-quota'd, and health-checked independently. A model that OOMs on load cannot evict another team's model from a shared GPU — MIG partitions provide memory isolation, and the scheduler never co-locates non-MIG workloads on the same GPU. |

### Key Assumptions

1. **The model artifact is the contract.** A registered model version is an immutable, content-addressed artifact. Once registered, it cannot be modified — only new versions can be created. This is the foundation of reproducibility, rollback, and audit.
2. **GPU memory is the binding constraint**, not compute or network. Serving a 70B LLM consumes 140GB of GPU memory for weights alone (FP16); the platform's primary scheduling challenge is fitting models into finite GPU memory, not CPU cores.
3. **Heterogeneous workloads coexist.** A 2MB XGBoost model at 50K QPS on CPU and a 140GB LLM at 100 QPS on 4 GPUs are both first-class citizens. The platform must not optimize for one at the expense of the other.
4. **Callers are internal services**, not public internet. An API gateway sits in front for external traffic. This means mTLS for auth, no public DNS, and latency budgets assume same-datacenter or same-region hops.
5. **The fleet is mixed on-premise and cloud.** The platform abstracts this: a deployment spec says "give me 2x A100-80GB" and the scheduler decides whether that comes from the on-prem cluster or a cloud GPU pool — the model owner does not care.
6. **Failure is the norm for GPU workloads.** GPU OOM, CUDA errors, driver crashes, NVLink failures, and thermal throttling happen regularly at fleet scale (2000+ GPUs). Every component is designed assuming the GPU underneath it can fail at any moment.

### What We Are Explicitly Not Building (v1)

- Not a training platform (models arrive as pre-trained artifacts).
- Not a feature store (callers provide features; pipeline nodes can call an external store).
- Not an experiment tracking system (we integrate with MLflow/W&B for lineage, we don't replace them).
- Not a notebook or interactive development environment.
- Not a batch inference framework (though the same serving endpoints can be called in batch mode by the caller; we don't manage batch job orchestration ourselves).

---

## 2. Architecture Overview

### Component Map

```
                          ┌──────────────────────────────────────────────────────────┐
                          │                    Control Plane                          │
                          │  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐  │
                          │  │ Model Registry│  │  Deployment   │  │  Autoscaler  │  │
                          │  │ (artifacts,   │  │  Controller   │  │  Controller  │  │
                          │  │  versions,    │  │  (reconciles  │  │  (HPA+custom │  │
                          │  │  metadata)    │  │  desired vs   │  │  metrics,    │  │
                          │  │              │  │  actual state) │  │  predictive) │  │
                          │  └──────┬───────┘  └───────┬───────┘  └──────┬───────┘  │
                          │         │                  │                  │          │
                          │  ┌──────┴──────────────────┴──────────────────┴───────┐  │
                          │  │              Kubernetes API Server                   │  │
                          │  │  (CRDs: InferenceService, ModelVersion,             │  │
                          │  │   ServingRuntime, AutoscalePolicy)                   │  │
                          │  └─────────────────────┬───────────────────────────────┘  │
                          │                        │                                  │
                          │  ┌─────────────────────┴──────────────────────────────┐  │
                          │  │            Optimization Pipeline                      │  │
                          │  │  (quantization, TensorRT compile, ONNX optimize)      │  │
                          │  └───────────────────────────────────────────────────────┘  │
                          └──────────────────────────────────────────────────────────┘
                                                     │
                    watch (CRD changes, <1s propagation)
                                                     │
 ┌──────────┐     ┌──────────────────────────────────┼────────────────────────────────┐
 │  Caller   │────▶│                      Data Plane                                    │
 │ (service) │     │                                                                     │
 └──────────┘     │  ┌────────────┐   ┌────────────┐   ┌──────────────────────────┐   │
      ▲            │  │ Inference   │──▶│  Traffic    │──▶│   Serving Pod Pool         │   │
      │            │  │ Gateway     │   │  Router     │   │                            │   │
      │            │  │ (Envoy/     │   │ (Istio VirtualService  │ ┌──────────────────┐│   │
      │            │  │  Istio      │   │  canary,    │   │ │ Traditional ML Pod  ││   │
      │            │  │  ingress)   │   │  blue-green,│   │ │ (ONNX Runtime /     ││   │
      │            │  │             │   │  A/B, shadow│   │ │  XGBoost / sklearn) ││   │
      │            │  │ - TLS term  │   │  routing)   │   │ └──────────────────────┘│   │
      │            │  │ - auth      │   │             │   │ ┌──────────────────────┐│   │
      │            │  │ - schema    │   └────────────┘   │ │ Deep Learning Pod    ││   │
      │            │  │   validate  │                    │ │ (Triton Inference    ││   │
      │            │  │ - rate limit│                    │ │  Server / TorchServe)││   │
      │            │  │ - telemetry │                    │ └──────────────────────┘│   │
      │            │  └────────────┘                    │ ┌──────────────────────┐│   │
      │            │                                     │ │ LLM Pod              ││   │
      │            │                                     │ │ (vLLM / TensorRT-LLM ││   │
      │            │                                     │ │  continuous batching, ││   │
      │            │                                     │ │  multi-GPU tensor     ││   │
      │            │                                     │ │  parallelism)         ││   │
      │            │                                     │ └──────────────────────┘│   │
      │            │                                     └──────────────────────────┘   │
      │            │                                                                     │
      │            │  ┌────────────────────────────────────────────────────────────────┐ │
      │            │  │  Observability Sidecar (per pod)                                │ │
      │            │  │  - OTel metrics (GPU util, queue depth, batch size, latency)   │ │
      │            │  │  - Distributed tracing                                          │ │
      │            │  │  - Prediction logging (sampled, for drift detection)            │ │
      │            │  └────────────────────────────────────────────────────────────────┘ │
      └────────────┴──────────────────────────────────────────────────────────────────────┘
                                           │
                          ┌────────────────┼────────────────────┐
                          ▼                ▼                    ▼
                  ┌──────────────┐ ┌──────────────────┐ ┌──────────────────┐
                  │ Object Store  │ │ Prometheus /      │ │ Cost Attribution │
                  │ (S3/GCS:      │ │ Thanos (metrics)  │ │ Service (GPU-sec │
                  │  model        │ │ Jaeger (traces)   │ │ ledger, per-team │
                  │  artifacts)   │ │ Loki (logs)       │ │ chargeback)      │
                  └──────────────┘ └──────────────────┘ └──────────────────┘
```

### Control Plane vs. Data Plane

| Plane | Components | Characteristics |
|---|---|---|
| **Control plane** | Model registry, deployment controller, autoscaler controller, optimization pipeline, admin API/CLI | Can tolerate seconds of staleness; reconciliation loops, not synchronous per-request calls. Must survive a model-registry outage without disrupting already-deployed endpoints. |
| **Data plane** | Inference gateway, traffic router, serving pods, observability sidecars | Every millisecond counts. Stateless routing layer; the serving pods hold model state in GPU memory. Must serve traffic even if the control plane is down — already-running replicas continue serving; only new deployments/scale changes are blocked. |

**Why this split matters**: the most common reliability bug in ML serving platforms is making the inference hot path depend synchronously on a control-plane call (e.g., "fetch model config from the registry on every request"). Every fact the data plane needs — endpoint routing table, model health, schema — is **pushed** to the data plane via Kubernetes CRD watches and Istio config pushes, never fetched inline per request.

### Request Path, Step by Step (with Latency Contribution)

```
                                                              Cumulative
Step  Action                                                  Latency
────  ──────────────────────────────────────────────────────  ──────────
 1.   Caller sends HTTP/2 (or gRPC) request to the           0 ms
      endpoint's stable URL.

 2.   Envoy ingress sidecar receives the connection.          +0.1 ms
      TLS already terminated at the L4 load balancer
      (or mTLS at Envoy).

 3.   AuthN: validate caller's service identity               +0.2 ms
      (mTLS client cert or bearer JWT, checked locally
      against a cached JWKS — no external call).

 4.   Schema Validation: validate input against the           +0.3 ms
      declared input schema (JSON Schema, cached in
      Envoy Wasm filter or sidecar). Reject malformed
      requests with 400 before they reach the model.

 5.   Rate Limiting: local token-bucket check per             +0.1 ms
      (caller, endpoint). Reject with 429 if exceeded.

 6.   Traffic Router: Istio VirtualService routes the         +0.2 ms
      request to the correct model version (canary %,
      blue-green, A/B assignment via consistent hash
      on user_id header).

 7.   Load Balancer (Envoy): pick a healthy replica           +0.1 ms
      from the destination version's pod pool using
      least-outstanding-requests.

 8.   Serving Pod receives the request.                       +0.1 ms
      Model Server sidecar (or the runtime's HTTP
      server) accepts and enqueues the request.

 9a.  [Traditional ML] Inference executes immediately         +1-8 ms
      (XGBoost predict, ONNX Runtime session.run).
      No batching queue for latency-sensitive CPU models.

 9b.  [Deep Learning] Dynamic batcher accumulates             +2-15 ms
      the request into a batch (up to max_wait_time),         (wait)
      dispatches the batch to the GPU for inference.          +5-80 ms
                                                              (compute)

 9c.  [LLM] Request enters the continuous batching            +0 ms
      scheduler in vLLM. Prefill (prompt encoding)            (queue)
      begins immediately if GPU has capacity; if not,         +50-400 ms
      queued until an in-flight request completes and         (prefill)
      frees KV cache blocks.                                  +30-80 ms
      Decode: tokens stream one-by-one.                       per token

 10.  Response serialized to JSON (or SSE stream begun).      +0.3 ms

 11.  Telemetry emitted asynchronously (fire-and-forget       +0 ms
      to OTel collector; does NOT block the response).        (async)

 12.  Response returned to caller via the Envoy sidecar.      +0.2 ms
                                                              ──────────
Platform overhead (steps 2-8, 10, 12):                        ~1.3 ms
                                                              P99 < 5 ms
```

**Summary of platform overhead**: steps 2 through 8 and steps 10 through 12 contribute approximately 1-2 ms at P50 and 3-5 ms at P99 — well within the 5ms P99 platform overhead budget. The overwhelming majority of end-to-end latency is model inference itself (step 9a/9b/9c), which varies by 3 orders of magnitude depending on model type.

---

## 3. Model Registry and Artifact Management

### Design Goals

* Every deployed model is traceable to an exact, immutable artifact version.
* Support heterogeneous model formats without the registry needing to understand their internals.
* Model metadata (hardware requirements, offline metrics, lineage) is first-class, not an afterthought — it drives scheduling, right-sizing, and optimization decisions.

### Data Model

```sql
-- A model is a logical entity (e.g., "fraud-detector", "product-embedder")
CREATE TABLE models (
    model_id            TEXT PRIMARY KEY,        -- e.g. 'fraud-detector'
    team                TEXT NOT NULL,
    description         TEXT,
    input_schema        JSONB,                    -- JSON Schema for input validation
    output_schema       JSONB,                    -- JSON Schema for output validation
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A model version is an immutable snapshot of a trained model
CREATE TABLE model_versions (
    model_id            TEXT NOT NULL REFERENCES models(model_id),
    version             TEXT NOT NULL,            -- semver or monotonic int, e.g. 'v23'
    artifact_uri        TEXT NOT NULL,            -- s3://models/fraud-detector/v23/
    artifact_sha256     TEXT NOT NULL,            -- content-addressed, immutable
    artifact_size_bytes BIGINT NOT NULL,
    model_format        TEXT NOT NULL,            -- 'onnx' | 'torchscript' | 'xgboost' |
                                                  -- 'lightgbm' | 'tensorflow_savedmodel' |
                                                  -- 'huggingface' | 'tensorrt' | 'triton_ensemble' |
                                                  -- 'custom_container'
    framework           TEXT,                     -- 'pytorch' | 'tensorflow' | 'xgboost' | ...
    framework_version   TEXT,                     -- '2.3.0'
    runtime_hint        TEXT,                     -- 'onnxruntime' | 'triton' | 'vllm' | 'torchserve'
    -- Hardware requirements (declared by the model owner or inferred)
    min_gpu_memory_mb   INTEGER,                  -- NULL for CPU-only models
    num_gpus            INTEGER DEFAULT 0,        -- 0 = CPU only; 2-8 for tensor-parallel LLMs
    gpu_type_required   TEXT,                     -- NULL (any) | 'a100' | 'h100'
    cpu_request_cores   NUMERIC(4,1) DEFAULT 1,
    memory_request_mb   INTEGER DEFAULT 512,
    -- Optimization metadata
    quantization        TEXT,                     -- NULL | 'fp16' | 'int8' | 'int4' | 'awq' | 'gptq'
    tensorrt_compiled   BOOLEAN DEFAULT FALSE,
    -- Offline metrics (from training evaluation)
    offline_metrics     JSONB,                    -- {"accuracy": 0.97, "f1": 0.94, "latency_p99_ms": 12}
    -- Lineage
    training_job_id     TEXT,                     -- link to training platform's job
    dataset_version     TEXT,
    experiment_id       TEXT,                     -- MLflow / W&B experiment
    parent_version      TEXT,                     -- if this was produced by optimizing another version
    -- Lifecycle
    status              TEXT NOT NULL DEFAULT 'registered',
                                                  -- registered | validated | deployed | deprecated | archived
    registered_by       TEXT NOT NULL,
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, version)
);

CREATE INDEX idx_versions_status ON model_versions (model_id, status);
CREATE INDEX idx_versions_artifact ON model_versions (artifact_sha256);

-- Deployment configuration (what the user declares; the controller reconciles)
CREATE TABLE deployments (
    deployment_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id            TEXT NOT NULL,
    endpoint_name       TEXT NOT NULL UNIQUE,      -- stable URL: /v1/endpoints/{endpoint_name}/predict
    -- Active version(s) and traffic split
    traffic_config      JSONB NOT NULL,            -- [{"version":"v23","weight":90},{"version":"v24","weight":10}]
    -- Serving configuration
    serving_runtime     TEXT NOT NULL,             -- 'onnxruntime' | 'triton' | 'vllm' | 'torchserve' | 'custom'
    instance_type       TEXT NOT NULL,             -- 'cpu-4c-8g' | 'gpu-a100-1' | 'gpu-a100-4-tp' | 'mig-a100-3g.40gb'
    min_replicas        INTEGER NOT NULL DEFAULT 1,
    max_replicas        INTEGER NOT NULL DEFAULT 10,
    autoscale_policy    JSONB NOT NULL,            -- see section 7
    -- SLOs
    target_latency_p99_ms INTEGER,
    target_availability   NUMERIC(5,4),            -- e.g. 0.9999
    tier                TEXT NOT NULL DEFAULT 'tier-2', -- tier-1 (revenue-critical) | tier-2 | tier-3
    -- Resource limits
    max_batch_size      INTEGER DEFAULT 32,
    max_wait_time_ms    INTEGER DEFAULT 10,
    max_concurrent_requests INTEGER DEFAULT 100,
    -- Status
    status              TEXT NOT NULL DEFAULT 'creating',
    team                TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Kubernetes CRDs

The deployment controller translates a `deployments` table row into Kubernetes custom resources:

```yaml
apiVersion: serving.ml-platform.io/v1
kind: InferenceService
metadata:
  name: fraud-detector
  namespace: team-risk
  labels:
    ml-platform.io/model-id: fraud-detector
    ml-platform.io/team: risk-analytics
    ml-platform.io/tier: tier-1
spec:
  predictor:
    runtime: onnxruntime
    modelVersion: v23
    modelUri: s3://models/fraud-detector/v23/model.onnx
    modelSha256: "a1b2c3d4..."
    resources:
      requests:
        cpu: "4"
        memory: "8Gi"
      limits:
        cpu: "4"
        memory: "8Gi"
    minReplicas: 3
    maxReplicas: 50
    autoscalePolicy:
      metrics:
        - type: qps
          target: 5000          # per-replica target QPS
        - type: cpu_utilization
          target: 70
      scaleDownStabilization: 300s
      scaleUpRate: 4            # max 4x current replicas per scale-up
    batchingConfig:
      maxBatchSize: 64
      maxWaitTimeMs: 5
    healthCheck:
      path: /v2/health/ready
      periodSeconds: 5
      failureThreshold: 3
  canary:
    modelVersion: v24
    trafficPercent: 5
    rolloutPolicy:
      successThreshold:
        latencyP99MsBelow: 25
        errorRateBelow: 0.001
      failureThreshold:
        latencyP99MsAbove: 50
        errorRateAbove: 0.01
      evaluationWindow: 300s
      autoPromote: true         # auto-promote canary to 100% if success threshold met
      autoRollback: true        # auto-rollback if failure threshold met
```

### Model Artifact Storage and Distribution

```
Model Artifact Layout in Object Store:
s3://ml-platform-models/
  └── fraud-detector/
      ├── v23/
      │   ├── model.onnx                  (ONNX model file)
      │   ├── config.json                 (model card, hyperparameters)
      │   ├── preprocessing.py            (optional: custom preprocessing code)
      │   └── MANIFEST.sha256             (per-file checksums)
      └── v24/
          ├── model.onnx
          ├── config.json
          └── MANIFEST.sha256

  └── llm-70b-chat/
      ├── v3/
      │   ├── config.json                 (HuggingFace config)
      │   ├── tokenizer.json
      │   ├── model-00001-of-00015.safetensors
      │   ├── model-00002-of-00015.safetensors
      │   ├── ...                         (sharded weight files, ~140GB total)
      │   └── MANIFEST.sha256
      └── v3-int8/                        (quantized variant, parent_version=v3)
          ├── config.json
          ├── tokenizer.json
          ├── model-00001-of-00008.safetensors
          │   ...                         (~70GB total)
          └── MANIFEST.sha256
```

**Model pre-caching**: large models (especially LLMs at 70-140GB) cannot be downloaded from S3 on every pod startup — this would blow the 5-minute cold-start budget. The platform maintains a **model cache** on each GPU node using a DaemonSet that pre-pulls popular model versions to local NVMe:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: model-cache-policy
data:
  policy.yaml: |
    cache_dir: /mnt/nvme/model-cache
    max_cache_size_gb: 2000           # per node, on 3.8TB NVMe
    eviction_policy: lru_weighted     # weight by model_size * access_frequency
    pre_pull:
      # Models that should always be warm on every GPU node
      - model: llm-70b-chat/v3
        priority: critical
      - model: llm-70b-chat/v3-int8
        priority: critical
    gc_interval: 300s                 # eviction check every 5 min
```

### Model Registration API

```python
# CLI / SDK usage
from ml_platform import ModelRegistry

registry = ModelRegistry()

# Register a new model version
version = registry.register_version(
    model_id="fraud-detector",
    version="v24",
    artifact_path="./model_output/model.onnx",     # uploaded to S3 automatically
    model_format="onnx",
    framework="pytorch",
    framework_version="2.3.0",
    offline_metrics={"accuracy": 0.974, "f1": 0.951, "latency_p99_ms": 8},
    training_job_id="train-20260901-abc123",
    dataset_version="ds-v47",
    hardware_requirements={
        "min_gpu_memory_mb": None,    # CPU-only
        "num_gpus": 0,
        "cpu_request_cores": 4,
        "memory_request_mb": 8192,
    },
)
# version.artifact_sha256 == "a1b2c3d4..."
# version.status == "registered"

# Validate the model (run a test inference with sample inputs)
registry.validate_version(
    model_id="fraud-detector",
    version="v24",
    test_inputs=[{"features": [0.1, 0.2, ...]}],
    expected_outputs=[{"prediction": 1, "probability": 0.87}],
)
# version.status == "validated"
```

### Model Lineage Graph

```
Training Run           Model Version         Optimized Variant      Deployment
─────────────          ─────────────         ──────────────────     ──────────
train-job-1234    ──▶  llm-70b-chat/v3   ──▶  llm-70b-chat/v3-int8  ──▶  prod endpoint
  │                    (FP16, 140GB)          (INT8, 70GB)               (canary: 10%)
  │                         │
  dataset: ds-v12           │
  experiment: exp-789       └──▶  llm-70b-chat/v3-trt
                                  (TensorRT, 65GB)
```

Every optimization (quantization, TensorRT compilation) produces a new model version with `parent_version` pointing to the original — so you can always trace back from a deployed optimized model to the original training run and dataset.

---

## 4. Serving Runtime Architecture

### Runtime Selection Matrix

The platform does not implement inference itself — it orchestrates battle-tested serving runtimes, selecting the right one per model type:

| Model Type | Primary Runtime | Backup Runtime | Instance Type | Batching | Typical Latency |
|---|---|---|---|---|---|
| XGBoost / LightGBM | ONNX Runtime (via Triton) | Native XGBoost lib | CPU (4-8 cores) | Optional (micro-batching) | P50: 1-3ms, P99: 5-10ms |
| scikit-learn | ONNX Runtime | Custom container (pickle) | CPU (2-4 cores) | No (already fast) | P50: 0.5-2ms, P99: 3-8ms |
| Small neural nets (BERT, ResNet) | Triton Inference Server | TorchServe | GPU (1x A100 MIG-1g.10gb) | Dynamic batching | P50: 5-15ms, P99: 20-60ms |
| Medium neural nets (ViT-L, T5-XL) | Triton Inference Server | TorchServe | GPU (1x A100 MIG-3g.40gb) | Dynamic batching | P50: 15-40ms, P99: 50-100ms |
| LLM ≤13B params | vLLM | TensorRT-LLM | GPU (1x A100-80GB) | Continuous batching | TTFT P50: 50ms |
| LLM 30-70B params | vLLM | TensorRT-LLM | GPU (2-4x A100/H100, TP) | Continuous batching | TTFT P50: 150-300ms |
| Custom preprocessing + model | Custom container image | N/A | Varies | User-defined | Varies |

### Triton Inference Server Configuration (Deep Learning Models)

Triton is the default for non-LLM GPU models because it supports dynamic batching, multiple model formats (ONNX, TensorRT, PyTorch, TF), model ensembles, and concurrent model execution on a single GPU:

```
# Triton model repository layout (generated by the deployment controller)
/models/
  └── bert-embedder/
      ├── config.pbtxt
      └── 1/
          └── model.onnx

# config.pbtxt — Triton model configuration
name: "bert-embedder"
platform: "onnxruntime_onnx"
max_batch_size: 64

input [
  {
    name: "input_ids"
    data_type: TYPE_INT64
    dims: [ -1 ]           # variable sequence length
  },
  {
    name: "attention_mask"
    data_type: TYPE_INT64
    dims: [ -1 ]
  }
]

output [
  {
    name: "embeddings"
    data_type: TYPE_FP32
    dims: [ 768 ]
  }
]

# Dynamic batching configuration
dynamic_batching {
  preferred_batch_size: [ 8, 16, 32 ]
  max_queue_delay_microseconds: 5000    # 5ms max wait
  preserve_ordering: true
  default_queue_policy {
    timeout_action: REJECT
    default_timeout_microseconds: 50000  # 50ms total timeout in queue
    allow_timeout_override: true
  }
}

# Instance group: how many model instances on how many GPUs
instance_group [
  {
    count: 2                           # 2 instances of this model
    kind: KIND_GPU
    gpus: [ 0 ]                        # all on GPU 0 (MIG partition)
    rate_limiter {
      resources [
        { name: "gpu_memory", count: 1, global: true }
      ]
    }
  }
]

# Model warm-up: pre-load and run a sample inference to JIT-compile
model_warmup [
  {
    name: "warmup"
    batch_size: 1
    inputs {
      key: "input_ids"
      value: { dims: [ 128 ], data_type: TYPE_INT64, zero_data: true }
    }
    inputs {
      key: "attention_mask"
      value: { dims: [ 128 ], data_type: TYPE_INT64, zero_data: true }
    }
    count: 3
  }
]
```

### vLLM Serving Configuration (LLMs)

```python
# vLLM server launch configuration (generated by the deployment controller)
# This is what the LLM serving pod runs on startup.

# For a 70B model on 4x A100-80GB with tensor parallelism:
vllm_config = {
    "model": "/models/llm-70b-chat/v3",           # local NVMe cache path
    "tokenizer": "/models/llm-70b-chat/v3",
    "tensor-parallel-size": 4,                      # 4 GPUs for tensor parallelism
    "dtype": "float16",                             # FP16 weights
    "max-model-len": 32768,                         # max context length
    "gpu-memory-utilization": 0.90,                 # 90% of GPU mem for model + KV cache
    "max-num-seqs": 256,                            # max concurrent sequences in-flight
    "max-num-batched-tokens": 32768,                # max tokens in a single batch step
    "enable-prefix-caching": True,                  # reuse KV cache for shared prefixes
    "swap-space": 32,                               # 32 GB CPU swap for KV cache overflow
    "block-size": 16,                               # PagedAttention block size
    "enforce-eager": False,                          # allow CUDA graphs for decode
    "disable-log-requests": False,
    # Speculative decoding (optional, for throughput)
    "speculative-model": "/models/llm-7b-draft/v1",
    "num-speculative-tokens": 5,
    # Quantization (if using a quantized model)
    # "quantization": "awq",
}

# Launched as:
# python -m vllm.entrypoints.openai.api_server \
#   --model /models/llm-70b-chat/v3 \
#   --tensor-parallel-size 4 \
#   --dtype float16 \
#   --max-model-len 32768 \
#   --gpu-memory-utilization 0.90 \
#   --max-num-seqs 256 \
#   --enable-prefix-caching \
#   --port 8000
```

### Pod Structure

Each serving pod has a consistent structure regardless of runtime:

```
┌──────────────────────────────────────────────────────┐
│  Serving Pod                                          │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │  Init Container: model-downloader                 │ │
│  │  - Check local NVMe cache for model artifact      │ │
│  │  - If miss: download from S3, verify SHA256       │ │
│  │  - Mount model at /models/{model_id}/{version}/   │ │
│  └──────────────────────────────────────────────────┘ │
│                                                        │
│  ┌────────────────────┐  ┌──────────────────────────┐ │
│  │  Serving Runtime     │  │  Envoy Sidecar            │ │
│  │  (vLLM / Triton /    │  │  - Health check proxy     │ │
│  │   ONNX Runtime)      │  │  - Request metrics        │ │
│  │                      │  │  - Rate limiting          │ │
│  │  Port: 8000          │  │  - mTLS termination       │ │
│  │  GPU: allocated      │  │  Port: 8080 (external)    │ │
│  └────────────────────┘  └──────────────────────────┘ │
│                                                        │
│  ┌──────────────────────────────────────────────────┐ │
│  │  OTel Sidecar                                      │ │
│  │  - Scrapes runtime metrics (GPU util, queue depth) │ │
│  │  - Exports to Prometheus / OTel Collector           │ │
│  │  - Prediction logging (sampled to Kafka)           │ │
│  └──────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────┘
```

### Health Checking Protocol

```python
# Every serving runtime exposes the KServe V2 health protocol:

# Liveness: is the process alive?
# GET /v2/health/live  ->  200 OK / 503

# Readiness: is the model loaded and ready to serve?
# GET /v2/health/ready ->  200 OK / 503

# Model-specific readiness (for pods serving multiple models):
# GET /v2/models/{model_name}/ready ->  200 OK / 503

# Kubernetes probes wired to these:
livenessProbe:
  httpGet:
    path: /v2/health/live
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /v2/health/ready
    port: 8000
  initialDelaySeconds: 30       # model load can take 30-120s for large models
  periodSeconds: 5
  failureThreshold: 6           # 30s of failed readiness before removal from LB
  successThreshold: 1
```

---

## 5. Dynamic Batching Design

### The Problem

GPUs are throughput machines — they process a batch of 32 inputs in barely more time than a single input, because the matrix multiplications that dominate inference are parallelized across thousands of CUDA cores. But requests arrive one at a time from HTTP clients. Dynamic batching bridges this gap: accumulate individual requests into batches to maximize GPU utilization without violating latency SLOs.

### Batching Algorithm

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class InferenceRequest:
    """A single inference request waiting to be batched."""
    request_id: str
    input_tensor: Any               # numpy array or torch tensor
    arrived_at: float                # time.monotonic()
    future: asyncio.Future = field(default_factory=asyncio.get_event_loop().create_future)
    timeout_ms: float = 50.0        # caller's deadline

@dataclass
class BatchConfig:
    max_batch_size: int = 32        # max requests in one batch
    max_wait_time_ms: float = 10.0  # max time to wait for a full batch
    preferred_batch_sizes: list[int] = field(default_factory=lambda: [8, 16, 32])
    pad_to_preferred: bool = True   # pad to nearest preferred size for GPU efficiency

class DynamicBatcher:
    """
    Accumulates individual requests into batches, dispatching when either
    max_batch_size is reached or max_wait_time_ms has elapsed since the
    first request in the current batch arrived.

    The key trade-off: shorter max_wait_time_ms → lower latency for individual
    requests, but smaller batches → lower GPU utilization. Longer max_wait_time_ms
    → larger batches → higher throughput, but some requests wait longer.
    """

    def __init__(self, config: BatchConfig, model_runner):
        self.config = config
        self.model_runner = model_runner
        self.queue: asyncio.Queue[InferenceRequest] = asyncio.Queue()
        self._batch_task = None

    async def start(self):
        self._batch_task = asyncio.create_task(self._batch_loop())

    async def enqueue(self, request: InferenceRequest) -> Any:
        """Enqueue a request and wait for the result."""
        await self.queue.put(request)
        return await request.future

    async def _batch_loop(self):
        """
        Core batching loop. Runs continuously, forming and dispatching batches.
        
        Invariant: the first request in a batch never waits longer than
        max_wait_time_ms before the batch is dispatched, regardless of
        whether max_batch_size is reached.
        """
        while True:
            # Wait for the first request (no timeout — block until work arrives)
            first_request = await self.queue.get()
            batch = [first_request]
            batch_start = time.monotonic()
            deadline = batch_start + (self.config.max_wait_time_ms / 1000.0)

            # Try to fill the batch until max_batch_size or max_wait_time
            while len(batch) < self.config.max_batch_size:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    break
                try:
                    request = await asyncio.wait_for(
                        self.queue.get(),
                        timeout=remaining_time,
                    )
                    batch.append(request)
                except asyncio.TimeoutError:
                    break  # max_wait_time reached; dispatch what we have

            # Dispatch the batch
            asyncio.create_task(self._dispatch_batch(batch))

    async def _dispatch_batch(self, batch: list[InferenceRequest]):
        """Run inference on the batch and distribute results."""
        try:
            # Check for timed-out requests before wasting GPU compute on them
            now = time.monotonic()
            live_batch = []
            for req in batch:
                elapsed_ms = (now - req.arrived_at) * 1000
                if elapsed_ms > req.timeout_ms:
                    req.future.set_exception(TimeoutError(
                        f"Request {req.request_id} timed out in queue after {elapsed_ms:.1f}ms"
                    ))
                else:
                    live_batch.append(req)

            if not live_batch:
                return

            # Pad batch to preferred size for GPU efficiency (optional)
            actual_batch_size = len(live_batch)
            if self.config.pad_to_preferred:
                for ps in self.config.preferred_batch_sizes:
                    if ps >= actual_batch_size:
                        padded_size = ps
                        break
                else:
                    padded_size = actual_batch_size
            else:
                padded_size = actual_batch_size

            # Stack inputs into a batch tensor
            batch_inputs = self._stack_inputs(live_batch, padded_size)

            # Execute inference
            batch_outputs = await self.model_runner.predict_batch(batch_inputs)

            # Distribute results back to individual request futures
            for i, req in enumerate(live_batch):
                req.future.set_result(batch_outputs[i])

        except Exception as e:
            for req in batch:
                if not req.future.done():
                    req.future.set_exception(e)

    def _stack_inputs(self, batch, padded_size):
        """Stack individual inputs into a padded batch tensor."""
        import numpy as np
        inputs = [req.input_tensor for req in batch]
        # Pad with zeros if needed
        while len(inputs) < padded_size:
            inputs.append(np.zeros_like(inputs[0]))
        return np.stack(inputs)
```

### Batching Behavior Under Load

```
Low load (5 req/s):
  Requests trickle in. Batch of 1-3 forms every ~10ms (max_wait_time).
  GPU utilization: ~5-15%. Latency: ~2ms (minimal queue wait + fast inference).
  This is fine — the GPU is mostly idle because demand is low.

Moderate load (500 req/s):
  Batch of 32 fills every ~64ms. Good GPU utilization (~40-60%).
  Latency: ~3-10ms queue wait + inference time.

High load (5000 req/s):
  Batch of 32 fills every ~6.4ms (faster than max_wait_time).
  GPU is saturated. Queue starts building up.
  Latency: queue wait can grow to 20-50ms at P99.

Overload (>throughput capacity):
  Queue depth increases unboundedly. Requests start timing out.
  Autoscaler sees queue_depth > threshold, scales up replicas.
  Meanwhile, requests exceeding their timeout_ms are rejected at the
  queue, not after wasting GPU cycles.
```

### Adaptive Batching

For endpoints where the optimal batch size depends on input shape (variable-length sequences), the batcher adapts:

```python
class AdaptiveBatcher(DynamicBatcher):
    """
    Extends DynamicBatcher with input-size-aware batching.
    For sequence models, it's better to batch by total token count
    than by request count — a batch of 32 short sequences and a batch
    of 2 long sequences may use the same GPU memory.
    """
    def __init__(self, config: BatchConfig, model_runner, max_tokens_per_batch: int):
        super().__init__(config, model_runner)
        self.max_tokens_per_batch = max_tokens_per_batch

    def _should_dispatch(self, batch: list[InferenceRequest]) -> bool:
        if len(batch) >= self.config.max_batch_size:
            return True
        total_tokens = sum(req.input_tensor.shape[0] for req in batch)
        if total_tokens >= self.max_tokens_per_batch:
            return True
        return False
```

---

## 6. LLM-Specific Serving

LLM serving is architecturally distinct from traditional ML serving. This section covers the mechanisms that make serving a 70B-parameter model to 10K concurrent users feasible.

### GPU Memory Arithmetic for a 70B Model

**Weights (FP16)**:
```
Parameters:              70 billion
Bytes per parameter:     2 (FP16 = 16 bits = 2 bytes)
Total weight memory:     70B * 2 = 140 GB

With 4x A100-80GB (tensor parallelism degree = 4):
  Per-GPU weight memory: 140 GB / 4 = 35 GB per GPU
  Remaining per GPU:     80 GB - 35 GB = 45 GB available for KV cache + activations
  With gpu_memory_utilization=0.90:
    Usable per GPU:      80 * 0.90 = 72 GB
    For KV cache:        72 - 35 = 37 GB per GPU
    Total KV cache pool: 37 * 4 = 148 GB across 4 GPUs
```

**KV Cache per Request**:
```
A typical 70B model (e.g., Llama-2-70B architecture):
  num_layers:            80
  num_kv_heads:          8 (GQA: grouped-query attention)
  head_dim:              128
  dtype:                 FP16 (2 bytes)

KV cache per token per layer:
  K: num_kv_heads * head_dim * 2 bytes = 8 * 128 * 2 = 2,048 bytes = 2 KB
  V: same = 2 KB
  K+V per layer: 4 KB

KV cache per token (all layers):
  80 layers * 4 KB = 320 KB per token

KV cache per request (context_length tokens):
  At 2K context:    320 KB * 2,048   =  640 MB per request
  At 4K context:    320 KB * 4,096   = 1,280 MB = 1.25 GB per request
  At 8K context:    320 KB * 8,192   = 2,560 MB = 2.5 GB per request
  At 32K context:   320 KB * 32,768  = 10,240 MB = 10 GB per request
```

**Concurrent Request Capacity**:
```
With 148 GB total KV cache budget and average 4K context per request:
  Max concurrent requests: 148 GB / 1.25 GB = ~118 concurrent sequences

With average 2K context per request:
  Max concurrent requests: 148 GB / 0.64 GB = ~231 concurrent sequences

With 8K context (longer conversations):
  Max concurrent requests: 148 GB / 2.5 GB = ~59 concurrent sequences
```

**Activation Memory**:
```
Activation memory during forward pass (approximate, per batch token):
  ~2-4 MB per token in the batch (depends on model architecture)
  For a batch of 256 tokens being processed in one forward step:
    256 * 3 MB ≈ 768 MB total across all GPUs
  This is small relative to weights and KV cache.
  It is included in the ~5-8% overhead that gpu_memory_utilization
  accounts for by not using 100% of GPU memory.
```

**Summary Table**:

| Component | Memory (4x A100-80GB) | Notes |
|---|---|---|
| Model weights (FP16) | 140 GB (35 GB/GPU) | Fixed, loaded at startup |
| KV cache pool | 148 GB (37 GB/GPU) | Dynamic, grows/shrinks with concurrent requests |
| Activations + overhead | 32 GB (8 GB/GPU) | ~10% of total, covers CUDA context, activations |
| **Total** | **320 GB (80 GB/GPU)** | **100% of 4x A100-80GB** |

### Impact of Quantization on Memory

```
                  Weights      Per-GPU     KV Cache     Max Concurrent
                  Total        (4x A100)   Budget       Requests (4K ctx)
                  ─────────    ─────────   ──────────   ──────────────────
FP16 (baseline)   140 GB       35 GB       148 GB       ~118
INT8 (W8A16)       70 GB       17.5 GB     218 GB       ~174  (+47%)
INT4 (GPTQ/AWQ)    35 GB        8.75 GB    253 GB       ~202  (+71%)

INT8 on 2x A100 (instead of 4):
  Per-GPU weights: 35 GB       KV cache: 2*(72-35) = 74 GB
  Max concurrent:  74/1.25 = ~59 requests
  (same capacity as FP16-on-4-GPUs at 8K context, with half the GPUs)
```

This is the fundamental economic argument for quantization: INT8 lets you serve the same model with half the GPUs (2 instead of 4), or serve 47% more concurrent requests on the same hardware.

### Continuous Batching (vLLM)

Traditional batching waits for all sequences in a batch to finish generating before starting new ones. This wastes GPU cycles when sequences have different output lengths — short completions sit idle while long ones continue.

**Continuous batching** (also called "iteration-level scheduling") solves this by allowing the scheduler to insert new requests into the batch at every decode step:

```
Traditional (Static) Batching:
═════════════════════════════════════════════════════════
Time →

Seq A: [prefill][decode][decode][decode][decode][DONE]
Seq B: [prefill][decode][decode][decode][decode][decode][decode][decode][DONE]
Seq C: [prefill][decode][decode][DONE]...(idle)...(idle)...(idle)...(idle)
Seq D: (waiting)...........................................................[start after batch completes]

GPU utilization: ████████████████░░░░░░░░░░████████████████
                 ^-- wasted cycles while C is done but B is still going

Continuous Batching:
═════════════════════════════════════════════════════════
Time →

Seq A: [prefill][decode][decode][decode][decode][DONE]
Seq B: [prefill][decode][decode][decode][decode][decode][decode][decode][DONE]
Seq C: [prefill][decode][decode][DONE]
Seq D:                               ↑[prefill][decode][decode][decode][DONE]
                                     └── D joins the batch the moment C finishes

GPU utilization: ████████████████████████████████████████████
                 ^-- no wasted cycles; new requests join immediately
```

### PagedAttention Memory Management

Standard KV cache implementations pre-allocate a contiguous memory block per request for the maximum possible sequence length. This wastes memory massively — a request with `max_tokens=4096` allocates 1.25GB even if the actual generation is only 50 tokens.

**PagedAttention** (from vLLM) manages KV cache like an OS manages virtual memory — in fixed-size **pages** (blocks), allocated on demand:

```
Without PagedAttention (contiguous allocation):
═══════════════════════════════════════════════════════════
GPU Memory Map:

 Seq A: [████████████████████████████████████░░░░░░░░░░░░░░░]
         ^-- actual KV cache used (1.5K tokens) ^-- wasted (pre-allocated for 4K max)
 Seq B: [████████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░]
         ^-- actual (800 tokens)      ^-- wasted
 Seq C: [can't fit — no contiguous block large enough, even though
         total free memory is sufficient!]

  Internal fragmentation: ~40-60% of KV cache memory wasted.
  External fragmentation: large contiguous block not available even
  though total free memory is sufficient.

With PagedAttention (paged allocation):
═══════════════════════════════════════════════════════════
GPU Memory Map (block_size = 16 tokens):

  Block table (per sequence):
  Seq A: [blk_0][blk_3][blk_7][blk_12]...[blk_94]  (94 blocks = 1504 tokens)
  Seq B: [blk_1][blk_5][blk_8][blk_11]...[blk_50]  (50 blocks = 800 tokens)
  Seq C: [blk_2][blk_4][blk_6]...[blk_30]           (30 blocks = 480 tokens)

  Physical block pool:
  [blk_0:A][blk_1:B][blk_2:C][blk_3:A][blk_4:C][blk_5:B][blk_6:C][blk_7:A]...
   ^^^^^^^^  ^^^^^^^^  ^^^^^^^^
   Blocks are scattered in physical memory, but each sequence's
   block table provides logical contiguity.

  No internal fragmentation (blocks allocated on demand, one at a time).
  No external fragmentation (any free block can serve any sequence).
  Waste: only the last block per sequence may have unused slots.
  Typical waste: < 4% of KV cache memory.
```

**Block size choice**: vLLM's default `block_size=16` means each block holds 16 tokens' worth of KV cache. For our 70B model:
```
Block memory = 16 tokens * 320 KB/token = 5,120 KB = 5 MB per block
Total blocks in 148 GB pool = 148,000 MB / 5 MB = ~29,600 blocks
At 4K avg context: ~256 blocks per sequence
Max concurrent sequences: 29,600 / 256 = ~115 (close to our earlier estimate,
but with near-zero fragmentation waste)
```

### Prefix Caching

Many LLM requests share a common prefix (system prompt, few-shot examples). Prefix caching reuses the KV cache computed for this shared prefix across requests:

```
Without prefix caching:
  Request A: [system prompt (2K tokens)] + [user message A]
  Request B: [system prompt (2K tokens)] + [user message B]
  → system prompt KV cache computed twice, 2 * 640 MB = 1.28 GB wasted

With prefix caching:
  Shared prefix: [system prompt (2K tokens)] → KV cache computed once, stored
  Request A: reuse prefix KV cache + compute only [user message A]
  Request B: reuse prefix KV cache + compute only [user message B]
  → 640 MB saved, prefill latency reduced by ~50% for these requests

  In vLLM, prefix caching is managed at the block level:
  Prefix blocks are hashed by their token content and stored in a
  block-level hash table. When a new request's prefix matches,
  the existing blocks are shared (copy-on-write semantics).
```

### Speculative Decoding

Standard autoregressive decoding generates one token per forward pass of the large model. Speculative decoding uses a small "draft" model to propose N candidate tokens, then the large model verifies all N in a single forward pass:

```
Standard decoding (70B model):
  Step 1: forward(70B) → token_1     ~30ms
  Step 2: forward(70B) → token_2     ~30ms
  Step 3: forward(70B) → token_3     ~30ms
  Step 4: forward(70B) → token_4     ~30ms
  Step 5: forward(70B) → token_5     ~30ms
  Total for 5 tokens: ~150ms

Speculative decoding (7B draft + 70B target):
  Step 1: draft(7B) → [t1, t2, t3, t4, t5]    ~15ms (5 tokens from small model)
  Step 2: verify(70B, [t1..t5]) → accept 4/5    ~35ms (one forward pass, batch of 5)
  Step 3: draft(7B) → [t5', t6, t7, t8, t9]   ~15ms
  Step 4: verify(70B, [t5'..t9]) → accept 5/5   ~35ms
  Total for ~9 tokens: ~100ms

  Effective speedup: 1.5-2.5x in tokens/sec, depending on acceptance rate.
  Acceptance rate depends on how well the draft model matches the target.
  Typical acceptance: 70-85% for a well-matched 7B draft of a 70B target.
```

**When to use speculative decoding**: it helps when the model is memory-bound (decode phase), not compute-bound (prefill phase). For our 70B model, the decode phase is heavily memory-bandwidth-bound (reading 140GB of weights to generate one token), so speculative decoding is beneficial.

### Tensor Parallelism Layout

For models that don't fit in one GPU's memory, tensor parallelism shards each layer's weight matrices across GPUs:

```
4-way Tensor Parallelism for a 70B model on 4x A100:
═══════════════════════════════════════════════════════

Each transformer layer's attention and MLP weights are split:

  Attention (per layer):
    Q, K, V projections: [hidden_dim, num_heads * head_dim]
    Split across GPUs by heads: each GPU gets num_heads/4 heads
    
    GPU 0: heads 0-19    GPU 1: heads 20-39
    GPU 2: heads 40-59   GPU 3: heads 60-79

  MLP (per layer):
    Gate/Up projection: [hidden_dim, intermediate_dim]
    Split column-wise: each GPU gets intermediate_dim/4 columns
    
    Down projection: [intermediate_dim, hidden_dim]
    Split row-wise: each GPU gets intermediate_dim/4 rows

  Communication pattern per layer:
    1. Each GPU computes its shard of attention/MLP independently
    2. AllReduce across 4 GPUs to combine results (NVLink)
    3. ~2 AllReduce operations per layer * 80 layers = 160 AllReduce ops per forward pass

  NVLink bandwidth (A100): 600 GB/s bidirectional
  AllReduce data per layer: ~hidden_dim * dtype = 8192 * 2 = 16 KB
  Total AllReduce per forward: ~2.5 MB (negligible compared to compute)
  
  Latency overhead of TP communication: ~0.5-1ms per forward pass
  (NVLink latency, not bandwidth-bound for these small messages)
```

### Streaming Token Delivery

```
Client                Inference Gateway          vLLM Pod
  │                         │                        │
  │──POST /predict (SSE)───▶│                        │
  │                         │──POST /generate────────▶│
  │                         │                        │ (prefill: encode prompt)
  │                         │                        │
  │                         │◀──SSE: token_1──────────│ (decode step 1)
  │◀─SSE: {"token":"The"}───│                        │
  │                         │◀──SSE: token_2──────────│ (decode step 2)
  │◀─SSE: {"token":" filing"}│                       │
  │                         │    ...                  │
  │                         │◀──SSE: [DONE]───────────│ (generation complete)
  │◀─SSE: {"done":true,     │                        │
  │    "usage":{...}}───────│                        │
  │                         │                        │
```

**Backpressure for slow clients**: identical to the LLM gateway design (section 7 of the reference). The inference gateway maintains a bounded buffer of 64 un-flushed token events per stream. If the client can't keep up, the gateway stops reading from the vLLM backend (applying TCP backpressure), and if the stall exceeds 2 seconds, the connection is terminated and the vLLM request is cancelled — freeing KV cache blocks for other requests.

---

## 7. Autoscaling Design

### Scaling Signals

| Signal | Source | Used For | Threshold Example |
|---|---|---|---|
| **QPS** (requests/sec per replica) | Envoy sidecar metrics | Traditional ML / deep learning endpoints | Target: 5000 QPS/replica (XGBoost) |
| **GPU Utilization** | NVIDIA DCGM exporter | GPU-based endpoints | Target: 70% (leave headroom for bursts) |
| **GPU Memory Utilization** | NVIDIA DCGM exporter | LLM endpoints (KV cache pressure) | Target: 85% (above this, new requests queue) |
| **Queue Depth** | Serving runtime metrics | All endpoints | Target: 0 (scale up when queue builds) |
| **Tokens/sec** | vLLM metrics | LLM endpoints | Target: 2000 tokens/sec/replica |
| **Inflight Requests** | Serving runtime metrics | LLM endpoints (concurrent streams) | Target: 200 concurrent/replica |
| **Batch Queue Latency** | Dynamic batcher metrics | Deep learning endpoints | Target: < 5ms queue wait |
| **Custom (caller-defined)** | Prometheus query | Any | Arbitrary PromQL expression |

### Autoscaler Controller

```python
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

@dataclass
class AutoscalePolicy:
    metrics: list[dict]                    # list of {type, target, weight}
    min_replicas: int = 1
    max_replicas: int = 100
    scale_up_stabilization_s: int = 0      # no delay on scale-up (react fast)
    scale_down_stabilization_s: int = 300   # 5 min cooldown before scale-down
    scale_up_rate: float = 4.0             # max 4x current replicas per scale-up
    scale_down_rate: float = 0.5           # max halve per scale-down
    scale_to_zero: bool = False
    scale_to_zero_grace_period_s: int = 900  # 15 min of no traffic before zero

@dataclass
class ScalingDecision:
    current_replicas: int
    desired_replicas: int
    reason: str
    metrics_snapshot: dict

class AutoscaleController:
    """
    Custom autoscaler that extends Kubernetes HPA with:
    - Multi-signal scaling (combine GPU util + queue depth + QPS)
    - Predictive pre-scaling from historical patterns
    - Scale-to-zero with cold-start budget
    - GPU-aware scheduling constraints
    """

    def __init__(self, policy: AutoscalePolicy):
        self.policy = policy
        self.scale_up_history: list[datetime] = []
        self.scale_down_history: list[datetime] = []
        self.traffic_predictor = TrafficPredictor()

    def compute_desired_replicas(
        self, current_replicas: int, metrics: dict
    ) -> ScalingDecision:
        """
        Called every 15 seconds by the controller loop.
        
        Algorithm:
        1. For each metric, compute the ratio (current_value / target_value).
        2. The desired replica count per metric = current_replicas * ratio.
        3. Take the MAX across all metrics (any one metric being hot is enough
           to trigger scale-up — we don't want GPU-underutilized but queue-deep).
        4. Apply stabilization windows and rate limits.
        5. Apply predictive pre-scaling if enabled.
        """
        if current_replicas == 0:
            # Scale-from-zero: at least one request arrived (via queue)
            return ScalingDecision(0, 1, "scale-from-zero", metrics)

        # Step 1-2: compute desired replicas per metric
        metric_desires = []
        for metric_config in self.policy.metrics:
            metric_type = metric_config["type"]
            target = metric_config["target"]
            current_value = metrics.get(metric_type, 0)

            if target <= 0:
                continue

            ratio = current_value / target
            desired = math.ceil(current_replicas * ratio)
            metric_desires.append((metric_type, desired, ratio))

        # Step 3: take the max (most aggressive scaling need wins)
        if not metric_desires:
            return ScalingDecision(current_replicas, current_replicas, "no-metrics", metrics)

        max_metric, max_desired, max_ratio = max(metric_desires, key=lambda x: x[1])

        # Step 4: apply rate limits
        if max_desired > current_replicas:
            # Scale-up: cap at scale_up_rate * current
            max_allowed = math.ceil(current_replicas * self.policy.scale_up_rate)
            desired = min(max_desired, max_allowed)
            # No stabilization delay on scale-up
        elif max_desired < current_replicas:
            # Scale-down: cap at scale_down_rate * current
            min_allowed = max(
                math.floor(current_replicas * self.policy.scale_down_rate),
                self.policy.min_replicas
            )
            desired = max(max_desired, min_allowed)
            # Check scale-down stabilization
            if not self._can_scale_down():
                desired = current_replicas
        else:
            desired = current_replicas

        # Step 5: predictive pre-scaling
        predicted = self.traffic_predictor.predict_replicas(
            endpoint=self.endpoint_name,
            horizon_minutes=15,
        )
        if predicted is not None and predicted > desired:
            desired = predicted
            max_metric = "predictive"

        # Clamp to min/max
        desired = max(self.policy.min_replicas, min(desired, self.policy.max_replicas))

        # Scale-to-zero check
        if (
            self.policy.scale_to_zero
            and desired <= self.policy.min_replicas
            and metrics.get("qps", 0) == 0
            and self._zero_traffic_duration() > self.policy.scale_to_zero_grace_period_s
        ):
            desired = 0

        return ScalingDecision(
            current_replicas=current_replicas,
            desired_replicas=desired,
            reason=f"metric={max_metric}, ratio={max_ratio:.2f}",
            metrics_snapshot=metrics,
        )

    def _can_scale_down(self) -> bool:
        if not self.scale_down_history:
            return True
        last_scale_down = self.scale_down_history[-1]
        return (datetime.utcnow() - last_scale_down).total_seconds() > self.policy.scale_down_stabilization_s

    def _zero_traffic_duration(self) -> float:
        """How long (seconds) has this endpoint had zero QPS?"""
        # Queried from Prometheus: time since last non-zero QPS sample
        ...
```

### Predictive Scaling

```python
class TrafficPredictor:
    """
    Uses historical traffic patterns to pre-scale before expected spikes.
    
    Model: weighted average of same time-of-day traffic from the past 4 weeks,
    with exponential decay weighting (most recent weeks weighted more heavily).
    
    This is deliberately simple — a linear model that a platform engineer can
    debug, not a deep learning forecaster that requires ML expertise to operate.
    """

    def predict_replicas(self, endpoint: str, horizon_minutes: int) -> int | None:
        # Query Prometheus for historical QPS at this time-of-day
        # over the past 4 weeks
        now = datetime.utcnow()
        target_time = now + timedelta(minutes=horizon_minutes)

        historical_qps = []
        weights = []
        for weeks_ago in range(1, 5):
            historical_time = target_time - timedelta(weeks=weeks_ago)
            qps = self._query_historical_qps(endpoint, historical_time, window="15m")
            if qps is not None:
                historical_qps.append(qps)
                weights.append(0.5 ** (weeks_ago - 1))  # exponential decay

        if len(historical_qps) < 2:
            return None  # not enough history, skip prediction

        predicted_qps = sum(q * w for q, w in zip(historical_qps, weights)) / sum(weights)

        # Add 20% safety margin
        predicted_qps *= 1.2

        # Convert QPS to replica count using the target QPS/replica
        target_qps_per_replica = self._get_target_qps_per_replica(endpoint)
        return math.ceil(predicted_qps / target_qps_per_replica)
```

### GPU-Aware Scheduling and Bin-Packing

Small models (BERT embedder: 400MB, sentiment classifier: 200MB) should not each get a full A100 (80GB). The platform uses NVIDIA MIG and MPS for bin-packing:

**MIG (Multi-Instance GPU) for Memory Isolation**:
```
A100-80GB MIG Partitioning:

Profile         GPU Memory    Compute (SMs)    Use Case
──────────────  ────────────  ──────────────   ─────────────────────
1g.10gb         10 GB         ~14 SMs          Small models (< 2GB): XGBoost,
                                               small BERT, sentiment classifier
2g.20gb         20 GB         ~28 SMs          Medium models (2-10GB): BERT-large,
                                               ViT, small T5
3g.40gb         40 GB         ~42 SMs          Large models (10-30GB): T5-XL,
                                               13B param models
4g.40gb         40 GB         ~56 SMs          (A100 only) Compute-heavy,
                                               medium memory
7g.80gb         80 GB         ~108 SMs         Full GPU — LLMs, large models

One A100 can be partitioned into:
  7 x 1g.10gb   → 7 small models simultaneously, memory-isolated
  3 x 2g.20gb + 1 x 1g.10gb  → 4 models
  1 x 3g.40gb + 1 x 4g.40gb  → 2 medium models
  1 x 7g.80gb   → 1 large model (no partitioning)
```

**MPS (Multi-Process Service) for Time-Slicing**:

When MIG's fixed memory partitions are too coarse (e.g., 8 models each needing 3GB — MIG can only fit 7 at 10GB each), MPS provides time-slicing without memory isolation:

```yaml
# Kubernetes resource for a MIG-partitioned GPU
resources:
  limits:
    nvidia.com/mig-1g.10gb: 1    # request one 10GB MIG slice

# Kubernetes resource for MPS time-sliced GPU
resources:
  limits:
    nvidia.com/gpu: 1             # full GPU, but MPS shares it
  annotations:
    mps.nvidia.com/max-threads: "25"  # this pod gets 25% of GPU compute time
```

**Scheduling Decision Tree**:
```
Model GPU memory requirement:
  │
  ├── 0 (CPU-only): schedule on CPU node pool
  │
  ├── ≤ 10GB: try MIG-1g.10gb partition first
  │   └── no partition available? try MPS time-slice on a shared GPU
  │       └── no shared GPU has capacity? allocate a new full GPU
  │
  ├── 10-40GB: try MIG-3g.40gb or MIG-4g.40gb
  │   └── no partition available? allocate a full GPU
  │
  ├── 40-80GB: allocate a full A100/H100
  │
  └── > 80GB: allocate multiple GPUs (tensor parallelism)
      └── Must be on the same node with NVLink interconnect
```

### Scale-to-Zero and Cold Start

```python
class ScaleToZeroManager:
    """
    For infrequently-used models (internal tools, dev/staging endpoints),
    scale to zero replicas when idle to save GPU cost.
    
    On the first request after scaling to zero, the request is queued
    while a new replica is started. If the cold-start takes longer than
    the caller's timeout, return 202 Accepted with retry-after.
    """

    async def handle_cold_start_request(self, endpoint: str, request):
        # Check if endpoint is at zero replicas
        if self.get_replica_count(endpoint) == 0:
            # Trigger scale-up
            self.trigger_scale_up(endpoint, target_replicas=1)

            # Wait for the replica to become ready, up to the cold-start budget
            cold_start_budget = self.get_cold_start_budget(endpoint)
            try:
                await asyncio.wait_for(
                    self.wait_for_ready_replica(endpoint),
                    timeout=cold_start_budget.total_seconds(),
                )
                # Replica is ready, forward the request
                return await self.forward_request(endpoint, request)
            except asyncio.TimeoutError:
                # Cold start is taking too long
                return Response(
                    status=202,
                    headers={"Retry-After": "30"},
                    body={"message": "Endpoint is starting up", "retry_after_s": 30},
                )
```

**Cold-start optimization layers**:

| Technique | Latency Saved | Applicable To |
|---|---|---|
| Pre-pulled container images (DaemonSet keeps images warm on all nodes) | 30-120s (skip image pull) | All |
| Model artifact cached on local NVMe (model-cache DaemonSet) | 10-60s (skip S3 download) | All GPU models |
| GPU memory pre-allocation (keep CUDA context warm via MPS) | 2-5s (skip CUDA init) | MIG/MPS shared GPUs |
| Model warm-up requests (run dummy inference to trigger JIT compilation) | 1-5s (skip first-request JIT) | TorchScript, TensorRT |

With all layers: cold start from zero = 15-30s for a CPU model, 30-90s for a GPU model, 60-180s for a multi-GPU LLM.

---

## 8. Traffic Management

### Canary Deployment with Automatic Rollback

```python
from dataclasses import dataclass
from enum import Enum

class RolloutPhase(Enum):
    CANARY_INIT = "canary_init"        # 1% traffic
    CANARY_LOW = "canary_low"          # 5% traffic
    CANARY_MED = "canary_med"          # 25% traffic
    CANARY_HIGH = "canary_high"        # 50% traffic
    CANARY_PROMOTE = "canary_promote"  # 100% traffic (canary becomes primary)
    ROLLED_BACK = "rolled_back"
    COMPLETED = "completed"

@dataclass
class RolloutConfig:
    phases: list[dict] = None          # [{"weight": 1, "duration_s": 300}, ...]
    success_criteria: dict = None       # {"latency_p99_ms_below": 25, "error_rate_below": 0.001}
    failure_criteria: dict = None       # {"latency_p99_ms_above": 50, "error_rate_above": 0.01}
    evaluation_window_s: int = 300
    auto_promote: bool = True
    auto_rollback: bool = True
    min_request_count: int = 100        # need enough data to evaluate

    def __post_init__(self):
        if self.phases is None:
            # Default canary progression
            self.phases = [
                {"weight": 1,  "duration_s": 300},    # 1% for 5 min
                {"weight": 5,  "duration_s": 300},    # 5% for 5 min
                {"weight": 25, "duration_s": 600},     # 25% for 10 min
                {"weight": 50, "duration_s": 600},     # 50% for 10 min
                {"weight": 100, "duration_s": 0},      # promote to 100%
            ]

class CanaryController:
    """
    Manages the progressive rollout of a new model version.
    
    At each phase:
    1. Update the Istio VirtualService to route `weight%` of traffic
       to the canary version.
    2. Wait for `duration_s`, collecting metrics.
    3. Evaluate success/failure criteria.
    4. If success: advance to next phase.
       If failure: rollback immediately to previous version.
       If inconclusive (not enough data): extend the phase.
    """

    def evaluate_canary(
        self, endpoint: str, canary_version: str, baseline_version: str,
        config: RolloutConfig,
    ) -> str:  # "advance" | "rollback" | "hold"
        """Called every evaluation_window_s seconds."""

        canary_metrics = self.fetch_metrics(endpoint, canary_version, window_s=config.evaluation_window_s)
        baseline_metrics = self.fetch_metrics(endpoint, baseline_version, window_s=config.evaluation_window_s)

        # Not enough data to evaluate
        if canary_metrics.request_count < config.min_request_count:
            return "hold"

        # Check failure criteria FIRST (fail fast)
        fc = config.failure_criteria
        if fc:
            if canary_metrics.latency_p99_ms > fc.get("latency_p99_ms_above", float("inf")):
                self._trigger_rollback(endpoint, canary_version, reason=(
                    f"Canary P99 latency {canary_metrics.latency_p99_ms}ms "
                    f"exceeds threshold {fc['latency_p99_ms_above']}ms"
                ))
                return "rollback"
            if canary_metrics.error_rate > fc.get("error_rate_above", 1.0):
                self._trigger_rollback(endpoint, canary_version, reason=(
                    f"Canary error rate {canary_metrics.error_rate:.4f} "
                    f"exceeds threshold {fc['error_rate_above']}"
                ))
                return "rollback"

        # Check success criteria (advance)
        sc = config.success_criteria
        if sc:
            latency_ok = canary_metrics.latency_p99_ms <= sc.get("latency_p99_ms_below", float("inf"))
            error_ok = canary_metrics.error_rate <= sc.get("error_rate_below", 1.0)
            # Also compare against baseline — canary should not be significantly worse
            latency_regression = canary_metrics.latency_p99_ms > baseline_metrics.latency_p99_ms * 1.2
            if latency_ok and error_ok and not latency_regression:
                return "advance"

        return "hold"

    def _trigger_rollback(self, endpoint: str, canary_version: str, reason: str):
        """
        Instant rollback: set canary traffic weight to 0% in the Istio VirtualService.
        The canary pods keep running (for debugging) but receive no traffic.
        This takes effect in < 1 second (Envoy xDS push).
        """
        self.update_traffic_split(endpoint, canary_weight=0)
        self.emit_alert(
            severity="critical",
            message=f"Auto-rollback triggered for {endpoint} canary {canary_version}: {reason}",
        )
        self.record_rollback_event(endpoint, canary_version, reason)
```

### Istio VirtualService for Traffic Splitting

```yaml
# Generated and updated by the Canary Controller
apiVersion: networking.istio.io/v1beta1
kind: VirtualService
metadata:
  name: fraud-detector
  namespace: team-risk
spec:
  hosts:
    - fraud-detector.team-risk.svc.cluster.local
  http:
    - match:
        - headers:
            x-model-version:
              exact: "v24"            # explicit version pinning (for testing)
      route:
        - destination:
            host: fraud-detector-v24
            port:
              number: 8080
    - route:
        - destination:
            host: fraud-detector-v23   # baseline
            port:
              number: 8080
          weight: 95
        - destination:
            host: fraud-detector-v24   # canary
            port:
              number: 8080
          weight: 5
```

### Blue-Green Deployment

```
Phase 1: Green (v23) is live, Blue (v24) is being prepared
═══════════════════════════════════════════════════════════════
Traffic: ──────▶  [v23 pods (Green)]  ← 100% traffic

                   [v24 pods (Blue)]  ← 0% traffic (warming up,
                                        health checks running)

Phase 2: Blue health checks pass, switch traffic atomically
═══════════════════════════════════════════════════════════════
Traffic: ──────▶  [v24 pods (Blue)]   ← 100% traffic (atomic switch via
                                        Istio VirtualService update)

                   [v23 pods (Green)] ← 0% traffic (kept alive for
                                        instant rollback, 30 min TTL)

Phase 3 (if rollback needed): switch back to Green
═══════════════════════════════════════════════════════════════
Traffic: ──────▶  [v23 pods (Green)]  ← 100% traffic (< 1 second switch)

                   [v24 pods (Blue)]  ← 0% traffic (investigation)
```

### A/B Testing with Consistent Assignment

```python
import hashlib

class ABRouter:
    """
    Consistent A/B assignment: the same user always hits the same model
    version for the duration of the experiment, enabling meaningful
    quality comparisons.
    """

    def assign_version(self, experiment_id: str, user_id: str, versions: list[dict]) -> str:
        """
        versions: [{"version": "v23", "weight": 50}, {"version": "v24", "weight": 50}]
        
        Uses a deterministic hash of (experiment_id, user_id) to assign
        the user to a bucket. The same user always gets the same bucket
        for the same experiment, even across different requests and
        different gateway nodes.
        """
        hash_input = f"{experiment_id}:{user_id}".encode()
        hash_value = int(hashlib.sha256(hash_input).hexdigest(), 16)
        bucket = hash_value % 100  # 0-99

        cumulative_weight = 0
        for v in versions:
            cumulative_weight += v["weight"]
            if bucket < cumulative_weight:
                return v["version"]

        return versions[-1]["version"]  # fallback
```

### Shadow Mode

```python
class ShadowRouter:
    """
    Send a copy of production traffic to a new model version without
    serving its responses to users. Used for:
    - Comparing predictions between versions offline
    - Load-testing a new model with real traffic patterns
    - Measuring latency/GPU-utilization of a new version under real load
    """

    async def handle_request(self, request, primary_version: str, shadow_version: str):
        # Fire primary request (this is what the caller gets)
        primary_response = await self.forward(request, primary_version)

        # Fire shadow request asynchronously (caller never sees this)
        asyncio.create_task(self._run_shadow(request, shadow_version, primary_response))

        return primary_response

    async def _run_shadow(self, request, shadow_version, primary_response):
        try:
            shadow_response = await self.forward(request, shadow_version)
            # Log both responses for offline comparison
            self.log_shadow_comparison(
                request=request,
                primary=primary_response,
                shadow=shadow_response,
            )
        except Exception as e:
            # Shadow failures are logged but never affect the primary path
            self.log_shadow_error(request, shadow_version, e)
```

### Instant Rollback Mechanism

Rollback speed tiers:

| Mechanism | Rollback Time | How It Works |
|---|---|---|
| Traffic switch (Istio) | < 1 second | Update VirtualService weight to 0% for bad version. Old version pods are still running. Zero model reload. |
| Pod replacement | 30-120 seconds | If old version pods were already terminated: new pods with old model version are started. Model loaded from NVMe cache. |
| Emergency killswitch | < 1 second | Platform-wide override: any endpoint can be instantly pointed to a known-good "fallback" model version via a single API call that pushes an Istio config update. |

---

## 9. Multi-Model Composition

### Inference Pipelines

Chain models in sequence as a single endpoint:

```yaml
apiVersion: serving.ml-platform.io/v1
kind: InferencePipeline
metadata:
  name: document-processor
  namespace: team-search
spec:
  steps:
    - name: tokenizer
      model: bert-tokenizer/v2
      runtime: custom
      resources:
        cpu: "2"
        memory: "2Gi"
    - name: embedder
      model: bert-embedder/v5
      runtime: triton
      resources:
        nvidia.com/mig-1g.10gb: 1
      dependsOn: [tokenizer]
    - name: classifier
      model: document-classifier/v3
      runtime: onnxruntime
      resources:
        cpu: "2"
        memory: "4Gi"
      dependsOn: [embedder]
    - name: post-processor
      model: label-formatter/v1
      runtime: custom
      resources:
        cpu: "1"
        memory: "1Gi"
      dependsOn: [classifier]
  # Exposed as a single endpoint:
  # POST /v1/endpoints/document-processor/predict
  # Input: raw document text
  # Output: structured classification result
```

### Pipeline Execution Engine

```python
class PipelineExecutor:
    """
    Executes an inference pipeline as a DAG.
    Steps with no dependencies run in parallel.
    Each step is a call to a model endpoint (potentially on different hardware).
    """

    async def execute(self, pipeline: Pipeline, input_data: dict) -> dict:
        """
        Execute the pipeline DAG, passing outputs from completed steps
        as inputs to dependent steps.
        """
        results = {}
        pending = set(pipeline.steps.keys())
        in_flight = {}

        while pending:
            # Find steps whose dependencies are all satisfied
            ready = [
                step_name for step_name in pending
                if all(dep in results for dep in pipeline.steps[step_name].depends_on)
            ]

            if not ready and not in_flight:
                raise PipelineError("Deadlock: no steps are ready and none are in-flight")

            # Launch ready steps in parallel
            for step_name in ready:
                step = pipeline.steps[step_name]
                step_input = self._build_step_input(step, input_data, results)
                task = asyncio.create_task(self._call_step(step, step_input))
                in_flight[step_name] = task
                pending.remove(step_name)

            # Wait for at least one step to complete
            done, _ = await asyncio.wait(
                in_flight.values(),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                step_name = next(k for k, v in in_flight.items() if v is task)
                del in_flight[step_name]
                results[step_name] = task.result()

        return results[pipeline.output_step]
```

### Ensemble Serving

```yaml
apiVersion: serving.ml-platform.io/v1
kind: InferenceEnsemble
metadata:
  name: fraud-ensemble
spec:
  models:
    - name: xgboost-fraud
      model: fraud-xgboost/v12
      weight: 0.4
    - name: nn-fraud
      model: fraud-neural-net/v8
      weight: 0.35
    - name: lgbm-fraud
      model: fraud-lgbm/v5
      weight: 0.25
  aggregation: weighted_average    # weighted_average | majority_vote | stacking
  # All models called in parallel; results aggregated by the ensemble controller.
```

### Router Model Pattern

```python
class ModelRouter:
    """
    A lightweight model that routes the request to one of several
    specialized models based on input characteristics.
    
    Example: language detection -> language-specific translation model.
    """

    async def route_and_predict(self, request):
        # Step 1: Run the router model (fast, CPU-based)
        route_decision = await self.call_model("language-detector/v3", request)
        detected_language = route_decision["language"]

        # Step 2: Route to the specialized model
        model_map = {
            "en": "translation-en/v5",
            "es": "translation-es/v3",
            "zh": "translation-zh/v4",
            "default": "translation-multilingual/v2",
        }
        target_model = model_map.get(detected_language, model_map["default"])

        # Step 3: Call the specialized model
        return await self.call_model(target_model, request)
```

---

## 10. Model Optimization Pipeline

### Pipeline Architecture

```
Model Version (registered)
  │
  ▼
┌───────────────────────────────────────────────────────┐
│  Optimization Pipeline (runs as a Kubernetes Job)       │
│                                                          │
│  Step 1: Validation                                     │
│    - Load model, run sample inference, verify outputs   │
│    - Measure baseline latency and throughput             │
│                                                          │
│  Step 2: Graph Optimization (optional)                  │
│    - ONNX: operator fusion, constant folding            │
│    - TorchScript: torch.jit.optimize_for_inference      │
│    - TensorFlow: grappler passes                        │
│                                                          │
│  Step 3: Quantization (optional)                        │
│    - FP16: straightforward cast, ~0% quality loss       │
│    - INT8: requires calibration data (100-1000 samples) │
│    - INT4 (GPTQ/AWQ): for LLMs, ~1-3% quality loss     │
│                                                          │
│  Step 4: Compilation (optional)                         │
│    - TensorRT: compile graph for specific GPU arch      │
│    - torch.compile: dynamo + inductor backend           │
│                                                          │
│  Step 5: Validation (post-optimization)                 │
│    - Run same sample inference, compare outputs         │
│    - Measure optimized latency and throughput            │
│    - Compute quality delta (accuracy/perplexity change) │
│                                                          │
│  Step 6: Register optimized model as new version        │
│    - parent_version = original version                  │
│    - quantization = "int8" (or whatever was applied)    │
│    - tensorrt_compiled = true                           │
│    - offline_metrics includes quality delta             │
└───────────────────────────────────────────────────────┘
  │
  ▼
New Model Version (registered, linked to parent)
```

### Quantization Configurations

```python
# INT8 quantization for a BERT model using ONNX Runtime
import onnxruntime as ort
from onnxruntime.quantization import quantize_dynamic, quantize_static, QuantType

# Dynamic quantization (no calibration data needed, slightly less accurate)
quantize_dynamic(
    model_input="model_fp32.onnx",
    model_output="model_int8_dynamic.onnx",
    weight_type=QuantType.QInt8,
)

# Static quantization (requires calibration data, more accurate)
from onnxruntime.quantization import CalibrationDataReader

class ModelCalibrationReader(CalibrationDataReader):
    def __init__(self, calibration_dataset):
        self.data = iter(calibration_dataset)
    def get_next(self):
        try:
            return next(self.data)
        except StopIteration:
            return None

quantize_static(
    model_input="model_fp32.onnx",
    model_output="model_int8_static.onnx",
    calibration_data_reader=ModelCalibrationReader(calibration_samples),
    quant_format=ort.quantization.QuantFormat.QDQ,  # Quantize-Dequantize nodes
    activation_type=QuantType.QInt8,
    weight_type=QuantType.QInt8,
)
```

```python
# INT4 quantization for a 70B LLM using AutoAWQ
from awq import AutoAWQForCausalLM
from transformers import AutoTokenizer

model = AutoAWQForCausalLM.from_pretrained("/models/llm-70b-chat/v3")
tokenizer = AutoTokenizer.from_pretrained("/models/llm-70b-chat/v3")

quant_config = {
    "zero_point": True,
    "q_group_size": 128,      # quantize in groups of 128 weights
    "w_bit": 4,                # 4-bit weights
    "version": "GEMM",        # use GEMM kernel (faster on A100/H100)
}

# Calibration: run 128 samples through the model to determine quantization ranges
model.quantize(
    tokenizer,
    quant_config=quant_config,
    calib_data="pileval",      # calibration dataset
    n_samples=128,
)
model.save_quantized("/models/llm-70b-chat/v3-awq-int4/")
```

### TensorRT Compilation

```python
# TensorRT compilation for a ResNet model
import tensorrt as trt
import torch

# Export PyTorch model to ONNX
model = torch.load("resnet50.pt")
dummy_input = torch.randn(1, 3, 224, 224).cuda()
torch.onnx.export(model, dummy_input, "resnet50.onnx",
                  input_names=["input"], output_names=["output"],
                  dynamic_axes={"input": {0: "batch_size"}})

# Compile ONNX to TensorRT engine
logger = trt.Logger(trt.Logger.WARNING)
builder = trt.Builder(logger)
network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
parser = trt.OnnxParser(network, logger)

with open("resnet50.onnx", "rb") as f:
    parser.parse(f.read())

config = builder.create_builder_config()
config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)  # 1GB workspace
config.set_flag(trt.BuilderFlag.FP16)   # enable FP16 (2x speedup on A100)
# config.set_flag(trt.BuilderFlag.INT8) # enable INT8 (requires calibrator)

# Set dynamic batch size range
profile = builder.create_optimization_profile()
profile.set_shape("input",
    min=(1, 3, 224, 224),      # minimum batch size
    opt=(16, 3, 224, 224),     # optimal batch size (most common)
    max=(64, 3, 224, 224),     # maximum batch size
)
config.add_optimization_profile(profile)

engine = builder.build_serialized_network(network, config)
with open("resnet50.trt", "wb") as f:
    f.write(engine)
```

### Optimization Impact Matrix

| Model Type | Optimization | Size Change | Latency Change | Quality Change | GPU Memory Change |
|---|---|---|---|---|---|
| XGBoost (CPU) | ONNX conversion | -20% | -40% (ONNX Runtime faster than native) | 0% | N/A (CPU) |
| BERT-base | FP16 | -50% | -30% | < 0.1% | -50% |
| BERT-base | INT8 static | -75% | -50% | -0.3% | -75% |
| BERT-base | TensorRT FP16 | -60% | -60% | < 0.1% | -55% |
| ResNet-50 | TensorRT INT8 | -80% | -70% | -0.5% | -75% |
| 70B LLM | FP16 (baseline) | 140GB | baseline | baseline | 140GB |
| 70B LLM | AWQ INT4 | 35GB | +10% throughput | -1.5% perplexity | 35GB (4x less) |
| 70B LLM | GPTQ INT4 | 35GB | +15% throughput | -2% perplexity | 35GB |

---

## 11. Capacity Estimates

### Fleet Sizing for 2M Aggregate QPS

**Step 1: Classify the workload mix.**

Based on the problem statement (100+ teams, 500+ endpoints), a realistic workload distribution:

```
Workload Tier           % of 2M QPS    QPS        Typical Model           Hardware
────────────────────    ───────────    ─────────  ──────────────────────  ──────────────
Traditional ML (CPU)    85%            1,700,000  XGBoost, LightGBM,     CPU (4-8 cores)
                                                   sklearn, small ONNX

Deep Learning (GPU)     12%            240,000    BERT, ResNet, T5-base  GPU (MIG slices)

LLM (multi-GPU)         3%             60,000     7B-70B param LLMs      GPU (1-4x A100)
────────────────────    ───────────    ─────────
Total                   100%           2,000,000
```

**Step 2: Traditional ML fleet sizing.**

```
Target: 1,700,000 QPS on CPU

Per-replica throughput (XGBoost on ONNX Runtime, 4-core):
  Batch size 1:  ~10,000 QPS per replica
  Batch size 32: ~25,000 QPS per replica (with dynamic batching)

Using batch-size-32 throughput: 25,000 QPS/replica
Replicas needed: 1,700,000 / 25,000 = 68 replicas

With 70% target utilization (headroom for bursts):
  68 / 0.70 = ~97 replicas

CPU resources: 97 replicas * 4 cores = 388 cores
  → ~25 nodes of 16 vCPU each

Memory: 97 replicas * 8 GB = 776 GB
  → ~25 nodes of 32 GB each (comfortably fits)

Add 50% buffer for peak traffic (2x daily/weekly patterns):
  → ~38 CPU nodes (total fleet capacity: 608 cores, 1.2 TB RAM)
```

**Step 3: Deep Learning fleet sizing.**

```
Target: 240,000 QPS on GPU

Model mix within deep learning:
  - 60% small (BERT-base): 144,000 QPS
  - 30% medium (ViT-Large): 72,000 QPS
  - 10% large (T5-XL): 24,000 QPS

BERT-base on A100 MIG-1g.10gb (with Triton, batch size 32):
  Throughput: ~4,000 QPS per MIG instance
  Instances needed: 144,000 / 4,000 = 36
  A100 GPUs (7 MIG instances each): 36 / 7 = ~6 A100 GPUs

ViT-Large on A100 MIG-3g.40gb (with Triton, batch size 16):
  Throughput: ~800 QPS per MIG instance
  Instances needed: 72,000 / 800 = 90
  A100 GPUs (2 MIG-3g instances each): 90 / 2 = 45 A100 GPUs

T5-XL on A100-80GB full (with Triton, batch size 8):
  Throughput: ~200 QPS per GPU
  GPUs needed: 24,000 / 200 = 120 A100 GPUs

Total deep learning GPUs: 6 + 45 + 120 = 171 A100 GPUs

At 70% utilization target: 171 / 0.70 = ~244 A100 GPUs
With 30% burst headroom: ~317 A100 GPUs
  → ~40 GPU nodes (8 GPUs each)
```

**Step 4: LLM fleet sizing.**

```
Target: 60,000 QPS across LLM endpoints

LLM workload breakdown:
  - 70% small LLMs (7B): 42,000 QPS
  - 20% medium LLMs (13B): 12,000 QPS
  - 10% large LLMs (70B): 6,000 QPS

7B LLM on 1x A100-80GB (vLLM, continuous batching):
  Throughput: ~1,500 output tokens/sec/GPU
  Average output: 100 tokens/request → ~15 QPS/GPU
  But many are short responses (classification, extraction):
  Average output mix: 30 tokens/request → ~50 QPS/GPU
  GPUs needed: 42,000 / 50 = 840 A100 GPUs

  With INT4 quantization (7B fits easily in 1 GPU):
  Throughput doubles (compute-bound now, not memory-bound):
  ~100 QPS/GPU → GPUs needed: 42,000 / 100 = 420 GPUs

13B LLM on 1x A100-80GB (vLLM, FP16):
  Throughput: ~800 output tokens/sec/GPU
  At 30 tokens/request: ~27 QPS/GPU
  GPUs needed: 12,000 / 27 = 444 GPUs

  With INT8 (13B INT8 = 6.5GB, plenty of KV cache room):
  ~50 QPS/GPU → GPUs needed: 12,000 / 50 = 240 GPUs

70B LLM on 4x A100-80GB (vLLM, FP16, tensor parallel):
  Throughput: ~400 output tokens/sec across 4 GPUs
  At 50 tokens/request: ~8 QPS per 4-GPU instance
  Instances needed: 6,000 / 8 = 750 instances
  GPUs needed: 750 * 4 = 3,000 GPUs  (!!!)

  This is clearly not feasible. Optimization is required:
  
  With INT4 quantization (70B INT4 = 35GB, fits in 1x A100-80GB):
  Throughput: ~300 tokens/sec/GPU (fewer GPUs, but each GPU does more)
  At 50 tokens/request: ~6 QPS/GPU (single-GPU now!)
  GPUs needed: 6,000 / 6 = 1,000 GPUs

  Better. But 6K QPS of 70B LLM is massive. Realistically:
  - Most 70B requests are lower-QPS, higher-value (complex reasoning)
  - Typical 70B endpoint: 100-500 QPS, not 6,000
  - Prefix caching reduces effective compute by ~30%
  - Speculative decoding increases throughput by ~1.5x
  
  Revised with optimizations:
  Effective throughput: 6 * 1.3 (prefix caching) * 1.5 (speculative) = ~12 QPS/GPU
  GPUs needed: 6,000 / 12 = 500 GPUs
```

**Fleet Summary**:

```
                        GPUs (A100)    CPU Nodes    Notes
──────────────────────  ───────────    ─────────    ──────────────────────
Traditional ML          0              38           CPU-only workloads
Deep Learning           317            0            MIG-partitioned A100s
LLM (7B, quantized)     420            0            INT4, 1 GPU each
LLM (13B, quantized)    240            0            INT8, 1 GPU each
LLM (70B, optimized)    500            0            INT4 + prefix cache + speculative
Platform overhead        23             5           Monitoring, routing, controllers
──────────────────────  ───────────    ─────────    ──────────────────────
Total                   1,500 GPUs     43 CPU nodes

GPU nodes (8 GPUs each): 1,500 / 8 = ~188 GPU nodes
Total fleet: 188 GPU nodes + 43 CPU nodes = 231 nodes

Validation against task constraint: 2,000+ GPUs managed.
Our estimate of 1,500 GPUs assumes aggressive quantization.
Without quantization: ~2,400 GPUs needed.
The platform should be designed for 2,000-2,500 GPU capacity.
```

### GPU Memory Budget Summary

| Model | Weights | KV Cache Budget | Max Concurrent (4K ctx) | Hardware |
|---|---|---|---|---|
| 7B FP16 | 14 GB | 58 GB | ~460 requests | 1x A100-80GB |
| 7B INT4 | 3.5 GB | 68 GB | ~540 requests | 1x A100-80GB |
| 13B FP16 | 26 GB | 46 GB | ~150 requests | 1x A100-80GB |
| 13B INT8 | 13 GB | 59 GB | ~190 requests | 1x A100-80GB |
| 70B FP16 | 140 GB | 148 GB (across 4 GPUs) | ~118 requests | 4x A100-80GB |
| 70B INT4 | 35 GB | 37 GB | ~30 requests | 1x A100-80GB |
| 70B INT8 | 70 GB | 74 GB (across 2 GPUs) | ~59 requests | 2x A100-80GB |

### Storage Estimates

```
Model artifact storage:
  500 model endpoints * ~5 versions each = 2,500 model versions
  Size distribution:
    - 2,000 traditional ML / small DL models: avg 500 MB each = 1 TB
    - 400 medium DL models: avg 5 GB each = 2 TB
    - 100 LLM variants: avg 70 GB each = 7 TB
  Total model storage: ~10 TB

  With 2x replication in S3: 20 TB
  With local NVMe cache on 188 GPU nodes:
    188 nodes * 2 TB NVMe cache budget each = 376 TB local cache
    (not all models are cached on all nodes — LRU eviction)

Metrics/observability storage:
  2M QPS * 200 bytes/metric point * 3 metrics/request = 1.2 GB/sec
  Retention at 15s aggregation: ~30 GB/day raw, ~1 GB/day aggregated
  30-day retention: ~30 GB aggregated + 100 GB raw (sampled at 1%)

Prediction logs (for drift detection):
  1% sampling of 2M QPS = 20,000 predictions/sec * 1 KB each = 20 MB/sec
  30-day retention: ~52 TB
  → Store in object store (S3), compressed: ~10 TB
```

---

## 12. Failure Walkthroughs

### Scenario 1: GPU OOM During Inference

```
t+0s     A serving pod processes a request with an unusually large input
         (e.g., a 32K-token prompt on an LLM that was configured for 8K max).
         The KV cache allocation exceeds available GPU memory.

t+0s     CUDA OOM error raised inside vLLM/Triton.
         
         For vLLM (continuous batching):
           vLLM handles this internally — it preempts the oversized request,
           swaps its KV cache blocks to CPU memory, and continues serving
           other requests. The preempted request is re-queued and retried
           when GPU memory frees up. No pod crash.

         For Triton (static batching):
           The specific inference call fails with an error.
           Triton catches the CUDA error and returns a 500 to that request.
           Other requests in the same batch MAY also fail (CUDA error
           is device-wide — a CUDA OOM can corrupt the device state).

t+0-1s   The pod's liveness probe detects the unhealthy state (for Triton:
         /v2/health/live returns 503 after a CUDA error).

t+1-5s   Kubernetes marks the pod as NotReady, removing it from the
         Envoy load balancer's endpoint list. No new traffic is routed
         to this pod.

t+5-30s  Kubernetes restarts the pod (restartPolicy: Always).
         Init container re-validates model from NVMe cache (fast, no re-download).
         Model reloaded into GPU memory.
         Readiness probe passes.
         Pod re-enters the load balancer pool.

Impact:
  - Single request fails (or is preempted and retried for vLLM).
  - Other replicas continue serving unaffected.
  - If this was the last healthy replica: callers see 503 for 5-30 seconds
    until the pod recovers or autoscaler adds a new one.

Prevention:
  - Enforce max_model_len in vLLM configuration.
  - Schema validation at the gateway rejects inputs exceeding max sequence length.
  - Set appropriate resource limits so a pod can't allocate more GPU memory
    than its MIG partition allows.
```

### Scenario 2: Model Returns Garbage Predictions

```
t+0      A new model version v24 is deployed via canary at 5% traffic.
         The model was trained on incorrectly preprocessed data (subtle bug:
         feature normalization was wrong in the training pipeline).
         The model loads successfully and health checks pass —
         it returns valid-shaped predictions, just wrong ones.

t+0-5m   Canary is serving 5% of traffic. Predictions are plausible-shaped
         (valid JSON, correct output schema) but semantically wrong
         (fraud scores are near-random instead of calibrated).

t+5m     Canary controller evaluates metrics for the first time:

         Option A: Latency/error-rate detection.
           If the garbage predictions happen to be fast and don't cause
           downstream errors: canary metrics look FINE. Error rate is low,
           latency is good. This is the dangerous case — the canary
           controller does NOT catch it.

         Option B: Prediction distribution drift detection catches it.
           The observability sidecar samples predictions and sends them to
           the drift detection service. The drift detector compares v24's
           output distribution against v23's baseline:
             - v23 fraud scores: mean=0.12, std=0.18 (mostly low, some high)
             - v24 fraud scores: mean=0.49, std=0.28 (near-uniform random)
           KL-divergence between distributions exceeds alert threshold.

t+5-10m  Drift alert fires: "Model fraud-detector/v24 output distribution
         significantly different from baseline v23."

         If auto_rollback_on_drift is enabled:
           Canary controller immediately sets v24 traffic weight to 0%.
           Alert escalated to model owner team.
         
         If not:
           Alert goes to model owner team for manual investigation.
           Canary stays at 5% until human action.

Lesson: Canary metrics (latency, error rate) are necessary but not
sufficient. Output distribution monitoring is the only automated way to
catch semantically-wrong-but-structurally-valid predictions.
```

### Scenario 3: Autoscaler Thrashing

```
t+0      The fraud-detector endpoint receives bursty traffic:
         2000 QPS for 30s, then 200 QPS for 30s, repeating.

t+0s     Autoscaler sees QPS spike: current=5 replicas, target=5000 QPS/replica.
         2000 QPS / 5000 target = 0.4 ratio → desired = ceil(5 * 0.4) = 2.
         Wait — that's a scale-down. But queue_depth is also spiking because
         the burst overwhelms 5 replicas momentarily.
         Queue depth metric: target=0, current=50 → ratio=∞ → scale UP.
         Max metric wins: desired = scale up.

t+15s    Autoscaler scales up from 5 to 10 replicas.

t+30s    Traffic drops to 200 QPS. Queue drains immediately.
         Autoscaler sees: QPS ratio = 200/5000 * 10 replicas = 0.004.
         Desired: 1 replica. But scale_down_stabilization = 300s.
         Autoscaler holds at 10 replicas.

t+60s    Traffic spikes again to 2000 QPS. 10 replicas handle it easily.
         No scaling action needed.

t+330s   (300s after last scale-up) Scale-down stabilization window expires.
         Traffic is at 200 QPS. Autoscaler scales down from 10 to 5
         (scale_down_rate = 0.5, so max halve: 10 * 0.5 = 5).

t+360s   Traffic spikes to 2000 QPS again. 5 replicas are enough
         (5 * 5000 QPS/replica = 25,000 capacity), but queue spikes briefly.

The scale_down_stabilization_s = 300 prevents thrashing:
  Without it: scale up at t+0, scale down at t+30, scale up at t+60, ...
  With it: scale up at t+0, hold for 300s, then cautiously scale down once.

Further protection:
  - Predictive scaling (section 7) recognizes the periodic pattern after
    2-3 cycles and pre-scales to 10 before the next expected spike.
  - The 4x scale_up_rate cap prevents panic-scaling from 5 to 100 on a
    single QPS spike.
```

### Scenario 4: Bad Model Deployment Cascading to Dependent Services

```
t+0      Team A deploys a new version of "embedding-model/v12" that
         has a memory leak — it slowly consumes more GPU memory over time.
         This endpoint is used by 15 downstream services.

t+0-60m  GPU memory usage slowly climbs from 4GB to 8GB to 12GB.
         The model runs on a MIG-3g.40gb partition (40GB limit).
         Performance is fine. No alerts yet.

t+60m    GPU memory reaches 35GB. CUDA allocations start failing for
         new requests. Error rate spikes from 0% to 15%.

t+60m    Alert fires: "embedding-model error rate > 5% for 5 minutes."
         Autoscaler sees error rate, but can't help — it's a per-pod
         memory leak, not a load issue. More replicas would just be
         more leaky replicas.

t+61m    Downstream services see embedding requests failing.
         They have retry logic and circuit breakers:
         - Services with retries: succeed on retry to a different replica
           (not all replicas are equally affected — the leak depends on
           time since pod start, and not all pods started at the same time).
         - Services with circuit breakers: if >50% of embedding requests
           fail, breaker opens, and the downstream service fails gracefully
           (returns cached embeddings, or degrades to non-ML fallback).

Blast radius containment:
  1. Namespace isolation: embedding-model runs in team-a's namespace.
     The GPU memory leak cannot affect team-b's models on different GPUs.
  2. MIG isolation: even on the same physical GPU, MIG partitions have
     hard memory isolation. embedding-model's leak cannot steal memory
     from another model's MIG partition.
  3. Pod resource limits: Kubernetes OOM-kills the pod if it exceeds
     its memory limit (for CPU memory; for GPU memory, CUDA OOM fires
     and the pod is restarted by the kubelet).

Resolution:
  t+62m  On-call rolls back embedding-model to v11 via:
           ml-platform rollback embedding-model --to-version v11
         This updates the Kubernetes deployment to v11's artifact,
         triggers a rolling restart. All pods converge to v11 within 60s.
  t+63m  New pods with v11 are healthy. Downstream services recover
         as circuit breakers close.
```

---

## 13. Cost Model

### GPU Cost Attribution

```python
@dataclass
class GPUCostRecord:
    """
    Emitted every 10 seconds per serving pod by the cost attribution sidecar.
    """
    timestamp: datetime
    team: str
    endpoint_name: str
    model_id: str
    model_version: str
    node_id: str
    gpu_id: str
    gpu_type: str              # 'a100-80gb' | 'h100-80gb'
    allocation_type: str       # 'full' | 'mig-1g.10gb' | 'mig-3g.40gb' | 'mps-25pct'
    gpu_seconds: float         # 10 seconds * allocation fraction
    gpu_utilization_avg: float # average GPU compute utilization in this window
    gpu_memory_used_gb: float
    request_count: int         # requests served in this window
    cost_usd: float            # computed from gpu_seconds * rate

# Cost rates (internal pricing, updated quarterly)
GPU_COST_RATES = {
    "a100-80gb": {
        "full": 3.50,           # $/hour for a full A100
        "mig-1g.10gb": 0.50,   # $/hour for a 10GB MIG slice
        "mig-2g.20gb": 1.00,
        "mig-3g.40gb": 1.75,
        "mig-7g.80gb": 3.50,   # same as full GPU
    },
    "h100-80gb": {
        "full": 5.50,
        "mig-1g.10gb": 0.80,
        "mig-3g.40gb": 2.75,
    },
    "cpu": {
        "per_core_hour": 0.05,
    },
}
```

### Bin-Packing Savings Analysis

```
Without bin-packing (each model gets a full A100):
  171 deep-learning models, each needing < 10GB GPU memory.
  171 * A100 full GPU = 171 * $3.50/hr = $598.50/hr = $431,892/month

With MIG bin-packing:
  171 models, packed 7 per A100 (MIG-1g.10gb partitions):
  ceil(171 / 7) = 25 A100 GPUs
  25 * $3.50/hr = $87.50/hr = $63,000/month

  Savings: $431,892 - $63,000 = $368,892/month (85% savings!)

  In practice, not all models fit in 10GB — realistic bin-packing:
    120 small models (< 2GB) → 18 A100s with MIG-1g.10gb (7 per GPU)
    36 medium models (2-10GB) → 18 A100s with MIG-2g.20gb (3-4 per GPU)
    15 large models (10-30GB) → 8 A100s with MIG-3g.40gb (2 per GPU)
    
    Total: 44 A100 GPUs = $154/hr = $110,880/month
    
    Savings vs. no bin-packing: $321,012/month (74% savings)
```

### Spot Instance Savings

```
Workloads eligible for spot/preemptible instances:
  - Shadow mode traffic (section 8): not serving real users
  - Batch inference endpoints (tier-3, latency-tolerant)
  - Development/staging endpoints
  - Optimization pipeline jobs (section 10)

Spot vs. on-demand GPU pricing (typical cloud):
  A100 on-demand: $3.50/hr
  A100 spot:      $1.05/hr (70% discount, typical)

Estimated spot-eligible fraction: 20% of GPU fleet = 300 GPUs
  On-demand cost: 300 * $3.50 = $1,050/hr
  Spot cost: 300 * $1.05 = $315/hr
  Savings: $735/hr = $529,200/month

Spot interruption handling:
  - Spot instances get a 30-second warning before termination.
  - On warning: drain in-flight requests (complete current batch,
    reject new requests with 503).
  - Autoscaler immediately requests replacement capacity (on-demand
    if spot is unavailable).
  - For LLM workloads: active KV cache is lost on interruption.
    In-flight streams are terminated with an error.
    Clients retry to a surviving replica.
```

### Per-Endpoint Chargeback Report

```sql
-- Monthly chargeback report per team
SELECT
    team,
    endpoint_name,
    model_id,
    gpu_type,
    allocation_type,
    SUM(gpu_seconds) / 3600 AS gpu_hours,
    SUM(cost_usd) AS total_cost_usd,
    SUM(request_count) AS total_requests,
    SUM(cost_usd) / NULLIF(SUM(request_count), 0) * 1000 AS cost_per_1k_requests,
    AVG(gpu_utilization_avg) AS avg_gpu_utilization
FROM gpu_cost_records
WHERE timestamp >= date_trunc('month', now())
GROUP BY team, endpoint_name, model_id, gpu_type, allocation_type
ORDER BY total_cost_usd DESC;

-- Example output:
-- team           endpoint             gpu_type    allocation    gpu_hours  cost_usd   requests    $/1K req   gpu_util
-- ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
-- ml-search      llm-70b-chat         a100-80gb   full (4x TP)  11,520    $40,320    5.2M        $7.75      78%
-- risk-analytics fraud-detector       a100-80gb   mig-1g.10gb   720       $360       890M        $0.0004    62%
-- recommendations product-embedder   a100-80gb   mig-3g.40gb   1,440     $2,520     120M        $0.021     71%
-- ads            ctr-predictor        cpu         4-core         2,880     $576       1.2B        $0.0005    45%
```

### Right-Sizing Recommendations

```python
class RightSizingAdvisor:
    """
    Runs daily, analyzes GPU utilization per endpoint, and generates
    right-sizing recommendations.
    """

    def analyze_endpoint(self, endpoint: str, lookback_days: int = 7) -> list[Recommendation]:
        recommendations = []
        metrics = self.fetch_metrics(endpoint, lookback_days)

        # Under-utilized GPU: could use a smaller instance or MIG partition
        if metrics.avg_gpu_util < 30 and metrics.avg_gpu_memory_util < 40:
            current_type = self.get_instance_type(endpoint)
            if current_type == "gpu-a100-full":
                recommendations.append(Recommendation(
                    endpoint=endpoint,
                    type="downsize",
                    current="gpu-a100-full ($3.50/hr)",
                    suggested="mig-3g.40gb ($1.75/hr)",
                    estimated_savings_monthly=self.compute_savings(current_type, "mig-3g.40gb", metrics),
                    confidence="high",
                    reason=f"Avg GPU util {metrics.avg_gpu_util:.0f}%, memory util {metrics.avg_gpu_memory_util:.0f}%",
                ))

        # Over-provisioned replicas: could reduce min_replicas
        if metrics.avg_qps_per_replica < metrics.target_qps_per_replica * 0.3:
            recommendations.append(Recommendation(
                endpoint=endpoint,
                type="reduce_replicas",
                current=f"min_replicas={metrics.current_min_replicas}",
                suggested=f"min_replicas={max(1, metrics.current_min_replicas // 2)}",
                estimated_savings_monthly=self.compute_replica_savings(metrics),
                confidence="medium",
                reason=f"Avg {metrics.avg_qps_per_replica:.0f} QPS/replica vs target {metrics.target_qps_per_replica}",
            ))

        # Scale-to-zero candidate: long periods of zero traffic
        if metrics.zero_traffic_hours_per_day > 16:
            recommendations.append(Recommendation(
                endpoint=endpoint,
                type="enable_scale_to_zero",
                current="always-on",
                suggested="scale-to-zero with 120s cold-start budget",
                estimated_savings_monthly=self.compute_zero_savings(metrics),
                confidence="high",
                reason=f"Zero traffic {metrics.zero_traffic_hours_per_day:.1f} hrs/day",
            ))

        return recommendations
```

---

## 14. Trade-offs

| Decision | What We Chose | What We Gave Up | Why |
|---|---|---|---|
| **Dynamic batching latency vs. throughput** | Configurable max_wait_time_ms per endpoint (default 10ms for traditional ML, 50ms for deep learning, 0 for LLMs with continuous batching) | One-size-fits-all simplicity | A fraud detection model at 50K QPS cannot tolerate 50ms batch-wait; a ResNet image classifier at 1K QPS benefits from larger batches. The endpoint owner must set this based on their latency SLO, and the platform provides guidance but not a universal default. |
| **Quantization quality vs. speed** | Platform offers quantization as an opt-in optimization step with mandatory quality validation (section 10). The model owner must approve the quality delta before the quantized version is deployable. | Automatic quantization of all models without human approval | INT8 quantization is lossless for many models but can degrade quality for edge cases (adversarial inputs, distribution tails). INT4 for LLMs measurably degrades perplexity. Automatic deployment of a quantized model that the owner never validated is a silent quality regression — worse than the cost savings justify. |
| **Scale-to-zero cold-start vs. cost** | Opt-in per endpoint. Disabled by default for tier-1 (always warm). Enabled with configurable cold-start budget for tier-2/3 low-traffic endpoints. | Constant cost for all endpoints | A tier-1 endpoint that cold-starts on Black Friday traffic costs real revenue. A team-internal prototype that gets 10 requests/day should not hold an A100 24/7. The tier system makes this explicit. |
| **Model-level isolation (MIG) vs. request-level isolation (full GPU per model)** | MIG for small/medium models (memory-isolated partitions on shared GPUs). Full GPU only for models that genuinely need 40-80GB. | Simplicity of "one model, one GPU" and MPS for even finer-grained sharing | MIG provides hardware-enforced memory isolation — a misbehaving model in one MIG partition cannot corrupt another partition's GPU memory. MPS (time-slicing without memory isolation) is faster to set up but does not prevent one model's CUDA OOM from affecting another. We use MIG as the default for bin-packing and reserve MPS only for the case where MIG partitions are too coarse. Full GPU isolation is available for models that need it (large models) or teams that require it (compliance). |
| **vLLM vs. TensorRT-LLM for LLM serving** | vLLM as the primary LLM runtime; TensorRT-LLM as an optional high-performance alternative for latency-critical endpoints | Uniformity (one LLM runtime for all) | vLLM is pure Python, easy to debug, and has the broadest model support. TensorRT-LLM is faster (10-30% lower latency) but requires GPU-specific compilation, has a narrower model compatibility matrix, and is harder to debug. A platform team of 5-8 engineers can operate vLLM; TensorRT-LLM requires deeper NVIDIA toolchain expertise. We offer TensorRT-LLM for endpoints where the latency difference justifies the operational cost. |
| **Istio VirtualService for traffic splitting vs. application-level routing** | Istio (infrastructure-level, works with any serving runtime) | Application-level routing that could be more model-aware | Istio's weighted routing, canary, and A/B support are battle-tested and runtime-agnostic. An application-level router could make routing decisions based on model confidence or input features, but that requires model-specific logic in the router, which breaks the platform's abstraction boundary. Model-aware routing is supported via the Router Model pattern (section 9) where the routing model is itself a model endpoint, keeping the platform layer clean. |
| **Kubernetes CRDs + custom controllers vs. a standalone orchestration service** | CRDs: InferenceService, ModelVersion, AutoscalePolicy — reconciled by custom controllers running in the cluster | A standalone orchestration service that could be more flexible and testable outside Kubernetes | The serving pods already run on Kubernetes; adding a standalone orchestrator means maintaining two state machines (orchestrator's view and Kubernetes's view) that can diverge. CRDs make Kubernetes itself the source of truth, and Kubernetes reconciliation loops provide the retry/convergence semantics for free. The downside is Kubernetes-lock-in and the learning curve for CRD development, but since the constraint says the platform runs on Kubernetes, this is a strength, not a limitation. |

---

## 15. Observability

### Per-Endpoint Metrics

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `ml_serving_request_duration_ms` | Histogram | endpoint, model, version, runtime, status_code | End-to-end latency |
| `ml_serving_platform_overhead_ms` | Histogram | endpoint | Gateway-added latency (the SLO metric: P99 < 5ms) |
| `ml_serving_request_total` | Counter | endpoint, model, version, status_code | QPS and error rate |
| `ml_serving_gpu_utilization` | Gauge | node, gpu_id, endpoint | GPU compute utilization (0-100%) |
| `ml_serving_gpu_memory_used_bytes` | Gauge | node, gpu_id, endpoint | GPU memory consumption |
| `ml_serving_batch_size` | Histogram | endpoint | Actual batch sizes dispatched |
| `ml_serving_queue_depth` | Gauge | endpoint, replica | Requests waiting for inference |
| `ml_serving_queue_wait_ms` | Histogram | endpoint | Time spent waiting in batch queue |
| `ml_serving_tokens_per_sec` | Gauge | endpoint | LLM token throughput |
| `ml_serving_kv_cache_utilization` | Gauge | endpoint, replica | KV cache memory usage (LLM) |
| `ml_serving_ttft_ms` | Histogram | endpoint | Time-to-first-token (LLM) |
| `ml_serving_inter_token_latency_ms` | Histogram | endpoint | Inter-token latency (LLM) |
| `ml_serving_concurrent_streams` | Gauge | endpoint | Active SSE streams (LLM) |
| `ml_serving_prediction_drift_score` | Gauge | endpoint | Output distribution divergence from baseline |
| `ml_serving_cost_usd_total` | Counter | team, endpoint | Cumulative GPU cost |

### Distributed Tracing

```
Trace span tree per inference request:

ml_serving.request  (root span)
 ├─ ml_serving.authn                         (0.2ms)
 ├─ ml_serving.schema_validation             (0.3ms)
 ├─ ml_serving.rate_limit_check              (0.1ms)
 ├─ ml_serving.traffic_routing               (0.2ms)
 │    attributes: version_assigned, canary_flag, ab_variant
 ├─ ml_serving.load_balancer                 (0.1ms)
 │    attributes: selected_replica, reason
 ├─ ml_serving.model_inference               (varies by model type)
 │    attributes: model_id, version, runtime, batch_size, gpu_id
 │    ├─ ml_serving.batch_queue_wait         (0-10ms, dynamic batching)
 │    ├─ ml_serving.preprocessing            (0-5ms, if pipeline)
 │    ├─ ml_serving.gpu_inference            (1-400ms, the actual compute)
 │    └─ ml_serving.postprocessing           (0-2ms)
 └─ ml_serving.response_serialize            (0.3ms)
```

### Model Quality Monitoring (Drift Detection)

```python
class DriftDetector:
    """
    Detects when a model's prediction distribution has shifted significantly
    from a reference baseline, which may indicate:
    - Input feature drift (upstream data pipeline changed)
    - Model staleness (the world changed, model hasn't been retrained)
    - Bug in a new model version (caught by canary, section 8)
    """

    def compute_drift(
        self, endpoint: str, reference_window: str = "7d", current_window: str = "1h",
    ) -> DriftReport:
        # Fetch sampled predictions from both windows
        reference = self.fetch_predictions(endpoint, window=reference_window)
        current = self.fetch_predictions(endpoint, window=current_window)

        report = DriftReport(endpoint=endpoint)

        # Output distribution drift (KL divergence for classification,
        # Wasserstein distance for regression)
        if self.is_classification(endpoint):
            report.output_drift = self.kl_divergence(
                reference.class_distribution, current.class_distribution,
            )
            report.output_drift_threshold = 0.1  # KL > 0.1 is significant
        else:
            report.output_drift = self.wasserstein_distance(
                reference.output_values, current.output_values,
            )

        # Input feature drift (Population Stability Index per feature)
        for feature_name in reference.feature_names:
            psi = self.population_stability_index(
                reference.features[feature_name],
                current.features[feature_name],
            )
            if psi > 0.2:  # PSI > 0.2 indicates significant drift
                report.drifted_features.append((feature_name, psi))

        return report
```

### Alerting Rules

```yaml
# Prometheus alerting rules
groups:
  - name: ml-serving-slos
    rules:
      - alert: HighErrorRate
        expr: |
          sum(rate(ml_serving_request_total{status_code=~"5.."}[5m])) by (endpoint)
          / sum(rate(ml_serving_request_total[5m])) by (endpoint)
          > 0.01
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Endpoint {{ $labels.endpoint }} error rate > 1%"

      - alert: HighLatency
        expr: |
          histogram_quantile(0.99, 
            sum(rate(ml_serving_request_duration_ms_bucket[5m])) by (le, endpoint)
          ) > on(endpoint) group_left ml_serving_target_latency_p99_ms
        for: 5m
        labels:
          severity: warning

      - alert: GPUMemoryPressure
        expr: |
          ml_serving_gpu_memory_used_bytes / ml_serving_gpu_memory_total_bytes > 0.95
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "GPU {{ $labels.gpu_id }} on {{ $labels.node }} at 95% memory"

      - alert: PredictionDrift
        expr: |
          ml_serving_prediction_drift_score > 0.2
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "Model {{ $labels.endpoint }} output distribution has drifted"

      - alert: AutoscalerQueueBuildup
        expr: |
          ml_serving_queue_depth > 100
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Queue depth > 100 for {{ $labels.endpoint }} — autoscaler may be too slow"
```

---

## 16. Evolution Path

| Version | Scope | What's Added | What's Deliberately Deferred |
|---|---|---|---|
| **v1** | Core serving | Model registry (versions, artifacts, metadata), deployment controller (CRDs, single-version deploy), serving runtimes (ONNX Runtime for CPU, Triton for GPU, vLLM for LLM), health checking, basic HPA autoscaling (QPS + GPU utilization), Envoy-based routing (no canary yet), basic metrics (latency, error rate, GPU util). | No canary/blue-green, no A/B testing, no shadow mode, no MIG bin-packing (full GPU only), no optimization pipeline, no drift detection, no scale-to-zero, no cost attribution, no multi-model composition. |
| **v2** | Traffic & scaling | Canary deployments with auto-rollback, blue-green deployments, custom metric autoscaling (queue depth, tokens/sec), MIG bin-packing, model pre-caching on NVMe, prediction logging (sampled), cost attribution (GPU-second ledger), per-team chargeback dashboard. | No A/B testing, no shadow mode, no optimization pipeline, no drift detection, no scale-to-zero, no predictive scaling, no multi-model composition. |
| **v3** | Optimization & intelligence | Model optimization pipeline (quantization, TensorRT compilation), A/B testing with consistent assignment, shadow mode, scale-to-zero, predictive autoscaling, prediction drift detection, spot instance support, right-sizing recommendations. | No multi-model composition (pipelines, ensembles), no automatic model selection. |
| **v4** | Composition & automation | Inference pipelines, ensemble serving, router model pattern, automatic optimization recommendations (the platform suggests "quantize this model to INT8, estimated 50% latency reduction with < 0.3% quality loss" based on model type and observed utilization). | Fully autonomous model promotion (a human always approves a new model version going to production — the platform provides data and recommendations, but never removes the human from the deployment decision loop). |

Each version is independently shippable. v1 alone, operated for months, solves the core problem (teams no longer run their own serving stacks). Each subsequent version adds efficiency (v2 saves GPU cost), quality (v3 catches drift), and capability (v4 enables complex inference patterns).

---

## 17. Exercises

1. **Design the model pre-caching system in full.** The NVMe cache on each GPU node has 2 TB capacity. There are 2,500 model versions totaling 10 TB, and a new model version is registered every 30 minutes. Design the eviction policy, the pre-pull priority queue, and the cache-miss path (how a pod handles "my model isn't on this node yet" without violating the cold-start budget). What happens when a popular 140GB LLM is updated — do all nodes pull the new version simultaneously, and what does that do to the storage network?

2. **GPU memory fragmentation under PagedAttention.** With 29,600 blocks in the KV cache pool and requests arriving/completing continuously, can the block pool fragment in a way that prevents new requests from starting even though total free memory is sufficient? (Hint: PagedAttention blocks are fixed-size and non-contiguous, so the answer is no — but prove it, and then consider what happens when `block_size` is set too large vs. too small.)

3. **Design the canary evaluation for an LLM endpoint specifically.** Unlike a classification model where error rate and latency are clear metrics, an LLM's "quality" is hard to measure in real-time. What signals can the canary controller use to detect a bad LLM model version? Consider: output token distribution shift, tool-call success rate, user satisfaction signals (thumbs-up/down), response length distribution, and downstream task success rate. Which of these can be evaluated in 5 minutes of canary traffic at 5%?

4. **Multi-GPU failure modes.** A 4-way tensor-parallel 70B model loses one of its 4 GPUs mid-inference (GPU hardware failure, NVLink error, or CUDA driver crash). What happens to in-flight requests? Can the remaining 3 GPUs continue serving at reduced capacity, or must the entire 4-GPU instance restart? Design the recovery path and compute the downtime.

5. **Design the cost-aware autoscaler.** Extend the autoscaler from section 7 with a cost budget constraint: "this endpoint's monthly GPU cost must not exceed $X." How does the autoscaler behave when scaling up would exceed the budget? Does it reject new requests, degrade quality (route to a cheaper model), or alert the team? What if the budget is hit on day 15 of the month?

6. **Cross-endpoint resource contention.** Two tier-1 endpoints on the same GPU node both trigger scale-up simultaneously. The node has 2 free GPU slots but each endpoint wants 3. Design the priority-based scheduling that decides which endpoint gets the scarce GPU, and how the losing endpoint recovers (cross-node scheduling, spot instance fallback, or graceful degradation).

7. **Design variant: 10 endpoints, 20 GPUs, 2 engineers.** A much smaller deployment. What components from this design collapse entirely, what stays because it's cheap insurance even at small scale, and at what scale does each dropped component need to come back? Justify each answer with a number (GPU count, QPS, team size), not a feeling.

8. **Benchmark the platform overhead.** Design a load test that isolates and measures the platform overhead (steps 2-8 and 10-12 from section 2's request path) independently from model inference time. The test must produce a P99 number that proves (or disproves) the 5ms P99 overhead SLO. What model backend do you use? (Hint: a model that returns instantly, so all measured latency is platform overhead.)

---
