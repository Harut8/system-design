# 14 — LLM Inference Observability — Special Topic

> LLM serving has its own metric vocabulary, its own bottlenecks, and its own SLO menu. Generic inference observability (doc 04) gets you 60% of the way; this doc covers the other 40% — the metrics that vLLM, TensorRT-LLM, and TGI emit that no other workload class does.

The reference is vLLM's metric surface (the most-instrumented LLM serving framework as of 2026). TGI and Triton+TensorRT-LLM expose subsets with similar semantics.

---

## 1. Why LLM Inference Is Different

Three things make LLM inference uniquely hard:

| Property | Implication |
|----------|-------------|
| **Two distinct phases** (prefill, decode) | Different SLOs apply to each |
| **State (KV cache)** that persists across tokens | Memory pressure, not compute, often dominates |
| **Variable output length** | Tail latency is workload-driven, not infra-driven |

A request takes the prefill phase (process input, compute KV cache), then the decode phase (generate one token at a time). Prefill is compute-bound (one big GEMM); decode is memory-bound (small GEMMs reading the entire KV cache each step). Optimization, observation, and SLOs differ.

---

## 2. The vLLM Metric Surface

vLLM exposes ~60 Prometheus metrics on `:8000/metrics`. The ones that matter for production:

### 2.1 Latency

| Metric | Type | What |
|--------|------|------|
| `vllm:time_to_first_token_seconds` | histogram | TTFT — time from request received to first output token |
| `vllm:inter_token_latency_seconds` | histogram | ITL — gap between consecutive output tokens |
| `vllm:e2e_request_latency_seconds` | histogram | Full request duration |
| `vllm:request_queue_time_seconds` | histogram | Time spent waiting before scheduling |
| `vllm:request_prefill_time_seconds` | histogram | Prefill phase duration |
| `vllm:request_decode_time_seconds` | histogram | Decode phase duration |
| `vllm:request_inference_time_seconds` | histogram | Total RUNNING phase duration |

### 2.2 Tokens / throughput

