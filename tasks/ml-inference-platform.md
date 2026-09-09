## System Design Task: ML Model Serving & Inference Platform

### Problem Statement

Design a **managed ML model serving platform** that enables teams to deploy
trained models as low-latency, high-throughput prediction endpoints — covering
everything from a 100MB gradient-boosted tree serving 50K QPS to a 70B parameter
LLM streaming tokens to 10K concurrent users.

Today, each ML team runs its own serving stack: one team wraps a scikit-learn
model in Flask, another deploys a PyTorch model on a custom Triton setup, a
third team calls a vLLM instance they provisioned ad-hoc. There's no unified
deployment pipeline, no shared autoscaling, no consistent A/B testing
infrastructure, no cost attribution, and no standard way to roll back a bad
model. When the team that deployed the fraud-detection model went on vacation,
nobody knew how to scale it during Black Friday traffic, and it fell over.

The platform fixes this by providing a **single deployment target** for any ML
model: users register a model artifact, define a serving configuration (hardware,
scaling policy, traffic routing), and the platform handles **containerization,
resource allocation, autoscaling, health monitoring, traffic splitting, rollback,
and observability** — for both traditional ML models and large generative AI
models.

This is shared infrastructure serving 100+ teams, on the critical path of
customer-facing products. Its own reliability, latency overhead, and
cost-efficiency are first-class requirements.

---

### Functional Requirements

1. **Model Registry and Artifact Management**

   * Register model artifacts (weights, config, tokenizer, preprocessing
     code) with versioning — every deployment is traceable to an exact model
     version.
   * Support model formats: **ONNX, TorchScript, SavedModel (TF), XGBoost/
     LightGBM native, Triton ensemble, vLLM-compatible (HuggingFace format),
     and custom container images** for models with complex preprocessing.
   * Metadata per version: training job ID, dataset version, offline metrics
     (accuracy, latency profile), model size, hardware requirements, and
     dependencies.
   * Model lineage: link each model version to the training run, dataset, and
     experiment that produced it.

2. **Deployment and Serving**

   * **One-command deployment**: given a registered model version and a serving
     config (instance type, replica count, autoscaling policy), deploy to a
     live endpoint with a stable URL.
   * **Serving runtimes**:
     * **Traditional ML**: ONNX Runtime, XGBoost/LightGBM native inference,
       scikit-learn — CPU-based, high throughput, sub-10ms latency.
     * **Deep learning**: PyTorch (TorchServe), TensorFlow Serving, Triton
       Inference Server — GPU-based, batched inference.
     * **Generative AI / LLM**: vLLM, TensorRT-LLM, or SGLang — with
       continuous batching, PagedAttention, KV cache management, speculative
       decoding, tensor parallelism across multi-GPU.
   * **Dynamic batching**: accumulate individual requests into batches up to
     a configured max batch size or max wait time, to maximize GPU
     utilization without exceeding latency SLOs.
   * **Request/response schema enforcement**: validate input against a
     declared schema before model inference; return structured errors for
     malformed requests.
   * **Streaming inference**: for generative models, stream tokens via SSE
     as they're produced, with backpressure handling for slow clients.

3. **Autoscaling**

   * **Horizontal autoscaling**: scale replica count based on:
     * QPS (requests per second).
     * GPU utilization / compute saturation.
     * Queue depth (pending requests waiting for inference).
     * Custom metrics (tokens/sec for LLMs, batch queue latency).
   * **Scale-to-zero**: for infrequently used models, scale down to zero
     replicas and cold-start on the first request (with a configured cold-
     start latency budget).
   * **Predictive scaling**: use historical traffic patterns (time-of-day,
     day-of-week) to pre-scale before expected traffic spikes, avoiding
     cold-start latency during peak.
   * **GPU-aware scheduling**: bin-pack smaller models onto shared GPUs (MPS /
     MIG on A100/H100) to avoid wasting GPU memory; dedicate full GPUs/nodes
     to large models.
   * **Scaling speed**: from scale decision to new replica serving traffic:
     ≤ 60 seconds for warm instances (pre-pulled image), ≤ 5 minutes for
     cold start (image pull + model load).

4. **Traffic Management and Rollout**

   * **Canary deployments**: route a configurable percentage of traffic (1%,
     5%, 10%...) to a new model version while the rest goes to the current
     version, with automatic rollback if error rate or latency exceeds
     thresholds.
   * **Blue-green deployments**: spin up the new version alongside the old,
     switch traffic atomically once health checks pass.
   * **A/B testing**: split traffic between model versions with consistent
     user assignment (same user always hits the same version for the
     experiment duration), with metrics collection and statistical analysis.
   * **Shadow mode**: send a copy of production traffic to a new model version
     without serving its responses to users — for offline comparison of
     predictions and latency.
   * **Instant rollback**: revert to the previous model version within seconds
     if the new version misbehaves — without waiting for a full redeployment.

5. **Multi-Model Composition**

   * **Inference pipelines**: chain models in sequence (e.g., tokenizer →
     embedding model → classifier → post-processor) as a single endpoint
     that the caller treats as one model.
   * **Ensemble serving**: run multiple models in parallel and aggregate their
     outputs (voting, averaging, stacking).
   * **Routing models**: a lightweight model that routes the request to one
     of several specialized models based on input characteristics (e.g.,
     language detection → language-specific model).

6. **Optimization**

   * **Model optimization pipeline**: before deployment, optionally apply:
     * Quantization (FP16, INT8, INT4 — with calibration data).
     * Graph optimization (operator fusion, constant folding).
     * Compilation (TorchCompile, TensorRT, ONNX optimization).
   * **KV cache management** (LLMs): PagedAttention for efficient memory
     allocation, prefix caching for shared system prompts, KV cache offloading
     to CPU/NVMe for long contexts.
   * **Speculative decoding** (LLMs): use a smaller draft model to generate
     candidate tokens verified by the main model, increasing throughput.
   * **Continuous batching** (LLMs): add new requests to an in-flight batch
     as earlier requests complete, rather than waiting for the entire batch
     to finish.

7. **Observability**

   * **Per-endpoint metrics**: QPS, latency histograms (P50/P95/P99), error
     rate, GPU utilization, memory usage, batch size distribution, queue depth.
   * **Per-request tracing**: distributed trace from API gateway through
     preprocessing, inference, and postprocessing, with model version and
     replica ID.
   * **Model quality monitoring**: track prediction distribution drift
     (input feature drift, output distribution shift) to detect model
     staleness or data pipeline issues.
   * **Cost attribution**: per-endpoint GPU-hour and dollar cost, broken down
     by team/project.
   * **Alerting**: SLO-based alerts (latency P99 > threshold, error rate >
     threshold, GPU OOM events, model quality drift).

8. **Cost Management**

   * **Instance right-sizing recommendations**: based on observed utilization,
     recommend smaller/larger instance types or autoscaling policy changes.
   * **Spot/preemptible instance support**: for latency-tolerant workloads
     (batch inference, shadow mode), run on cheaper spot instances with
     automatic failover to on-demand.
   * **Multi-tenancy and bin-packing**: co-locate small models on shared GPUs
     to maximize utilization — a 2GB model shouldn't reserve an 80GB A100.
   * **Chargeback**: every GPU-second attributed to the team that owns the
     endpoint.

---

### Non-Functional Requirements

1. **Scale**

   * **500+ deployed model endpoints** across 100+ teams.
   * **Aggregate serving QPS**: 2,000,000 requests/sec across all endpoints.
   * **Largest single endpoint**: 50,000 QPS (traditional ML) or 10,000
     concurrent streaming sessions (LLM).
   * **Model sizes**: from 100 MB (XGBoost) to 140 GB (70B parameter LLM
     across 2-4 GPUs with tensor parallelism).
   * **GPU fleet**: 2,000+ GPUs managed by the platform (mix of A100, H100).