| Metric | Type | What |
|--------|------|------|
| `vllm:generation_tokens` | counter | Total output tokens generated |
| `vllm:prompt_tokens` | counter | Total input (prefill) tokens |
| `vllm:prompt_tokens_cached` | counter | Cached prompt tokens (didn't re-prefill) |
| `vllm:iteration_tokens_total` | histogram | Tokens per engine step (decode batch size, effectively) |

### 2.3 Cache (the heart of vLLM)

| Metric | Type | What |
|--------|------|------|
| `vllm:kv_cache_usage_perc` | gauge | Fraction of KV cache blocks in use |
| `vllm:prefix_cache_queries` | counter | Total cache lookups (per token) |
| `vllm:prefix_cache_hits` | counter | Total cache hits |
| `vllm:external_prefix_cache_queries` | counter | Cross-instance KV cache (KV connector) |
| `vllm:external_prefix_cache_hits` | counter | Cross-instance hits |
| `vllm:kv_block_lifetime_seconds` | histogram | How long blocks live before eviction |
| `vllm:kv_block_idle_before_evict_seconds` | histogram | How long evicted blocks were idle |
| `vllm:kv_block_reuse_gap_seconds` | histogram | Time between same block re-uses |

### 2.4 Scheduling & batching

| Metric | Type | What |
|--------|------|------|
| `vllm:num_requests_running` | gauge | Active requests in the current batch |
| `vllm:num_requests_waiting` | gauge | Queue depth |
| `vllm:num_requests_waiting_by_reason` | gauge | Labeled by `capacity` or `deferred` |
| `vllm:num_preemptions` | counter | KV evictions causing request preemption |

### 2.5 Speculative decoding

| Metric | Type | What |
|--------|------|------|
| `vllm:spec_decode_num_drafts` | counter | Draft inference invocations |
| `vllm:spec_decode_num_draft_tokens` | counter | Tokens proposed by draft model |
| `vllm:spec_decode_num_accepted_tokens` | counter | Tokens accepted by target |
| `vllm:spec_decode_num_accepted_tokens_per_pos` | counter | By position in draft |

### 2.6 LoRA

| Metric | Type | What |
|--------|------|------|
| `vllm:lora_requests_info` | gauge | Per-adapter active request stats |

### 2.7 MFU (Model FLOPs Utilization)

| Metric | Type | What |
|--------|------|------|
| `vllm:estimated_flops_per_gpu_total` | counter | Estimated FLOPs done |
| `vllm:estimated_read_bytes_per_gpu_total` | counter | HBM bytes read |
| `vllm:estimated_write_bytes_per_gpu_total` | counter | HBM bytes written |

---

## 3. The Six Dashboard Panels Every LLM Service Needs

### 3.1 TTFT and ITL percentiles

```promql
histogram_quantile(0.5, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))
histogram_quantile(0.95, ...)
histogram_quantile(0.99, ...)

histogram_quantile(0.5, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket[5m])))
histogram_quantile(0.95, ...)
histogram_quantile(0.99, ...)
```

These are the user-facing SLOs. For a chat assistant, TTFT < 200ms, ITL < 50ms is typical.

### 3.2 Token throughput

```promql
# Generation tokens per second
rate(vllm:generation_tokens[1m])

# Prompt (prefill) tokens per second
rate(vllm:prompt_tokens[1m])
```

The shape difference matters: generation rate is bounded by decode (small batches, memory-bound). Prompt rate is bounded by prefill (large batches, compute-bound). They scale differently with load.

### 3.3 KV cache pressure

```promql
# Current KV usage (gauge)
max(vllm:kv_cache_usage_perc)

# Preemption rate
rate(vllm:num_preemptions[5m])

# Block lifetime distribution — short = thrashing
histogram_quantile(0.5, sum by (le) (rate(vllm:kv_block_lifetime_seconds_bucket[5m])))
```

Healthy: KV usage 50–80%, preemption rate near zero, block lifetime > 30s. Pathological: usage > 95%, preemptions occurring, blocks living < 5s. The fix is bigger GPU memory or smaller `max_num_seqs`.

### 3.4 Prefix cache hit rate

```promql
# Hit rate
rate(vllm:prefix_cache_hits[5m]) / rate(vllm:prefix_cache_queries[5m])
```

For typical chatbot workloads with system prompts: 30–60% is good. < 10% means your cache isn't helping (try increasing `gpu_memory_utilization` or rethinking shared prompts).

### 3.5 Queue health

```promql
# Queue depth
vllm:num_requests_waiting

# Running requests
vllm:num_requests_running

# Reason breakdown
vllm:num_requests_waiting_by_reason
```

If `waiting_by_reason{reason="capacity"}` > 0 sustained → out of KV cache. If `waiting_by_reason{reason="deferred"}` → scheduler decision (not a problem).

### 3.6 Spec decode acceptance rate

```promql
# Acceptance rate per position
sum by (position) (rate(vllm:spec_decode_num_accepted_tokens_per_pos[5m]))
/
sum(rate(vllm:spec_decode_num_drafts[5m]))
```

Expected: ~70% at position 0, decaying. If first-position acceptance < 50%, your draft model is too divergent — re-train or pick a better draft.

---

## 4. The Prefill / Decode Split — Connecting to DCGM

The headline insight: prefill and decode use the GPU completely differently.

| Phase | Work | DCGM signature |
|-------|------|----------------|
| **Prefill** | One big matmul per layer, batch_size × seq_len wide | High `SM_active`, high `tensor_active`, low `dram_active` (compute-bound) |
| **Decode** | Tiny matmul per step but reads entire KV | Low `SM_active`, low `tensor_active`, high `dram_active` (memory-bound) |

Cross-correlation:

```promql
# Mean SM_active during prefill-heavy windows
DCGM_FI_PROF_SM_ACTIVE
  * on(pod, namespace) (rate(vllm:prompt_tokens[1m]) > rate(vllm:generation_tokens[1m]))

# Mean DRAM_active during decode-heavy windows
DCGM_FI_PROF_DRAM_ACTIVE
  * on(pod, namespace) (rate(vllm:generation_tokens[1m]) > 2 * rate(vllm:prompt_tokens[1m]))
```

Common diagnostic: SM_active is "low" (40%) and someone's worried — check the workload mix. If it's decode-heavy, 40% SM with 80% DRAM is *correct* for memory-bound decode. The fix isn't compute-side; it's memory throughput (move to H200 or bigger HBM).

---

## 5. GPU Memory Breakdown

vLLM doesn't directly expose model weights vs activations vs KV cache in HBM, but you can derive:

```promql
# Total HBM used by this pod's GPU
DCGM_FI_DEV_FB_USED
  * on(pod, namespace) group_left() kube_pod_labels{label_app="vllm"}

# KV cache bytes (vLLM derives from kv_cache_usage_perc × allocated KV memory)
vllm:kv_cache_usage_perc
  * on(pod) (vllm_kv_cache_total_bytes)  # if exposed
```

When `kv_cache_usage_perc` is high but `DCGM_FI_DEV_FB_USED` is also high (close to 80GB on H100), the GPU is saturated end-to-end. There's no headroom; reduce `max_num_seqs` or scale out.

### 5.1 Fragmentation

vLLM uses PagedAttention which is the answer to KV fragmentation. The bookkeeping metric:

```promql
# Block churn: created vs evicted
rate(vllm:kv_blocks_allocated[5m])
rate(vllm:kv_blocks_evicted[5m])
```

If eviction rate > allocation rate sustained, you have churn (preemption-driven). If allocation >> eviction, you're growing into the cache (good). If they're equal at high rates, healthy steady-state.

---

## 6. Tensor Parallelism Health

For models served across multiple GPUs (`--tensor-parallel-size 4`), all TP ranks should have identical SM_active, DRAM_active. Asymmetry is a problem.

```promql
# Variance in SM_active across TP ranks
stddev by (pod) (DCGM_FI_PROF_SM_ACTIVE)
  * on(pod, namespace) group_left() kube_pod_labels{label_app="vllm"}
> 0.05
```

If variance > 5%, one rank is lagging — likely NVLink or thermal issue on that GPU (drilldown via doc 06).

---

## 7. LoRA Adapter Loading

vLLM with LoRA serves N adapters in one batch. The metric:

```promql
# Active LoRA adapters
vllm:lora_requests_info
```

Adapter loading/unloading is overhead — the goal is high reuse:

```promql
# Compute the rate of adapter switches (custom; emitted via instrumentation)
rate(vllm:lora_adapter_loads_total[5m])
```

If the cluster cycles through adapters rapidly (one-shot serving), the loading overhead dominates and you should pin hot adapters. The metric tells you which ones.

---

## 8. Continuous Batching Efficiency

vLLM's killer feature is continuous batching: requests join the running batch mid-stream. The efficiency metric:

```promql
# Tokens per engine step — proxy for batch density
histogram_quantile(0.5, sum by (le) (rate(vllm:iteration_tokens_total_bucket[5m])))
```

Plot p50, p95 over time. A healthy server: p50 around 100–200 tokens per step (decode batch of 50 with 2-4 tokens each via spec decoding). Low values mean the batch is sparsely filled — under-loaded server.

---

## 9. SLOs in Composite

The "throughput at SLO" panel in doc 09 §5.2 specialized for LLM:

```promql
# Tokens/sec while p99 TTFT < 200ms and p99 ITL < 50ms
sum(rate(vllm:generation_tokens[5m]))
unless on()
(
  histogram_quantile(0.99, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m]))) > 0.2
  or
  histogram_quantile(0.99, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket[5m]))) > 0.05
)
```

Returns a number when both SLOs are met, blank otherwise. Plot over time → "what was our effective serving capacity yesterday."

---

## 10. Multimodal Cache

Vision-language models cache image embeddings:

```promql
# MM cache hit rate
rate(vllm:mm_cache_hits[5m]) / rate(vllm:mm_cache_queries[5m])
```

Highly workload-dependent: a chatbot rarely re-uses images (low hit rate, OK). A document-RAG system re-uses them constantly (high hit rate expected, problem if not).

---

## 11. NIXL — Cross-Instance KV Sharing

The newer KV-connector pattern (NIXL) shares KV blocks across vLLM instances. Observability:

```promql
# Cross-instance hit rate
rate(vllm:external_prefix_cache_hits[5m]) / rate(vllm:external_prefix_cache_queries[5m])

# Transfer time
histogram_quantile(0.95, sum by (le) (rate(vllm:nixl_xfer_time_seconds_bucket[5m])))

# Bytes transferred
rate(vllm:nixl_bytes_transferred_sum[5m])
```

If transfer time dominates the request latency budget, cross-instance KV sharing is hurting more than helping; tune the NIXL config.

---

## 12. Alerts Specific to LLM Serving

Beyond the generic inference alerts in doc 10:

```yaml
- alert: vLLM_KVCache_Saturated
  expr: max(vllm:kv_cache_usage_perc) > 0.95
  for: 5m
  labels: { severity: warning }
  annotations:
    summary: "KV cache > 95% — preemption imminent"

- alert: vLLM_Preemption_Rate_High
  expr: rate(vllm:num_preemptions[5m]) > 0.1   # >0.1 preemption/sec sustained
  for: 5m
  labels: { severity: warning }

- alert: vLLM_TTFT_p99
  expr: |
    histogram_quantile(0.99,
      sum by (service, le) (rate(vllm:time_to_first_token_seconds_bucket[5m]))
    ) > 0.5
  for: 5m
  labels: { severity: critical }

- alert: vLLM_QueueBacklog
  expr: vllm:num_requests_waiting > 50
  for: 5m
  labels: { severity: warning }

- alert: vLLM_PrefixCache_LowHitRate
  expr: |
    rate(vllm:prefix_cache_hits[1h])
    / rate(vllm:prefix_cache_queries[1h])
    < 0.05
  for: 1h
  labels: { severity: info, tier: p3 }
  annotations:
    summary: "Prefix cache barely helping — investigate prompt structure"

- alert: vLLM_TP_Imbalance
  expr: |
    stddev by (pod) (DCGM_FI_PROF_SM_ACTIVE
      * on(pod, namespace) group_left() kube_pod_labels{label_app="vllm"}
    ) > 0.05
  for: 10m
  labels: { severity: warning }
```

---

## 13. TGI / Triton-TRT-LLM

Two other LLM serving stacks worth covering:

### 13.1 TGI (Hugging Face Text Generation Inference)

Exposes a similar but smaller surface:

| Metric | Equivalent vLLM |
|--------|-----------------|
| `tgi_request_count` | (counter from Prom defaults) |
| `tgi_request_inference_duration_bucket` | `vllm:request_inference_time_seconds_bucket` |
| `tgi_batch_current_size` | `vllm:num_requests_running` |
| `tgi_queue_size` | `vllm:num_requests_waiting` |

### 13.2 Triton + TensorRT-LLM

Triton's metric surface (doc 04 §4.2) for batching, plus TensorRT-LLM-specific:

| Metric | Use |
|--------|-----|
| `nv_inference_batch_size_bucket` | Batch size distribution |
| `nv_inference_compute_inference_duration_us` | Compute time only |
| `nv_inference_queue_duration_us` | Time in queue |

For TensorRT-LLM, KV cache observability is exposed via TRT-LLM-specific gauges; the dashboard maps similarly to vLLM panels but the metric names differ.

---

## 14. Acceptance Checklist

- [ ] vLLM `:8000/metrics` is scraped from every replica
- [ ] L4 inference dashboard has TTFT, ITL, throughput, KV cache, queue panels
- [ ] Prefill vs decode separation visible in dashboards
- [ ] Spec decode acceptance rate plotted (if used)
- [ ] LoRA adapter usage tracked (if used)
- [ ] DCGM metrics joined to vLLM via `pod`/`namespace` for cross-correlation
- [ ] LLM-specific alerts (KV saturation, preemption, TTFT) defined
- [ ] Per-tenant LLM SLOs (TTFT, ITL) defined for customer-facing services

---

## 15. Forward Pointers

- **Doc 11**: profiling LLM kernels — when metrics aren't enough
- **Doc 13**: per-tenant LLM observability for shared serving infra

---

## Sources

- [vLLM Production Metrics (full reference)](https://docs.vllm.ai/en/stable/usage/metrics/)
- [vLLM v1 Metrics Design](https://docs.vllm.ai/en/v0.8.5/design/v1/metrics.html)
- [Inside vLLM (Sept 2025)](https://blog.vllm.ai/2025/09/05/anatomy-of-vllm.html)
- [vLLM — Speculative Decoding](https://docs.vllm.ai/en/latest/features/speculative_decoding/)
- [Glukhov — Monitor LLM Inference (2026)](https://www.glukhov.org/observability/monitoring-llm-inference-prometheus-grafana/)
- [Red Hat — Triage vLLM performance (March 2026)](https://developers.redhat.com/articles/2026/03/09/5-steps-triage-vllm-performance)
- [AWS — Speculative decoding on Trainium with vLLM](https://aws.amazon.com/blogs/machine-learning/accelerating-decode-heavy-llm-inference-with-speculative-decoding-on-aws-trainium-and-vllm/)