2. **Latency**

   * **Platform overhead** (routing, load balancing, schema validation,
     telemetry — excluding model inference itself): P99 ≤ **5 ms**.
   * **Traditional ML endpoint** (XGBoost, small neural net): P50 ≤ **5 ms**,
     P99 ≤ **20 ms** end-to-end.
   * **Deep learning endpoint** (ResNet, BERT): P50 ≤ **20 ms**, P99 ≤
     **100 ms**.
   * **LLM time-to-first-token**: P50 ≤ **200 ms**, P99 ≤ **500 ms**.
   * **LLM inter-token latency**: P50 ≤ **30 ms**, P99 ≤ **80 ms**.

3. **Availability**

   * Platform control plane (deployment, scaling): **99.95%**.
   * Model serving data plane: **99.99%** for tier-1 endpoints (revenue-
     critical), **99.9%** for tier-2/3.
   * A single model deployment/rollback must not affect other endpoints
     (blast radius isolation).

4. **Correctness**

   * **Bit-exact reproducibility**: the same input to the same model version
     must produce the same output (within floating-point determinism bounds
     for GPU inference).
   * **No stale models**: after a rollback or redeployment, no replica should
     serve the old model version for more than 30 seconds.

---

### Constraints and Assumptions

* The platform runs on Kubernetes with GPU operator and device plugin; you are
  designing the ML serving layer, not the cluster itself.
* Models are trained elsewhere (your Parallel ML Training Platform or external
  systems) and registered as artifacts; the serving platform is not responsible
  for training.
* Callers are internal microservices and user-facing APIs; there is no direct
  public internet exposure (an API gateway sits in front).
* Assume both on-premise GPU clusters and cloud GPU instances are available,
  with the platform abstracting the difference.
* Not in scope: feature computation / feature stores (callers provide fully
  formed input features). However, the platform must integrate cleanly with
  an external feature store.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: control plane vs. data plane, the deployment flow,
   and the inference request path.
3. Model registry and artifact management design.
4. Serving runtime architecture: how different model types (traditional ML,
   deep learning, LLM) are served with appropriate runtimes.
5. Dynamic batching design: how requests are accumulated, batched, and
   dispatched with latency guarantees.
6. LLM-specific serving: continuous batching, PagedAttention, KV cache
   management, speculative decoding, tensor parallelism.
7. Autoscaling design: signals, algorithms, GPU-aware scheduling, and
   scale-to-zero.
8. Traffic management: canary, blue-green, A/B, shadow mode, and rollback
   mechanisms.
9. Multi-model composition: inference pipelines, ensembles, and routing.
10. Model optimization pipeline: quantization, compilation, and their
    impact on latency/quality trade-offs.
11. Capacity estimates with arithmetic: GPU memory budgets per model type,
    fleet sizing for 2M QPS, storage for 500 model versions.
12. Failure walkthroughs: GPU OOM during inference, model returning garbage
    predictions, autoscaler thrashing, and a bad model deployment cascading
    to dependent services.
13. Cost model: GPU utilization efficiency, bin-packing savings, spot instance
    savings, and per-endpoint chargeback.
14. Trade-offs: dynamic batching latency vs. throughput, quantization quality
    vs. speed, scale-to-zero cold-start vs. cost, model-level vs.
    request-level isolation.

---

### Expectations

* **Do the arithmetic.** GPU memory breakdown for serving a 70B model (weights
  in FP16, KV cache per concurrent request, activation memory), throughput
  per GPU, fleet size for target QPS.
* **Name concrete mechanisms** — vLLM continuous batching, PagedAttention,
  TensorRT-LLM, NVIDIA MIG, Kubernetes HPA + custom metrics, Istio traffic
  splitting — and say what each buys.
* **Show the request path.** From HTTP request arrival to model prediction
  returned, every hop with its latency contribution.
* **LLM serving is the hard part.** Don't hand-wave "we use vLLM" — show the
  memory math, batching strategy, KV cache budget, and how you handle 10K
  concurrent streams on a finite GPU fleet.
* Prefer a design a platform team of 5-8 engineers can operate over one that
  requires deep GPU kernel expertise to debug.

---
