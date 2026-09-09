# Parallel ML Training Platform: Design Document

> Solution to [`tasks/parallel-ml-training.md`](../tasks/parallel-ml-training.md).

---

## Table of Contents

1. [Requirements Clarification](#1-requirements-clarification)
2. [Architecture Overview](#2-architecture-overview)
3. [Parallelism Strategy Engine](#3-parallelism-strategy-engine)
4. [Cluster Scheduler Design](#4-cluster-scheduler-design)
5. [Fault Tolerance Design](#5-fault-tolerance-design)
6. [Data Pipeline Architecture](#6-data-pipeline-architecture)
7. [Experiment Tracking and Observability](#7-experiment-tracking-and-observability)
8. [Network Architecture](#8-network-architecture)
9. [Capacity Estimates](#9-capacity-estimates)
10. [Failure Walkthroughs](#10-failure-walkthroughs)
11. [Cost Model](#11-cost-model)
12. [Evolution Path](#12-evolution-path)
13. [Trade-offs](#13-trade-offs)

---

## 1. Requirements Clarification

### Questions & Answers

| Category | Question | Answer |
|---|---|---|
| Scope | Does the platform manage bare-metal GPU provisioning, or does it sit above Kubernetes? | Above Kubernetes. We assume a K8s cluster with NVIDIA GPU Operator, Network Operator (for RDMA/InfiniBand), and a CSI driver for shared storage already exists. We build the ML-specific orchestration layer on top. |
| Scope | Does the platform support inference or serving? | No. Responsibility ends at a trained checkpoint saved to object storage and optionally registered in a model registry. |
| Scope | Does the platform run hyperparameter search? | No. The platform runs individual training jobs. An external HPO orchestrator (Optuna, Ray Tune) calls the platform's job submission API to launch independent trials as separate jobs. |
| Hardware | What GPU types are in the fleet? | Mix of NVIDIA A100 80GB (older nodes), H100 80GB (primary), and H200 141GB (newest nodes). All nodes within a single job are homogeneous GPU type. |
| Hardware | What is the intra-node interconnect? | NVLink 4.0 on H100 nodes (900 GB/s bidirectional per GPU pair via NVSwitch), NVLink 3.0 on A100 nodes (600 GB/s). |
| Hardware | What is the inter-node interconnect? | 400 Gbps InfiniBand NDR on H100/H200 racks, 200 Gbps HDR on A100 racks. Cross-rack connectivity via fat-tree InfiniBand fabric with 2:1 oversubscription at the spine. |
| Hardware | Node configuration? | 8 GPUs per node (DGX-style). 2 TB system RAM, 2x Intel Xeon (128 cores total), 8x 3.84 TB NVMe SSDs for local scratch. |
| Storage | What is the shared storage layer? | S3-compatible object storage (AWS S3 or MinIO on-prem) for datasets, checkpoints, and artifacts. Optional high-performance NFS (Lustre/GPFS) for working scratch where streaming from S3 is insufficient. |
| Framework | Primary framework? | PyTorch with `torch.distributed` and `torchrun`. JAX/XLA as secondary. TensorFlow is legacy-only, supported via container images but no platform-native integration. |
| Tenancy | How are teams isolated? | Kubernetes namespaces per team, with ResourceQuotas for GPU allocation. Network policies isolate NCCL traffic between jobs. Separate service accounts for data access. |
| Consistency | Must experiment metrics be exactly-once? | No. Metrics are best-effort streaming (a dropped metric point during recovery is acceptable). Checkpoint metadata is exactly-once (a checkpoint that exists must be registered; a missing registration means the checkpoint is treated as nonexistent). |
| Availability | What happens if the control plane is down? | Running training jobs continue unaffected (they are self-contained pods). No new jobs can be submitted, and no new failures can be recovered (the recovery controller is control-plane). Control plane targets 99.95% availability. |

### Key Assumptions

1. **Kubernetes is the substrate, not a suggestion.** Gang scheduling uses Volcano or a custom scheduler extender. GPU topology is exposed via NVIDIA's Device Plugin and Topology Manager.
2. **NCCL is the communication backend for PyTorch.** The platform does not replace NCCL -- it configures and monitors it. For JAX, XLA's built-in collective communication (also backed by NCCL on NVIDIA GPUs) is used.
3. **Checkpoints are the single source of truth for recovery.** No attempt is made to reconstruct training state from anything other than a checkpoint. The corollary: checkpoint frequency directly determines maximum lost compute on failure.
4. **GPU failures are common, not exceptional.** At 10,000 GPU scale, with an observed GPU MTBF of ~2,000 hours, the cluster sees roughly 5 GPU failures per hour. The platform is designed around this being a steady-state event.
5. **Network bandwidth between racks is the bottleneck**, not intra-node. TP must stay within a node; PP should stay within a rack; DP can span racks because all-reduce traffic is lower per-GPU than TP's all-gather.
6. **Mixed-precision training (bf16 forward, fp32 master weights)** is the default regime. The memory arithmetic throughout this document assumes this.

### What We Are Explicitly Not Building (v1)

- Not an AutoML / neural architecture search system.
- Not a data labeling or data quality platform.
- Not a model serving / inference platform.
- Not a notebook environment (users develop locally or in their own Jupyter; the platform takes a Git repo and a config).
- Not a custom distributed training framework -- we orchestrate PyTorch/JAX's own primitives, not replace them.

---

## 2. Architecture Overview

### Component Map

```
                    ┌─────────────────────────────────────────────────┐
                    │                 Control Plane                      │
                    │                                                    │
                    │  ┌──────────────┐  ┌──────────────────────────┐  │
                    │  │  API Server    │  │  Parallelism Advisor      │  │
                    │  │  (job submit,  │  │  (model graph analysis,   │  │
                    │  │   dry-run,     │  │   memory estimation,      │  │
                    │  │   status)      │  │   topology-aware plan)    │  │
                    │  └──────┬───────┘  └────────────┬─────────────┘  │
                    │         │                        │                 │
                    │  ┌──────▼───────────────────────▼──────────────┐  │
                    │  │            Job Controller                      │  │
                    │  │  (lifecycle management, state machine,         │  │
                    │  │   scales jobs, triggers recovery)              │  │
                    │  └──────┬────────────────────────┬──────────────┘  │
                    │         │                        │                 │
                    │  ┌──────▼───────┐  ┌────────────▼─────────────┐  │
                    │  │  Scheduler     │  │  Recovery Controller       │  │
                    │  │  (gang sched,  │  │  (failure detection,       │  │
                    │  │   topology,    │  │   node replacement,        │  │
                    │  │   fair-share,  │  │   checkpoint restore)      │  │
                    │  │   preemption)  │  │                            │  │
                    │  └──────┬───────┘  └────────────┬─────────────┘  │
                    │         │                        │                 │
                    │  ┌──────▼───────┐  ┌────────────▼─────────────┐  │
                    │  │  Checkpoint    │  │  Experiment Tracker        │  │
                    │  │  Manager       │  │  (metrics ingestion,       │  │
                    │  │  (lifecycle,   │  │   run metadata,            │  │
                    │  │   retention,   │  │   model registry)          │  │
                    │  │   dedup)       │  │                            │  │
                    │  └──────────────┘  └──────────────────────────┘  │
                    └─────────┬──────────────────────┬──────────────────┘
                              │                      │
              ┌───────────────┼──────────────────────┼──────────────────┐
              │               │     Data Plane        │                  │
              │               ▼                       ▼                  │
              │  ┌──────────────────────────────────────────────────┐   │
              │  │              Kubernetes Cluster                     │   │
              │  │                                                     │   │
              │  │  ┌──────────────────────────────────────────────┐  │   │
              │  │  │  Training Job Pod Group (gang-scheduled)       │  │   │
              │  │  │                                                 │  │   │
              │  │  │  ┌─────────┐ ┌─────────┐      ┌─────────┐   │  │   │
              │  │  │  │ Worker 0 │ │ Worker 1 │ ...  │Worker N-1│   │  │   │
              │  │  │  │ (rank 0) │ │ (rank 1) │      │(rank N-1)│   │  │   │
              │  │  │  │ 8 GPUs   │ │ 8 GPUs   │      │ 8 GPUs   │   │  │   │
              │  │  │  └────┬────┘ └────┬────┘      └────┬────┘   │  │   │
              │  │  │       │           │                 │         │  │   │
              │  │  │       └───────────┼─────────────────┘         │  │   │
              │  │  │                   │  NCCL over InfiniBand      │  │   │
              │  │  └──────────────────────────────────────────────┘  │   │
              │  │                                                     │   │
              │  │  ┌──────────────┐  ┌──────────────┐                │   │
              │  │  │ Node Agent     │  │ GPU Health     │                │   │
              │  │  │ (heartbeat,    │  │ Monitor        │                │   │
              │  │  │  local ckpt,   │  │ (Xid errors,   │                │   │
              │  │  │  NVMe staging) │  │  ECC, thermal) │                │   │
              │  │  └──────────────┘  └──────────────┘                │   │
              │  └─────────────────────────────────────────────────────┘   │
              │                                                            │
              │  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐   │
              │  │ Object Store   │  │ Lustre/GPFS   │  │ Metrics Store  │   │
              │  │ (S3/MinIO)     │  │ (optional)     │  │ (Prometheus +  │   │
              │  │ checkpoints,   │  │ fast scratch   │  │  VictoriaM.)   │   │
              │  │ datasets       │  │                │  │                │   │
              │  └──────────────┘  └──────────────┘  └───────────────┘   │
              └────────────────────────────────────────────────────────────┘
```

### Control Plane vs. Data Plane

| Plane | Components | Characteristics |
|---|---|---|
| **Control plane** | API Server, Job Controller, Scheduler, Recovery Controller, Checkpoint Manager, Experiment Tracker, Parallelism Advisor | Stateful (backed by etcd/PostgreSQL), can tolerate seconds of latency, horizontally scaled for availability (3+ replicas), not on the training hot path |
| **Data plane** | Worker pods (training code + NCCL), Node Agents (heartbeat + local checkpoint staging), GPU Health Monitors, data loading sidecars | Performance-critical, latency-sensitive, directly handles GPU compute and inter-GPU communication, must operate independently of control plane for short durations |

**Why this split matters**: A control-plane outage (API server down) must never stop an actively training job. Workers communicate with each other via NCCL directly -- the control plane is only consulted at lifecycle boundaries (start, checkpoint, scale, recover, stop). The Node Agent provides a local heartbeat even when control-plane connectivity is lost, enabling workers to detect peer failures without a centralized coordinator.

### Job Lifecycle State Machine

```
                         ┌──────────┐
            submit ────▶ │ PENDING   │  (validated, queued for scheduling)
                         └─────┬────┘
                               │  scheduler finds placement
                               ▼
                         ┌──────────┐
                         │SCHEDULING│  (gang: waiting for all pods to be placed)
                         └─────┬────┘
                               │  all pods placed + images pulled
                               ▼
                         ┌──────────┐
                         │ STARTING  │  (NCCL rendezvous, data loader warmup)
                         └─────┬────┘
                               │  first training step completes
                               ▼
                ┌──────── ┌──────────┐ ────────────┐
  elastic       │         │ RUNNING   │              │  user/system stop
  scale event   │         └─────┬────┘              │
                │               │                    ▼
                │               │  failure      ┌──────────┐
                │               ├──────────────▶│RECOVERING│
                │               │               └─────┬────┘
                │               │                     │  restored from checkpoint
                │               │                     │  + replacement node joined
                │               │◀────────────────────┘
                │               │
                │               │  loss converged / max steps reached
                ▼               ▼
          ┌──────────┐   ┌──────────┐
          │ SCALING   │   │COMPLETED │  (final checkpoint saved + registered)
          └─────┬────┘   └──────────┘
                │  new topology stable
                └──▶ RUNNING
```

### Declarative Job Spec

```yaml
apiVersion: training.platform/v1
kind: TrainingJob
metadata:
  name: llama-7b-finetune-v3
  team: nlp-research
  project: instruction-tuning
  priority: high              # critical | high | normal | low | preemptible
spec:
  # --- Model source ---
  source:
    git:
      repo: https://github.com/acme/llm-training.git
      branch: main
      commit: a1b2c3d4
    entrypoint: train.py
    args: ["--config", "configs/llama_7b_sft.yaml"]

  # --- Framework ---
  framework: pytorch           # pytorch | jax | tensorflow
  frameworkVersion: "2.4"
  image: registry.acme.com/training/pytorch:2.4-cuda12.4-nccl2.21

  # --- Hardware ---
  hardware:
    gpuType: H100               # H100 | H200 | A100_80GB
    gpuCount: 64                # total GPUs requested
    minGpuCount: 32             # minimum for elastic scaling
    gpuMemoryMin: 80GB
    nodesPreferred: 8           # hint: 64 GPUs / 8 per node

  # --- Parallelism ---
  parallelism:
    strategy: auto              # auto | manual
    # If manual, specify:
    # dataParallel: 8
    # tensorParallel: 4
    # pipelineParallel: 2
    # microBatchSize: 4
    hints:
      modelParameters: 7e9
      hiddenSize: 4096
      numLayers: 32
      numAttentionHeads: 32
      sequenceLength: 4096
      globalBatchSize: 256
      mixedPrecision: bf16

  # --- Dataset ---
  dataset:
    source: s3://datasets/instruction-tuning/v2.3/
    format: jsonl
    size: 50GB
    streaming: true

  # --- Checkpointing ---
  checkpoint:
    intervalSteps: 500
    intervalMinutes: 30         # whichever comes first
    maxKeep: 5
    keepBestMetric: eval_loss
    keepBestCount: 3
    storage: s3://checkpoints/llama-7b-finetune-v3/
    async: true
    compression: lz4

  # --- Training ---
  training:
    maxSteps: 50000
    maxRuntime: 48h
    evalIntervalSteps: 1000
    seed: 42
    env:
      NCCL_DEBUG: WARN
      CUDA_DEVICE_MAX_CONNECTIONS: "1"

  # --- Fault tolerance ---
  faultTolerance:
    maxRestarts: 10
    spotInstanceOk: false
    inMemoryCheckpointReplicas: 2
    redundantCompute: false
```

The platform injects the following environment variables into every worker pod at launch -- the user's training script reads these to initialize `torch.distributed`:

```bash
# Injected by platform, never set by user
MASTER_ADDR=10.0.1.100          # rank-0 node IP
MASTER_PORT=29500
WORLD_SIZE=64                   # total GPU count
RANK=<per-pod>                  # global rank [0, WORLD_SIZE)
LOCAL_RANK=<per-pod>            # GPU index within the node [0, 7]
NCCL_SOCKET_IFNAME=ib0          # InfiniBand interface
NCCL_IB_HCA=mlx5                # RDMA HCA device
NCCL_ALGO=Ring                  # or Tree, chosen by advisor
NCCL_NET_GDR_LEVEL=5            # GPUDirect RDMA level
```

---

## 3. Parallelism Strategy Engine

### Parallelism Dimensions

| Dimension | What is Sharded | Communication Pattern | Where to Place | Bandwidth Requirement |
|---|---|---|---|---|
| **Data Parallel (DP/DDP)** | Data batches across replicas; each replica holds full model | AllReduce on gradients after each step | Across racks (tolerates lower bandwidth) | `2 * model_size_bytes / step_time` for ring all-reduce |
| **Fully Sharded DP (FSDP/ZeRO-3)** | Parameters, gradients, optimizer states across DP group | AllGather params before forward; ReduceScatter gradients after backward | Across racks, but benefits from higher bandwidth | `3 * model_size_bytes / step_time` (more communication than DDP) |
| **Tensor Parallel (TP)** | Individual layers (attention heads, FFN columns) across GPUs | AllReduce or AllGather at every layer boundary | Within a node (NVLink mandatory) | Very high: `2 * hidden_size * seq_len * batch / step_time` per layer |
| **Pipeline Parallel (PP)** | Model layers split into stages, micro-batches pipelined | Point-to-point send/recv of activations between stages | Within a rack (InfiniBand) | `hidden_size * seq_len * micro_batch / step_time` per stage boundary |
| **Expert Parallel (EP)** | MoE expert replicas across nodes | All-to-All for token routing | Across racks (traffic scales with expert count) | Depends on top-k routing and token distribution |

### GPU Memory Budget Arithmetic

For a transformer model with `P` parameters trained in mixed precision (bf16 forward/backward, fp32 master weights + optimizer):

```
Per-parameter memory cost:
  - bf16 parameters:      2 bytes
  - bf16 gradients:       2 bytes
  - fp32 master weights:  4 bytes  (for Adam/AdamW)
  - fp32 optimizer state: 8 bytes  (Adam: first moment + second moment, 4 each)
  ────────────────────────────────
  Total per parameter:    16 bytes

Activation memory per layer (approximate, for transformer):
  = seq_len * batch_size * hidden_size * (10 + 24/tp_degree) bytes (bf16)
  (The 10 accounts for attention QKV projections, softmax outputs, dropout
   masks; 24/tp accounts for the FFN intermediate, divided by TP degree
   since activations are partitioned.)

With activation checkpointing (recompute activations during backward):
  Activation memory per layer drops to ~2 * seq_len * batch_size * hidden_size bytes
  (only store input activations at checkpoint boundaries, recompute the rest)
```

### Example 1: 7B Parameter Model on 8x H100 GPUs (1 Node)

```
Model: LLaMA-7B (32 layers, hidden=4096, heads=32, FFN=11008)
Parameters: 6.7 * 10^9

=== No sharding (pure DDP, single replica) ===

Parameter memory (bf16):          6.7B * 2 = 13.4 GB
Gradient memory (bf16):           6.7B * 2 = 13.4 GB
Optimizer state (fp32, Adam):     6.7B * 12 = 80.4 GB  (master + m1 + m2)
──────────────────────────────────────────────────
Total model state:                107.2 GB

This does NOT fit on a single H100 80 GB GPU.

=== FSDP (ZeRO Stage 3) across 8 GPUs on one node ===

Per-GPU model state: 107.2 GB / 8 = 13.4 GB
Activation memory per layer (seq=4096, micro_batch=2, with act checkpointing):
  = 2 * 4096 * 2 * 4096 * 2 bytes = 128 MB per layer
  x 32 layers = 4.1 GB total activations

Per-GPU total:  13.4 + 4.1 = 17.5 GB
Available: 80 GB (H100)
Headroom: 62.5 GB  (plenty for larger batch, or sequence length)

Strategy: FSDP across 8 GPUs on 1 node. No TP or PP needed.
DP degree = 1 (single node). Effective batch size = micro_batch * 8 = 16.
For global_batch_size = 256: use gradient accumulation of 256/16 = 16 steps.
```

### Example 2: 200B Parameter Model on 256x H100 GPUs (32 Nodes)

```
Model: 200B (96 layers, hidden=12288, heads=96, FFN=49152)
Parameters: 200 * 10^9

=== Model state ===

Parameter memory (bf16):          200B * 2 = 400 GB
Gradient memory (bf16):           200B * 2 = 400 GB
Optimizer state (fp32, Adam):     200B * 12 = 2,400 GB
──────────────────────────────────────────────────
Total model state:                3,200 GB = 3.2 TB

=== Why FSDP alone is insufficient ===

FSDP across 256 GPUs: 3,200 GB / 256 = 12.5 GB per GPU (fine for state)
BUT: FSDP requires AllGather of the FULL layer parameters before each
forward pass. A single layer's parameters:
  - Attention: 4 * hidden^2 * 2 bytes = 4 * 12288^2 * 2 = 1.15 GB
  - FFN: 3 * hidden * ffn * 2 bytes = 3 * 12288 * 49152 * 2 = 3.46 GB
  - Total per layer: ~4.6 GB

AllGather of 4.6 GB across 256 GPUs over InfiniBand (400 Gbps = 50 GB/s):
  Time = 4.6 GB / 50 GB/s = 92 ms PER LAYER BOUNDARY
  x 96 layers x 2 (forward + backward) = 17.7 seconds of communication
  per training step. This dominates training time. Unacceptable.

=== Hybrid Parallelism: TP=8, PP=4, DP=8 ===

Tensor Parallel (TP=8): within each node (NVLink, 900 GB/s bidirectional)
  - Each GPU holds 1/8th of each layer
  - AllReduce per layer: 2 * hidden * seq_len * batch * 2 bytes / 8
    = 2 * 12288 * 4096 * 4 * 2 / 8 = 96 MB
  - Over NVLink at 900 GB/s: 96 MB / 900 GB/s = 0.1 ms (negligible)

Pipeline Parallel (PP=4): across 4 nodes within a rack (InfiniBand)
  - 96 layers / 4 stages = 24 layers per stage
  - Activation transfer per micro-batch between stages:
    = seq_len * micro_batch * hidden * 2 bytes
    = 4096 * 4 * 12288 * 2 = 384 MB
  - Over InfiniBand at 50 GB/s: 384 MB / 50 GB/s = 7.7 ms per stage boundary

Data Parallel (DP=8): across 8 rack-groups
  - 256 GPUs / (8 TP * 4 PP) = 8 DP replicas
  - AllReduce of gradients: model_state_per_DP_group = 3,200 GB / 8 = 400 GB
    Wait -- that is the FSDP version. With TP=8 and PP=4, each DP replica
    holds a TP-sharded slice of PP-assigned layers:
    Per-DP-replica params = 200B / (8 * 4) = 6.25B params
    Gradient size = 6.25B * 2 bytes = 12.5 GB
    AllReduce (ring) across 8 replicas: 2 * 12.5 * (8-1)/8 = 21.9 GB
    Over InfiniBand (cross-rack, effective ~25 GB/s with oversubscription):
    = 21.9 GB / 25 GB/s = 0.88 seconds
    This overlaps with backward computation (gradient-as-ready AllReduce).

Total GPUs: 8 (TP) * 4 (PP) * 8 (DP) = 256. Checks out.

Per-GPU memory:
  Parameters: 200B / (8 * 4) * 2 bytes = 6.25 GB (bf16)
  Gradients: 6.25 GB
  Optimizer: 200B / (8 * 4) * 12 bytes = 37.5 GB
  Activations (24 layers, with checkpointing, TP=8):
    = 24 * 2 * 4096 * 4 * 12288 * 2 / 8 = 1.15 GB
  ────────────────────────────────────────────────
  Total per GPU: ~51.4 GB (out of 80 GB, 64% utilization)

Pipeline bubble overhead (1F1B schedule with m micro-batches):
  bubble_ratio = (pp_stages - 1) / m
  With PP=4 stages and m=32 micro-batches: bubble = 3/32 = 9.4%
  (within the 15% target)
```

### Auto-Parallelism Advisor

```python
from dataclasses import dataclass

@dataclass
class ModelProfile:
    params_billion: float
    hidden_size: int
    num_layers: int
    num_heads: int
    ffn_size: int
    sequence_length: int
    global_batch_size: int
    precision: str  # "bf16" | "fp16" | "fp32"

@dataclass
class ClusterProfile:
    total_gpus: int
    gpus_per_node: int          # typically 8
    gpu_memory_gb: float        # e.g. 80
    intra_node_bw_gbps: float   # NVLink, e.g. 900
    inter_node_bw_gbps: float   # InfiniBand, e.g. 400
    cross_rack_bw_gbps: float   # InfiniBand with oversubscription, e.g. 200
    nodes_per_rack: int         # e.g. 8

@dataclass
class ParallelismPlan:
    tp_degree: int
    pp_degree: int
    dp_degree: int
    micro_batch_size: int
    num_micro_batches: int
    gradient_accumulation_steps: int
    fsdp_enabled: bool
    activation_checkpointing: bool
    estimated_memory_per_gpu_gb: float
    estimated_bubble_ratio: float
    estimated_comm_overhead_pct: float

def compute_parallelism_plan(
    model: ModelProfile,
    cluster: ClusterProfile,
) -> ParallelismPlan:
    """
    Greedy heuristic: TP first (within node), then PP (within rack), then DP (across racks).
    The advisor tries increasing TP/PP degrees until the model fits in per-GPU memory,
    then fills the remaining GPU budget with DP replicas.
    """
    bytes_per_param = 2 if model.precision in ("bf16", "fp16") else 4
    total_param_bytes = model.params_billion * 1e9 * bytes_per_param
    total_optim_bytes = model.params_billion * 1e9 * 12  # fp32 master + Adam m1 + m2
    total_grad_bytes = total_param_bytes
    total_model_state = total_param_bytes + total_grad_bytes + total_optim_bytes

    # Phase 1: determine minimum TP degree to fit a single layer in GPU memory
    tp_degree = 1
    while tp_degree <= cluster.gpus_per_node:
        layer_params = estimate_layer_params(model)
        layer_mem = layer_params * 16 / tp_degree  # 16 bytes/param with optimizer
        # A layer plus activations for one micro-batch must fit
        act_per_layer = (2 * model.sequence_length * 4 * model.hidden_size *
                         bytes_per_param / tp_degree)
        if layer_mem + act_per_layer < cluster.gpu_memory_gb * 1e9 * 0.85:
            break
        tp_degree *= 2

    tp_degree = min(tp_degree, cluster.gpus_per_node)

    # Phase 2: determine PP degree based on total memory per GPU
    pp_degree = 1
    while True:
        layers_per_stage = model.num_layers // pp_degree
        per_gpu_state = total_model_state / (tp_degree * pp_degree)
        per_gpu_act = (layers_per_stage * 2 * model.sequence_length * 4 *
                       model.hidden_size * bytes_per_param / tp_degree)
        per_gpu_total = per_gpu_state + per_gpu_act
        if per_gpu_total < cluster.gpu_memory_gb * 1e9 * 0.85:
            break
        pp_degree *= 2
        if pp_degree > 32:
            raise ValueError("Model too large for available GPU memory even with PP=32")

    # Phase 3: DP fills the remaining GPUs
    gpus_per_replica = tp_degree * pp_degree
    dp_degree = cluster.total_gpus // gpus_per_replica
    if dp_degree < 1:
        raise ValueError(
            f"Need {gpus_per_replica} GPUs per replica but only {cluster.total_gpus} available"
        )

    # Phase 4: determine micro-batch sizing for pipeline schedule
    micro_batch_size = 4  # start small
    num_micro_batches = max(pp_degree * 4, 8)  # at least 4x PP stages for low bubble
    gradient_accumulation = model.global_batch_size // (dp_degree * micro_batch_size * num_micro_batches)
    gradient_accumulation = max(gradient_accumulation, 1)

    # Use FSDP within DP groups when model state per DP replica is > 50% of GPU memory
    per_dp_replica_state = total_model_state / (tp_degree * pp_degree)
    fsdp_enabled = per_dp_replica_state > cluster.gpu_memory_gb * 1e9 * 0.5

    activation_checkpointing = model.params_billion > 1.0

    bubble_ratio = (pp_degree - 1) / num_micro_batches if pp_degree > 1 else 0.0

    per_gpu_mem_gb = (total_model_state / (tp_degree * pp_degree *
                      (dp_degree if fsdp_enabled else 1))) / 1e9

    return ParallelismPlan(
        tp_degree=tp_degree,
        pp_degree=pp_degree,
        dp_degree=dp_degree,
        micro_batch_size=micro_batch_size,
        num_micro_batches=num_micro_batches,
        gradient_accumulation_steps=gradient_accumulation,
        fsdp_enabled=fsdp_enabled,
        activation_checkpointing=activation_checkpointing,
        estimated_memory_per_gpu_gb=per_gpu_mem_gb,
        estimated_bubble_ratio=bubble_ratio,
        estimated_comm_overhead_pct=estimate_comm_overhead(
            model, cluster, tp_degree, pp_degree, dp_degree
        ),
    )

def estimate_layer_params(model: ModelProfile) -> int:
    """Rough parameter count for one transformer layer."""
    attn = 4 * model.hidden_size ** 2  # Q, K, V, O projections
    ffn = 3 * model.hidden_size * model.ffn_size  # gate, up, down
    ln = 2 * model.hidden_size  # two layernorms
    return attn + ffn + ln
```

### Parallelism Selection Quick Reference

| Model Size | GPU Count | Recommended Strategy | TP | PP | DP | FSDP |
|---|---|---|---|---|---|---|
| 1-3B | 8 (1 node) | DDP or FSDP | 1 | 1 | 8 | Optional |
| 7B | 8 (1 node) | FSDP | 1 | 1 | 8 | Yes |
| 7B | 64 (8 nodes) | FSDP | 1 | 1 | 64 | Yes |
| 70B | 64 (8 nodes) | TP + FSDP | 8 | 1 | 8 | Yes |
| 70B | 256 (32 nodes) | TP + PP + DP | 8 | 4 | 8 | No |
| 200B | 256 (32 nodes) | TP + PP + DP | 8 | 4 | 8 | Optional |
| 200B | 1024 (128 nodes) | TP + PP + DP | 8 | 8 | 16 | No |
| 200B + MoE | 2048 (256 nodes) | TP + PP + DP + EP | 8 | 8 | 16 | No |

---

## 4. Cluster Scheduler Design

### Gang Scheduling

A training job requires all `N` workers to be placed atomically -- a half-placed job wastes GPUs waiting for the rest. The platform extends Kubernetes scheduling via Volcano (or a custom scheduler plugin) with the following semantics:

```yaml
# Internal representation: VolcanoJob wrapping the training job
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: llama-7b-finetune-v3
spec:
  schedulerName: training-scheduler
  minAvailable: 8               # all 8 pods must be placed or none
  queue: team-nlp-research
  policies:
    - event: PodEvicted
      action: RestartJob
    - event: TaskCompleted
      action: CompleteJob
  tasks:
    - name: worker
      replicas: 8
      template:
        spec:
          containers:
            - name: trainer
              resources:
                limits:
                  nvidia.com/gpu: 8
          affinity:
            # Topology-aware: prefer same rack for PP groups
            podAffinity:
              requiredDuringSchedulingIgnoredDuringExecution:
                - labelSelector:
                    matchLabels:
                      job: llama-7b-finetune-v3
                      pp-group: "0"
                  topologyKey: topology.kubernetes.io/rack
```

### Topology-Aware Placement Algorithm

```
GPU topology hierarchy (DGX-H100 cluster):

    Rack 0                    Rack 1                    Rack 2
    ┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
    │ Node 0  Node 1  │      │ Node 8  Node 9  │      │ Node 16 Node 17 │
    │ ┌─┬─┬─┬─┬─┬─┬─┬─┐│    │ ┌─┬─┬─┬─┬─┬─┬─┬─┐│    │ ┌─┬─┬─┬─┬─┬─┬─┬─┐│
    │ │0│1│2│3│4│5│6│7││    │ │0│1│2│3│4│5│6│7││    │ │0│1│2│3│4│5│6│7││
    │ └─┴─┴─┴─┴─┴─┴─┴─┘│    │ └─┴─┴─┴─┴─┴─┴─┴─┘│    │ └─┴─┴─┴─┴─┴─┴─┘│
    │     NVSwitch       │    │     NVSwitch       │    │     NVSwitch     │
    │ ┌─┬─┬─┬─┬─┬─┬─┬─┐│    │ ┌─┬─┬─┬─┬─┬─┬─┬─┐│    │ ┌─┬─┬─┬─┬─┬─┬─┬─┐│
    │ │0│1│2│3│4│5│6│7││    │ │0│1│2│3│4│5│6│7││    │ │0│1│2│3│4│5│6│7││
    │ └─┴─┴─┴─┴─┴─┴─┴─┘│    │ └─┴─┴─┴─┴─┴─┴─┴─┘│    │ └─┴─┴─┴─┴─┴─┴─┘│
    │ Node 2  Node 3  │      │ Node 10 Node 11 │      │ Node 18 Node 19 │
    │ ...             │      │ ...              │      │ ...              │
    │ Node 6  Node 7  │      │ Node 14 Node 15 │      │ Node 22 Node 23 │
    └───── IB Switch ──┘      └───── IB Switch ──┘      └───── IB Switch ──┘
           │                         │                         │
           └─────────────────────────┼─────────────────────────┘
                                     │
                              IB Spine Switch
                           (2:1 oversubscription)

Placement rules (hard constraints):
  1. TP group:   GPUs [0-7] on the SAME node (NVLink).
  2. PP group:   Nodes in the same rack (InfiniBand leaf, full bisection).
  3. DP replicas: Spread across racks (maximize fault isolation).
  4. All nodes in a job: same GPU type (H100 or A100, never mixed).

Placement rules (soft preferences, score-based):
  1. Prefer racks with the most contiguous free nodes (reduce fragmentation).
  2. Prefer nodes with healthy GPU history (no recent Xid errors).
  3. Prefer nodes with warm data cache (if dataset overlaps with prior job).
```

### Placement Scoring Function

```python
def score_placement(
    job: TrainingJob,
    candidate_nodes: list[Node],
    topology: ClusterTopology,
) -> float:
    """
    Score a candidate placement. Higher is better. Called by the scheduler
    for each feasible placement option; the highest-scoring option is chosen.
    """
    score = 0.0

    # 1. Topology affinity: TP within node, PP within rack
    tp_groups = partition_tp_groups(candidate_nodes, job.tp_degree)
    for group in tp_groups:
        if all_same_node(group):
            score += 100  # NVLink available
        else:
            return -float('inf')  # hard constraint violation

    pp_groups = partition_pp_groups(candidate_nodes, job.pp_degree)
    for group in pp_groups:
        rack_ids = {topology.rack_of(n) for n in group}
        if len(rack_ids) == 1:
            score += 50  # all PP stages in same rack, full IB bisection
        else:
            score -= 20 * len(rack_ids)  # penalty per additional rack

    # 2. Fault isolation: DP replicas across racks
    dp_groups = partition_dp_groups(candidate_nodes, job.dp_degree)
    rack_distribution = {topology.rack_of(n) for n in candidate_nodes}
    score += 10 * len(rack_distribution)  # more racks = more fault isolation

    # 3. Fragmentation: prefer contiguous free blocks
    for rack in rack_distribution:
        free_block_size = topology.largest_free_block(rack)
        score += 5 * free_block_size  # less fragmentation

    # 4. GPU health history
    for node in candidate_nodes:
        recent_xid_count = node.gpu_xid_errors_last_7d
        score -= 2 * recent_xid_count  # penalize unreliable nodes

    # 5. Data locality (cache warmth)
    for node in candidate_nodes:
        if node.has_cached_dataset(job.dataset_id):
            score += 3

    return score
```

### Fair-Share Multi-Tenancy

```
Queue hierarchy:

    Platform (10,000 GPUs total)
    ├── team-foundation (40% share = 4,000 GPUs guaranteed)
    │   ├── critical jobs (preempt others within team)
    │   └── normal jobs
    ├── team-nlp-research (25% share = 2,500 GPUs)
    ├── team-vision (15% share = 1,500 GPUs)
    ├── team-speech (10% share = 1,000 GPUs)
    └── shared-pool (10% share = 1,000 GPUs, open to all)

Fair-share rules:
  1. Each team's guaranteed share is their MINIMUM allocation when
     the cluster is fully loaded.
  2. When a team is using LESS than its share, surplus GPUs are
     redistributed proportionally to other teams with pending demand.
  3. When a team exceeds its share (using borrowed GPUs), those excess
     jobs run at reduced priority and can be preempted if the lending
     team needs GPUs back.
  4. Preemption order: preemptible > low > borrowed-normal > normal > high > critical.
  5. Preemption is graceful: the platform triggers a checkpoint before
     evicting the preempted job, then requeues it.
```

```python
# Scheduler priority computation
def compute_effective_priority(job: TrainingJob, team_usage: TeamUsage) -> float:
    base_priority = {
        "critical": 1000,
        "high": 800,
        "normal": 500,
        "low": 200,
        "preemptible": 50,
    }[job.priority]

    # Penalize teams that are over their fair share
    usage_ratio = team_usage.current_gpus / team_usage.guaranteed_share
    if usage_ratio > 1.0:
        # Over-quota penalty: each 10% over share drops priority by 100
        over_penalty = (usage_ratio - 1.0) * 1000
        base_priority -= over_penalty

    # Bonus for jobs that have been waiting a long time (starvation prevention)
    wait_hours = (now() - job.submitted_at).total_seconds() / 3600
    starvation_bonus = min(wait_hours * 20, 200)

    return base_priority + starvation_bonus
```

### Elastic Scaling

For data-parallel jobs (no PP dimension), the platform can add or remove DP replicas mid-training:

```
Scale-out trigger:
  1. Team's GPU allocation has new capacity (lower-priority job finished).
  2. Job's current batch throughput is below target and more GPUs would help.

Scale-out procedure:
  1. Job Controller requests N additional nodes from Scheduler.
  2. Scheduler places new pods (topology-aware, same constraints as initial placement).
  3. New pods pull the latest in-memory checkpoint from an existing worker.
  4. Job Controller calls the training framework's elastic resize API:
     - PyTorch: torch.distributed.elastic (torchelastic) handles group resize.
     - The rendezvous is re-run with the new world size.
  5. Learning rate schedule is adjusted: linear scaling rule (LR *= new_world_size / old_world_size)
     or the user's custom callback.
  6. Data loader re-shards: new workers pick up shards from the adjusted shard assignment.

Scale-in procedure (reverse):
  1. Trigger: team needs GPUs back (preemption of over-quota borrowed GPUs).
  2. Checkpoint is taken immediately.
  3. Excess workers are gracefully removed (torchelastic shrinks the group).
  4. Remaining workers continue from the checkpoint with adjusted world size.
```

---

## 5. Fault Tolerance Design

### Failure Detection

```
 ┌───────────────────────────────────────────────────────────────────┐
 │                     Node Agent (per node)                          │
 │                                                                    │
 │  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────┐  │
 │  │ Heartbeat Sender │  │ GPU Health       │  │ NCCL Watchdog     │  │
 │  │ (every 5s to     │  │ Monitor          │  │ (monitors         │  │
 │  │  control plane)  │  │ (nvidia-smi,     │  │  collective ops   │  │
 │  │                  │  │  DCGM polling    │  │  for timeouts)    │  │
 │  │ 3 missed = dead  │  │  every 10s)      │  │                   │  │
 │  └─────────────────┘  └─────────────────┘  └──────────────────┘  │
 │                                                                    │
 │  Detected signals:                                                 │
 │  - HEARTBEAT_TIMEOUT: node unreachable (network or crash)         │
 │  - GPU_XID_ERROR: Xid 48 (double-bit ECC), Xid 79 (fallen off)  │
 │  - GPU_THERMAL_THROTTLE: sustained >83C for >60s                  │
 │  - NCCL_TIMEOUT: collective op not completed in 300s              │
 │  - TRAINING_DIVERGED: loss is NaN or >10x baseline for 100 steps │
 │  - OOM_KILLED: GPU out of memory (CUDA OOM)                      │
 │  - SPOT_RECLAMATION: cloud preemption notice (2 min warning)      │
 └───────────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌───────────────────────────────────────────────────────────────────┐
 │                   Recovery Controller                               │
 │                                                                     │
 │  Failure classification:                                            │
 │  ┌──────────────────┬────────────────┬───────────────────────────┐ │
 │  │ Failure Type       │ Recovery Action │ Expected Recovery Time    │ │
 │  ├──────────────────┼────────────────┼───────────────────────────┤ │
 │  │ Single GPU error   │ Replace node    │ 2-4 minutes               │ │
 │  │ Full node loss     │ Replace node    │ 2-4 minutes               │ │
 │  │ NCCL hang          │ Kill + restart  │ 3-5 minutes               │ │
 │  │                    │ affected group  │                           │ │
 │  │ Training diverged  │ Rollback to     │ 1-2 minutes               │ │
 │  │                    │ last good ckpt  │ (no node change)          │ │
 │  │ Spot reclamation   │ Checkpoint +    │ 3-5 minutes               │ │
 │  │                    │ replace node    │                           │ │
 │  │ Network partition  │ Wait 60s, then  │ 1-6 minutes               │ │
 │  │ between racks      │ checkpoint +    │                           │ │
 │  │                    │ rearrange       │                           │ │
 │  │ Checkpoint corrupt │ Fall back to    │ 5-10 minutes              │ │
 │  │                    │ previous ckpt   │ (retrains from earlier)   │ │
 │  └──────────────────┴────────────────┴───────────────────────────┘ │
 └───────────────────────────────────────────────────────────────────┘
```

### Recovery State Machine (per job)

```
                            failure detected
                                  │
                                  ▼
                     ┌─────────────────────┐
                     │  FAILURE_DETECTED     │  (timestamp, type, affected nodes)
                     └──────────┬──────────┘
                                │
                   ┌────────────┤
                   │            │
         node failure    training diverged
                   │            │
                   ▼            ▼
      ┌────────────────┐  ┌──────────────────┐
      │ NODE_REPLACING   │  │ ROLLING_BACK      │  (to last known-good checkpoint)
      │ (request new     │  │                    │
      │  node from       │  └─────────┬────────┘
      │  scheduler)      │            │
      └───────┬────────┘            │
              │  node allocated      │
              ▼                      │
      ┌────────────────┐            │
      │ CHECKPOINT_     │            │
      │ RESTORING       │◀───────────┘
      │ (load from      │
      │  object store   │
      │  or in-memory   │
      │  replica)       │
      └───────┬────────┘
              │  all workers have restored state
              ▼
      ┌────────────────┐
      │ NCCL_RENDEZVOUS │  (re-establish process group with new topology)
      └───────┬────────┘
              │  NCCL init_process_group succeeds
              ▼
      ┌────────────────┐
      │ DATA_REBALANCE   │  (re-shard data loader, skip consumed batches)
      └───────┬────────┘
              │  data loaders ready
              ▼
      ┌────────────────┐
      │ TRAINING_RESUMED │  ──▶ job state = RUNNING
      └────────────────┘
```

### Checkpoint Architecture

```
                      Training Loop (GPU)
                              │
               every N steps  │
                              ▼
                    ┌──────────────────┐
                    │  Snapshot State    │  CPU-side copy of model state,
                    │  to CPU Memory     │  optimizer state, LR scheduler,
                    │  (non-blocking)   │  RNG state, data loader position
                    └────────┬─────────┘
                             │  training continues immediately
                             │  (no GPU blocked past the D2H copy)
                             ▼
                    ┌──────────────────┐
                    │  Write to Local    │  Background CPU thread writes
                    │  NVMe SSD          │  to /scratch/checkpoints/
                    │  (staging area)    │  using direct I/O
                    └────────┬─────────┘
                             │  local write completes (~5-10s for 50GB)
                             ▼
                    ┌──────────────────┐
                    │  Upload to Object  │  Background thread uploads
                    │  Store (S3)        │  from NVMe to S3, multi-part,
                    │  (async, retriable)│  with progress tracking
                    └────────┬─────────┘
                             │
                    ┌────────┴─────────┐
                    │                    │
                    ▼                    ▼
           ┌──────────────┐    ┌──────────────────┐
           │  In-Memory     │    │  Register in       │
           │  Replication   │    │  Checkpoint DB      │
           │  (send to 1-2  │    │  (step, path, size, │
           │  peer nodes    │    │   metrics, SHA256)  │
           │  via RDMA)     │    │                      │
           └──────────────┘    └──────────────────┘
```

### Checkpoint Sizing Arithmetic

```
=== 7B Model, FSDP across 8 GPUs ===

Per-GPU FSDP shard:
  Parameters: 7B / 8 * 2 bytes (bf16) = 1.75 GB
  Optimizer state: 7B / 8 * 12 bytes (fp32 master + m1 + m2) = 10.5 GB
  Gradients: not checkpointed (recomputed)
  RNG state + LR scheduler + misc: ~1 MB

Per-GPU checkpoint shard: ~12.25 GB
Total across all 8 GPUs: 12.25 * 8 = 98 GB
With LZ4 compression (~1.5x ratio on fp32 data): ~65 GB

Write to local NVMe (3.5 GB/s sequential write per drive):
  12.25 GB / 3.5 GB/s = 3.5 seconds per GPU (each writes its own shard)

Upload to S3 (assuming 10 Gbps per node, 1.25 GB/s):
  12.25 GB / 1.25 GB/s = 9.8 seconds per GPU (parallel uploads)

In-memory replication to 2 peers (InfiniBand, 50 GB/s):
  12.25 GB / 50 GB/s = 0.25 seconds

Total checkpoint latency (training not blocked, async):
  D2H copy: ~0.5s
  Local NVMe write: ~3.5s (overlapped with continued training)
  S3 upload: ~10s (overlapped with continued training)
  In-memory replication: ~0.25s (overlapped)

Training overhead: only the D2H copy blocks the GPU briefly.
With double-buffering (copy to CPU while training on next batch), overhead < 1%.

=== 200B Model, TP=8 PP=4 DP=8 (256 GPUs, 32 nodes) ===

Per-GPU state:
  Parameters: 200B / 32 * 2 bytes = 12.5 GB (bf16, TP*PP sharded)
  Optimizer state: 200B / 32 * 12 bytes = 75 GB
  Total per GPU: ~87.5 GB

Total checkpoint: 87.5 * 256 = ~22.4 TB (uncompressed)
With LZ4: ~15 TB
With incremental dedup (only changed params since last checkpoint,
  typically <5% of optimizer states change significantly): ~3-5 TB delta

Per-node write to NVMe (8 GPUs, 8 NVMe drives):
  87.5 GB * 8 / (3.5 GB/s * 8) = 25 seconds (parallel across drives)

S3 upload (10 Gbps per node, 32 nodes in parallel):
  87.5 * 8 GB per node / 1.25 GB/s = 560 seconds per node
  But all 32 nodes upload in parallel, so wall clock = 560 seconds
  With incremental: ~100-180 seconds

Checkpoint every 500 steps. At ~45 seconds per step (200B model):
  500 * 45 = 22,500 seconds between checkpoints
  Checkpoint overhead: 560 / 22,500 = 2.5% of training time for upload
  But training is NOT blocked (async upload), so real overhead is only
  the D2H copy: ~1-2 seconds / 45 seconds = ~3% of one step, amortized
  over 500 steps = 0.006%. Well within the 2% target.
```

### Checkpoint Retention Policy

```python
class CheckpointRetentionPolicy:
    """
    Applied by the Checkpoint Manager after each new checkpoint is written.
    Multiple policies compose: a checkpoint is kept if ANY policy wants it.
    """
    def should_keep(self, ckpt: CheckpointMetadata, all_ckpts: list[CheckpointMetadata]) -> bool:
        # Policy 1: keep the last N checkpoints
        if ckpt in sorted(all_ckpts, key=lambda c: c.step, reverse=True)[:self.keep_last_n]:
            return True

        # Policy 2: keep the best K by a tracked metric (e.g., eval_loss)
        by_metric = sorted(
            [c for c in all_ckpts if c.metrics.get(self.best_metric) is not None],
            key=lambda c: c.metrics[self.best_metric],
        )
        if ckpt in by_metric[:self.keep_best_k]:
            return True

        # Policy 3: keep every Mth checkpoint (for long-running experiments)
        if ckpt.step % self.keep_every_m_steps == 0:
            return True

        # Policy 4: never delete a checkpoint younger than min_age
        if (now() - ckpt.created_at) < self.min_age:
            return True

        return False  # eligible for deletion

    def apply(self, all_ckpts: list[CheckpointMetadata]) -> list[CheckpointMetadata]:
        to_delete = [c for c in all_ckpts if not self.should_keep(c, all_ckpts)]
        return to_delete
```

### In-Memory Checkpoint Replication

For fast recovery without hitting object storage:

```
                    Node 0 (rank 0-7)
                    │  checkpoint at step 1000
                    │
            ┌───────┼───────┐
            │       │       │
            ▼       ▼       ▼
       Node 1   Node 4   Node 7     (replicas chosen to maximize
       (same     (different (different  fault isolation: different
        rack)     rack)      rack)      racks preferred)

Recovery from in-memory replica:
  1. Failed node's rank assignment is given to a new node.
  2. New node requests checkpoint shards from the replica holders.
  3. Transfer over RDMA: 87.5 GB / 50 GB/s = 1.75 seconds.
  4. Compare with: S3 restore: 87.5 GB / 1.25 GB/s = 70 seconds.

Speed improvement: 40x faster recovery from in-memory replica vs S3.
```

---

## 6. Data Pipeline Architecture

### Distributed Data Loading

```
 ┌──────────────────────────────────────────────────────────────┐
 │                    Data Pipeline (per worker)                  │
 │                                                                │
 │  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐  │
 │  │  Shard         │   │  Prefetch     │   │  Preprocessing    │  │
 │  │  Assignment    │   │  from S3      │   │  (tokenize,       │  │
 │  │  (deterministic│──▶│  (async,      │──▶│   augment,        │──▶ GPU
 │  │   based on     │   │   multi-conn) │   │   pad/pack)       │  │
 │  │   rank + seed) │   │              │   │  (CPU workers)    │  │
 │  └──────────────┘   └──────────────┘   └──────────────────┘  │
 │                                                                │
 │  Cache hierarchy:                                              │
 │  L1: GPU memory (current micro-batch, pinned)                  │
 │  L2: CPU RAM (next N micro-batches, prefetched)                │
 │  L3: Local NVMe (working set for current epoch shard)          │
 │  L4: Shared NFS / Lustre (if configured, cluster-wide cache)  │
 │  L5: Object Store (S3/GCS, source of truth, unlimited)         │
 └──────────────────────────────────────────────────────────────┘
```

### Deterministic Resumable Shuffling

After a restart, the data loader must replay the exact same data order to guarantee reproducibility:

```python
class DeterministicDistributedSampler:
    """
    Generates a deterministic permutation of the dataset based on (seed, epoch).
    After restart, fast-forwards to the exact sample index where training left off.
    """
    def __init__(
        self,
        dataset_size: int,
        rank: int,
        world_size: int,
        seed: int,
        epoch: int = 0,
        start_index: int = 0,  # resume point, from checkpoint
    ):
        self.dataset_size = dataset_size
        self.rank = rank
        self.world_size = world_size
        self.seed = seed
        self.epoch = epoch
        self.start_index = start_index

    def __iter__(self):
        # Generate full permutation (deterministic given seed + epoch)
        g = torch.Generator()
        g.manual_seed(self.seed + self.epoch)
        perm = torch.randperm(self.dataset_size, generator=g)

        # Shard assignment: this rank gets every world_size-th sample
        indices = perm[self.rank::self.world_size]

        # Fast-forward past already-consumed samples
        indices = indices[self.start_index:]

        return iter(indices.tolist())

    def state_dict(self) -> dict:
        """Saved in checkpoint for exact resume."""
        return {
            "epoch": self.epoch,
            "samples_consumed": self.start_index,
            "seed": self.seed,
        }
```

### Streaming from Object Storage

For 100 TB+ datasets that cannot fit on local disk:

```python
class StreamingDataLoader:
    """
    Streams data directly from S3 with multi-level caching.
    Never requires the full dataset to be present locally.
    """
    def __init__(self, config: DatasetConfig, rank: int, world_size: int):
        self.s3_client = boto3.client('s3')
        self.local_cache = NVMeCache(
            path="/scratch/data_cache",
            max_size_gb=500,  # use up to 500 GB of local NVMe for caching
            eviction="lru",
        )
        self.prefetch_buffer = asyncio.Queue(maxsize=64)  # 64 batches ahead
        self.rank = rank
        self.world_size = world_size

    async def prefetch_loop(self):
        """Background task: continuously prefetch upcoming data shards."""
        for shard_info in self.shard_schedule:
            # Check cache hierarchy
            data = self.local_cache.get(shard_info.key)
            if data is None:
                # Cache miss: fetch from S3
                data = await self._fetch_from_s3(shard_info)
                self.local_cache.put(shard_info.key, data)

            # Preprocess on CPU
            processed = self.preprocessor.process(data)

            # Place in prefetch buffer (blocks if buffer is full,
            # applying backpressure to prefetching)
            await self.prefetch_buffer.put(processed)

    async def _fetch_from_s3(self, shard: ShardInfo) -> bytes:
        """Fetch with retry, range requests for large shards."""
        response = self.s3_client.get_object(
            Bucket=shard.bucket,
            Key=shard.key,
            Range=f"bytes={shard.offset}-{shard.offset + shard.length - 1}",
        )
        return response['Body'].read()
```

### Cache Sizing Arithmetic

```
Dataset: 100 TB, training for 3 epochs
Per-worker data volume per epoch: 100 TB / 256 workers = 390 GB
Per-worker data volume per step: 390 GB / 100,000 steps = 3.9 MB

Local NVMe cache per node: 500 GB (out of 8 * 3.84 TB available)
  = covers ~1.3 epochs of data per worker on a node
  = after epoch 1, epoch 2 is ~70% cache hits (LRU eviction)

Prefetch buffer in CPU RAM: 64 batches * ~4 MB per batch = 256 MB per worker
  = 2 GB per node (8 workers), negligible relative to 2 TB system RAM

S3 bandwidth per node: 10 Gbps = 1.25 GB/s
  Time to fill NVMe cache: 500 GB / 1.25 GB/s = 400 seconds (~7 min)
  (This happens during the first epoch; subsequent epochs are cache-warm.)

Training step time (7B model, 64 GPUs): ~200 ms per step
  Data needed per step per worker: 3.9 MB
  Data bandwidth needed: 3.9 MB / 0.2 s = 19.5 MB/s per worker = 156 MB/s per node
  Local NVMe read bandwidth: 7 GB/s (sequential reads across 8 drives)
  Bandwidth utilization: 156 MB/s / 7000 MB/s = 2.2% -- data loading is not
  the bottleneck even at high throughput.
```

---

## 7. Experiment Tracking and Observability

### Metrics Collection Architecture

```
  Worker Pod (rank 0-N)                Control Plane
  ┌────────────────────┐              ┌────────────────────────┐
  │ Training Script     │              │ Metrics Aggregator      │
  │ │                   │   gRPC       │ (Prometheus + custom    │
  │ ├─ loss: 2.34       │─────────────▶│  push receiver)         │
  │ ├─ grad_norm: 0.87  │  every 10    │                         │
  │ ├─ lr: 3e-4         │  steps       │ Stores:                 │
  │ ├─ tokens_per_sec   │              │  - time-series DB       │
  │ ├─ gpu_util: 92%    │              │    (VictoriaMetrics)    │
  │ ├─ gpu_mem: 74.2 GB │              │  - experiment metadata  │
  │ ├─ nccl_time_ms     │              │    (PostgreSQL)         │
  │ └─ step: 15023      │              │  - TensorBoard events   │
  └────────────────────┘              │    (object store)       │
                                       └────────────────────────┘
         │
         │  DCGM + Node Agent (always-on, independent of training)
         ▼
  ┌────────────────────┐
  │ GPU Telemetry       │
  │ (per GPU, 10s poll) │
  │  - SM utilization    │
  │  - memory util       │
  │  - temperature       │
  │  - power draw        │
  │  - ECC errors        │
  │  - NVLink throughput │
  │  - PCIe throughput   │
  │  - Xid error count   │
  └────────────────────┘
```

### Key Metrics and Alerts

| Metric | Collection | Alert Threshold | Severity |
|---|---|---|---|
| `training_loss` | Per step, rank 0 | NaN or >10x baseline for 100 steps | High -- trigger rollback |
| `gradient_norm` | Per step, rank 0 | >100 (gradient explosion indicator) | Warning |
| `tokens_per_second` | Per step, all ranks | <80% of initial steady-state | Warning -- straggler or degradation |
| `gpu_utilization` | Per GPU, 10s | <70% sustained for 10 min | Warning -- possible data stall or comm bottleneck |
| `gpu_memory_used` | Per GPU, 10s | >95% of total | Warning -- OOM risk |
| `gpu_temperature` | Per GPU, 10s | >83C sustained >60s | High -- thermal throttling |
| `gpu_ecc_errors` | Per GPU, 10s | Any uncorrectable (double-bit) | Critical -- GPU failing |
| `nccl_collective_time_ms` | Per collective, sampled | >3x rolling average | Warning -- network degradation |
| `checkpoint_upload_time_s` | Per checkpoint | >2x previous | Warning -- storage bottleneck |
| `data_loader_stall_time_s` | Per step, per worker | >10% of step time | Warning -- data pipeline bottleneck |
| `mfu` (Model FLOP/s Utilization) | Computed from throughput + model FLOPs | <35% sustained | Warning -- inefficient training config |

### MFU Calculation

```python
def compute_mfu(
    tokens_per_second: float,
    model_params_billion: float,
    num_gpus: int,
    gpu_peak_tflops: float,  # H100: 989 TFLOPs bf16
) -> float:
    """
    Model FLOP/s Utilization: what fraction of theoretical peak FLOPs
    is actually used for model computation (not communication, not idle).

    Approximate FLOPs per token for a transformer:
      6 * num_params (forward + backward = 3x forward, forward = 2 * params)
    """
    model_flops_per_token = 6 * model_params_billion * 1e9
    achieved_flops_per_second = tokens_per_second * model_flops_per_token
    peak_flops_per_second = num_gpus * gpu_peak_tflops * 1e12
    return achieved_flops_per_second / peak_flops_per_second

# Example: 200B model on 256 H100 GPUs
# Observed: 1,200 tokens/second
# MFU = 1200 * 6 * 200e9 / (256 * 989e12)
#      = 1.44e15 / 2.53e17
#      = 0.0057 ... wait, that is wrong. Let me redo.
# tokens_per_second is total across all GPUs.
# FLOPs per token = 6 * 200e9 = 1.2e12
# Achieved = 1200 * 1.2e12 = 1.44e15 FLOP/s
# Peak = 256 * 989e12 = 2.53e17 FLOP/s
# MFU = 1.44e15 / 2.53e17 = 0.57% -- something is off.
#
# The issue: tokens_per_second for a 200B model at scale is typically
# measured differently. Let me use a realistic number.
# A well-tuned 200B model on 256 H100s achieves ~100k tokens/sec total.
# MFU = 100000 * 1.2e12 / 2.53e17 = 1.2e17 / 2.53e17 = 47.4%
# That aligns with published benchmarks (40-50% MFU for large models).
```

### Experiment Metadata Schema

```sql
CREATE TABLE training_runs (
    run_id              UUID PRIMARY KEY,
    job_name            TEXT NOT NULL,
    team                TEXT NOT NULL,
    project             TEXT NOT NULL,
    status              TEXT NOT NULL,  -- pending | running | completed | failed | cancelled
    -- Source
    git_repo            TEXT NOT NULL,
    git_commit          TEXT NOT NULL,
    entrypoint          TEXT NOT NULL,
    -- Config
    model_config        JSONB NOT NULL,    -- model architecture params
    training_config     JSONB NOT NULL,    -- hyperparameters
    parallelism_config  JSONB NOT NULL,    -- TP, PP, DP degrees
    hardware_config     JSONB NOT NULL,    -- GPU type, count, nodes
    -- Dataset
    dataset_uri         TEXT NOT NULL,
    dataset_version     TEXT NOT NULL,     -- content hash or version tag
    -- Results
    final_metrics       JSONB,
    best_checkpoint_uri TEXT,
    best_metric_value   FLOAT,
    -- Resources
    total_gpu_hours     FLOAT,
    total_cost_usd      FLOAT,
    -- Timing
    submitted_at        TIMESTAMPTZ NOT NULL,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    wall_time_hours     FLOAT,
    training_time_hours FLOAT,             -- wall time minus recovery/idle
    -- Fault tolerance
    num_failures        INTEGER DEFAULT 0,
    num_checkpoints     INTEGER DEFAULT 0,
    total_recovery_time_s INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_runs_team ON training_runs (team, project, created_at DESC);
CREATE INDEX idx_runs_status ON training_runs (status) WHERE status = 'running';
```

### Cost Tracking per Job

```python
def compute_job_cost(run: TrainingRun) -> JobCost:
    """
    GPU-hour cost computation with breakdown.
    """
    gpu_hour_rates = {
        "H100": 3.50,      # $/GPU-hour (on-prem amortized or cloud on-demand)
        "H200": 4.50,
        "A100_80GB": 2.50,
    }
    rate = gpu_hour_rates[run.hardware_config["gpu_type"]]

    # Total GPU-hours = num_gpus * wall_clock_hours
    total_gpu_hours = run.hardware_config["gpu_count"] * run.wall_time_hours

    # Breakdown
    training_gpu_hours = run.hardware_config["gpu_count"] * run.training_time_hours
    idle_gpu_hours = total_gpu_hours - training_gpu_hours  # scheduling wait + recovery

    return JobCost(
        total_gpu_hours=total_gpu_hours,
        training_gpu_hours=training_gpu_hours,
        idle_gpu_hours=idle_gpu_hours,
        total_cost_usd=total_gpu_hours * rate,
        training_cost_usd=training_gpu_hours * rate,
        idle_waste_usd=idle_gpu_hours * rate,
        idle_pct=idle_gpu_hours / total_gpu_hours * 100,
        gpu_utilization_pct=run.avg_gpu_utilization,
        mfu_pct=run.avg_mfu,
    )
```

---

## 8. Network Architecture

### Physical Topology

```
                            IB Spine Switches
                         (2:1 oversubscription)
               ┌──────────────┬────────────────┬──────────────┐
               │              │                │              │
        IB Leaf Switch  IB Leaf Switch   IB Leaf Switch  IB Leaf Switch
        (Rack 0, full   (Rack 1)          (Rack 2)        (Rack 3)
         bisection)
        ┌──────┐       ┌──────┐          ┌──────┐       ┌──────┐
        │      │       │      │          │      │       │      │
     Node 0  Node 1  Node 8  Node 9   Node 16 Node 17  Node 24 Node 25
     ...     ...     ...     ...       ...     ...      ...     ...
     Node 6  Node 7  Node 14 Node 15  Node 22 Node 23  Node 30 Node 31

     Each node: 8x H100 + NVSwitch (full NVLink mesh within node)

     Bandwidth tiers:
     ┌──────────────────────────────────────────────────────────────┐
     │ Level          │ Bandwidth (per GPU)    │ Latency           │
     ├──────────────────────────────────────────────────────────────┤
     │ Intra-GPU      │ 3.35 TB/s (HBM3e)     │ ~nanoseconds      │
     │ NVLink (intra  │ 900 GB/s bidir         │ ~1-5 us           │
     │   node)        │ (NVSwitch mesh)        │                   │
     │ InfiniBand     │ 400 Gbps = 50 GB/s     │ ~1-3 us           │
     │ (intra-rack)   │ (NDR, per port)        │                   │
     │ InfiniBand     │ ~200 Gbps = 25 GB/s    │ ~3-10 us          │
     │ (cross-rack)   │ (2:1 oversubscription) │                   │
     └──────────────────────────────────────────────────────────────┘
```

### NCCL Communication Groups

For the 200B model example (TP=8, PP=4, DP=8, 256 GPUs across 32 nodes):

```
Mapping parallelism to physical topology:

DP Replica 0:                    DP Replica 1:
  Rack 0:                          Rack 1:
  ┌─Node 0──────────────────┐    ┌─Node 8──────────────────┐
  │ GPU 0,1,2,3,4,5,6,7     │    │ GPU 64,65,66,67,...,71  │
  │ TP Group 0 (PP Stage 0)  │    │ TP Group 4 (PP Stage 0)  │
  └──────────────────────────┘    └──────────────────────────┘
  ┌─Node 1──────────────────┐    ┌─Node 9──────────────────┐
  │ GPU 8,9,10,11,...,15     │    │ GPU 72,...,79            │
  │ TP Group 1 (PP Stage 1)  │    │ TP Group 5 (PP Stage 1)  │
  └──────────────────────────┘    └──────────────────────────┘
  ┌─Node 2──────────────────┐    ┌─Node 10─────────────────┐
  │ GPU 16,...,23            │    │ GPU 80,...,87            │
  │ TP Group 2 (PP Stage 2)  │    │ TP Group 6 (PP Stage 2)  │
  └──────────────────────────┘    └──────────────────────────┘
  ┌─Node 3──────────────────┐    ┌─Node 11─────────────────┐
  │ GPU 24,...,31            │    │ GPU 88,...,95            │
  │ TP Group 3 (PP Stage 3)  │    │ TP Group 7 (PP Stage 3)  │
  └──────────────────────────┘    └──────────────────────────┘

  ... (DP Replicas 2-7 in Racks 2-7)

NCCL process group configuration:

1. TP groups (AllReduce within each group, NVLink):
   Group 0: [GPU 0, 1, 2, 3, 4, 5, 6, 7]       -- Node 0
   Group 1: [GPU 8, 9, 10, 11, 12, 13, 14, 15]   -- Node 1
   ... (32 TP groups total, one per node)

2. PP groups (Point-to-point send/recv, InfiniBand intra-rack):
   Group 0: [GPU 0, GPU 8, GPU 16, GPU 24]        -- Node 0-3, same TP position 0
   Group 1: [GPU 1, GPU 9, GPU 17, GPU 25]        -- same TP position 1
   ... (8 PP groups per DP replica, 64 total)

3. DP groups (AllReduce of gradients, InfiniBand cross-rack):
   Group 0: [GPU 0, GPU 64, GPU 128, ..., GPU 448] -- same TP pos + PP stage across replicas
   ... (32 DP groups total, one per TP_pos x PP_stage)
```

### Communication Volume per Training Step

```
=== 200B model, TP=8, PP=4, DP=8 ===

1. Tensor Parallel AllReduce (per layer, forward + backward):
   Volume per AllReduce = 2 * hidden_size * seq_len * micro_batch * dtype_size / TP
   = 2 * 12288 * 4096 * 4 * 2 / 8 = 96 MB
   Over NVLink (900 GB/s): 96 MB / 900 GB/s = 0.1 ms
   Per step (96 layers * 2 directions): 96 * 2 * 0.1 ms = 19.2 ms
   Overlap with computation: ~80% overlapped = 3.8 ms exposed

2. Pipeline Parallel point-to-point (per micro-batch, per stage boundary):
   Volume = seq_len * micro_batch * hidden_size * dtype_size
   = 4096 * 4 * 12288 * 2 = 384 MB per activation transfer
   Over InfiniBand (50 GB/s): 384 MB / 50 GB/s = 7.7 ms
   Per step (32 micro-batches * 3 stage boundaries): 32 * 3 * 7.7 = 739 ms
   But pipeline schedule overlaps most transfers: effective ~100-150 ms

3. Data Parallel AllReduce (gradients, once per step):
   Gradient volume per DP group member: 200B / 32 * 2 bytes = 12.5 GB
   Ring AllReduce across 8 DP replicas: 2 * 12.5 * 7/8 = 21.9 GB
   Over cross-rack InfiniBand (25 GB/s effective): 21.9 / 25 = 0.88 seconds
   Overlap with backward computation (gradient-as-ready): ~70% overlapped
   Exposed time: ~0.26 seconds

Total communication time per step (exposed, after overlap):
   TP: ~4 ms + PP: ~120 ms + DP: ~260 ms = ~384 ms
   Step time (compute-only): ~30 seconds (estimated)
   Communication overhead: 384 ms / 30000 ms = 1.3% -- well below 10% target

   Note: DP AllReduce overlap assumes bucket-based async AllReduce
   (PyTorch DDP's GradBucket). Without overlap, DP alone would be
   0.88s / 30s = 2.9%, still within target.
```

### NCCL Configuration

```bash
# Platform-injected NCCL environment variables (per job, tuned by advisor)

# Transport selection
NCCL_IB_DISABLE=0                # Use InfiniBand (not TCP)
NCCL_NET_GDR_LEVEL=5             # GPUDirect RDMA: GPU memory <-> IB HCA directly
NCCL_IB_HCA=mlx5_0:1,mlx5_1:1   # Use both IB HCA ports
NCCL_SOCKET_IFNAME=ib0            # Interface for bootstrap

# Algorithm selection (auto-tuned by NCCL, but can be overridden)
NCCL_ALGO=Ring                    # Ring for AllReduce (DP gradients)
                                  # Tree for large messages with many nodes
# Tuning
NCCL_BUFFSIZE=8388608             # 8 MB NCCL buffer (larger for large messages)
NCCL_NTHREADS=512                 # NCCL threads per GPU
NCCL_MAX_NCHANNELS=32             # Max parallel channels

# Timeout and debugging
NCCL_TIMEOUT=300                  # 5 min timeout for collective ops (detect hangs)
NCCL_DEBUG=WARN                   # Default WARN; INFO for debugging
NCCL_DEBUG_SUBSYS=INIT,COLL       # Subsystems to debug

# Performance
CUDA_DEVICE_MAX_CONNECTIONS=1     # Serialize CUDA kernels (avoids SM contention
                                  # between compute and communication kernels)
```

---

## 9. Capacity Estimates

### Cluster-Wide Resource Budget

```
Total platform: 10,000 GPUs (H100 80GB)
  = 1,250 nodes (8 GPUs each)
  = ~160 racks (8 nodes per rack)

Aggregate GPU memory: 10,000 * 80 GB = 800 TB
Aggregate GPU compute: 10,000 * 989 TFLOPs (bf16) = 9.89 ExaFLOPs peak
Aggregate NVMe storage: 1,250 * 8 * 3.84 TB = 38.4 PB local scratch
Aggregate InfiniBand bandwidth: 10,000 * 50 GB/s = 500 TB/s (intra-rack)
```

### Storage Requirements

```
=== Checkpoint Storage ===

Concurrent training jobs: 200
Average checkpoint size per job (compressed):
  - Small jobs (8 GPUs, 7B): 65 GB * 5 kept = 325 GB per job
  - Medium jobs (64 GPUs, 70B): 800 GB * 5 kept = 4 TB per job
  - Large jobs (256+ GPUs, 200B): 5 TB * 5 kept = 25 TB per job

Distribution assumption: 150 small, 40 medium, 10 large
Total checkpoint storage:
  150 * 325 GB + 40 * 4 TB + 10 * 25 TB
  = 48.75 TB + 160 TB + 250 TB
  = ~459 TB active checkpoints

With 30-day retention for completed jobs:
  ~30 * 200 jobs * average 2 TB = 12 PB (this is where lifecycle
  policies and incremental dedup matter -- without them, storage
  grows linearly and becomes the dominant cost)

=== Dataset Storage ===

Typical dataset sizes: 10 GB to 100 TB per job
Total unique dataset volume: ~500 TB (deduplicated across teams)
  + versioned copies: ~2 PB total

=== Metrics and Logs ===

Metrics rate: 200 jobs * 256 avg GPUs * 10 metrics * every 10s
  = 51,200 metric points/second
  At ~100 bytes per point: 5 MB/s = 432 GB/day
  30-day retention: ~13 TB

Training logs: ~1 MB/min per job = 200 * 1 MB/min = 200 MB/min = 288 GB/day
  30-day retention: ~8.6 TB
```

### Network Bandwidth Requirements

```
=== Worst case: 4,096-GPU training job spanning the cluster ===

TP communication (intra-node NVLink): contained, no network impact
PP communication (intra-rack IB): 4096 / 8 = 512 nodes, ~64 racks
  PP traffic: 384 MB * 32 micro-batches * 3 boundaries / step_time
  Per rack pair: ~3 GB/s sustained
  Within leaf switch capacity (400 Gbps = 50 GB/s), ~6% utilization

DP AllReduce (cross-rack): 4096 / (8 TP * 8 PP) = 64 DP replicas
  Gradient volume: 200B * 2 / 64 = 6.25 GB per DP member
  Ring AllReduce: 2 * 6.25 * 63/64 = 12.3 GB across 64 members
  Cross-rack bandwidth needed: 12.3 GB / 30 s step = 410 MB/s per GPU
  Per spine port: 8 GPUs/node * 8 nodes/rack * 410 MB/s = 26 GB/s
  Spine port capacity: 200 Gbps = 25 GB/s
  Utilization: 104% -- EXCEEDS capacity! This is why DP AllReduce must
  overlap with backward computation. With 70% overlap:
  Effective demand: 0.3 * 26 = 7.8 GB/s per spine port = 31% utilization.

  Alternatively, for 4096-GPU jobs with DP > 32:
  Use FSDP with sharded AllReduce (ReduceScatter + AllGather) which
  has better bandwidth efficiency than Ring AllReduce for large groups.
```

---

## 10. Failure Walkthroughs

### Scenario 1: Single GPU Failure Mid-Training (Xid 79 -- GPU Fallen Off Bus)

```
t=0s      Node Agent on Node 5 detects Xid 79 error from DCGM on GPU 3.
          GPU 3 is no longer responding to CUDA calls.

t=0.5s    Node Agent reports GPU_FAILURE event to Recovery Controller.
          Event: {node: 5, gpu: 3, type: XID_79, job: llama-200b-pretrain}

t=1s      Recovery Controller classifies: SINGLE_GPU_FAILURE.
          Action: replace entire node (GPU 3 is dead; the job needs all 8 GPUs
          on a node for TP=8, so a single dead GPU kills the whole node).

t=1s      Recovery Controller sends STOP signal to all workers in the job.
          Workers receive signal, flush current in-memory checkpoint
          to peer nodes (fast, already partially replicated from last
          async checkpoint 30 seconds ago).

t=2s      All workers stop. Recovery Controller requests a replacement
          node from the Scheduler.
          Constraint: same rack as Node 5 (to maintain PP group topology),
          same GPU type (H100), healthy GPUs.

t=3s      Scheduler finds Node 5-spare (standby node in same rack) and
          assigns it. Pod is created; image is pre-pulled (warm cache).

t=30s     New pod is running on the replacement node.
          Recovery Controller instructs it to pull the checkpoint:
          - First tries in-memory replica from Node 4 (same rack peer).
          - Checkpoint restore: 87.5 GB / 50 GB/s (RDMA) = 1.75 seconds.

t=33s     Checkpoint restored on replacement node.
          NCCL rendezvous re-runs with updated node topology.
          All workers re-initialize process groups.

t=45s     Data loaders resume from the saved position.
          Training resumes from step 15,023 (checkpoint was at step 15,020;
          3 steps of compute lost = ~135 seconds of training).

Total recovery time: ~45 seconds.
Total lost compute: ~3 steps = ~135 seconds of GPU time across 256 GPUs
  = 256 * 135 / 3600 = 9.6 GPU-hours wasted.
  At $3.50/GPU-hour: $33.60 cost of this failure.
```

### Scenario 2: Full Node Loss (Hardware Crash, All 8 GPUs Gone)

```
t=0s      Heartbeat from Node 12 stops. Node Agent is unreachable.

t=15s     Recovery Controller declares Node 12 dead (3 missed heartbeats
          at 5-second intervals).
          Affected: 8 GPUs, TP Group 12, PP Stage 1 in DP Replica 1.

t=16s     Recovery Controller sends PAUSE signal to all other workers.
          Workers enter a barrier wait (NCCL operations from the dead node
          were already timing out; this formalizes the pause).

t=17s     Scheduler allocates a replacement node.
          If no spare is available in the same rack:
          - Option A: use a node from an adjacent rack (degrades PP
            to cross-rack, but keeps the job running). Accept the
            ~2x latency on PP communication for this stage.
          - Option B: preempt a low-priority job in the same rack
            to free a node. (Preemption triggers a checkpoint for that
            job first.)

t=45s     Replacement node is ready. Checkpoint restored from in-memory
          replica (or S3 if in-memory replicas were on the dead node --
          this is why we replicate to nodes in DIFFERENT racks).

t=60s     NCCL rendezvous completes. Training resumes.

Total recovery time: ~60 seconds. Within the 5-minute MTTR target.
Lost compute: ~15 steps (60s / 4s per step for this job) = 60 seconds
  of training progress. Within the 10-minute-max-loss target.
```

### Scenario 3: Network Partition Between Racks

```
Scenario: IB spine switch failure partitions Racks 0-3 from Racks 4-7.
Affected job: 200B model spanning all 8 racks.

t=0s      NCCL AllReduce operations across the partition start timing out.
          Workers in Racks 0-3 can communicate with each other but not
          with Racks 4-7.

t=5s      NCCL watchdog on multiple nodes reports NCCL_TIMEOUT.
          (Before the 300-second NCCL timeout, the platform's own
          watchdog detects the partition by observing that heartbeats
          from Racks 4-7 have stopped arriving at the control plane
          and vice versa.)

t=10s     Recovery Controller detects: NETWORK_PARTITION.
          Heuristic: if >20% of nodes in a job are mutually unreachable,
          classify as partition, not individual node failures.

t=15s     Recovery Controller decides: cannot recover in place (the job
          needs cross-rack communication for DP AllReduce).
          Options:
          a) Wait for the partition to heal (if transient, <60s).
          b) Rearrange the job to fit within the reachable half.

t=60s     Partition not healed. Recovery Controller chooses option (b):
          - Save checkpoint on both sides of the partition (each side
            has its own DP replicas that can independently checkpoint).
          - Reschedule the job using only Racks 0-3 (160 GPUs instead
            of 256). Reduce DP degree from 8 to 4. Throughput drops ~50%.
          - Or: if the job has minGpuCount > 160, wait for partition heal.

t=120s    If rearranged: job resumes at reduced scale.
          When partition heals: scheduler scales the job back to full
          allocation using elastic scaling (add DP replicas from
          Racks 4-7 back).

Total time in reduced mode: depends on partition duration.
Platform SLA: for long-running jobs, >95% of wall-clock time training.
A 10-minute partition costs 10 minutes of 50% throughput, which is
equivalent to 5 minutes of lost training at full throughput.
```

### Scenario 4: Checkpoint Corruption

```
Scenario: S3 returns a corrupted checkpoint due to a bit flip during upload.
Job tries to restore after an unrelated failure.

t=0s      Worker fails, Recovery Controller initiates restore.

t=5s      Checkpoint Manager loads checkpoint from S3.
          SHA-256 verification fails: stored hash does not match
          downloaded data.

t=6s      Checkpoint Manager logs: CHECKPOINT_CORRUPTED, step=15000.
          Falls back to previous checkpoint: step=14500.

t=7s      SHA-256 verification passes for step 14500.
          Restore proceeds.

t=30s     Training resumes from step 14500 instead of 15000.
          Lost compute: 500 steps * 45 seconds = 6.25 hours of training.
          This is painful but bounded: checkpoint interval directly
          bounds maximum lost compute.

Mitigation layers:
  1. SHA-256 hash stored in checkpoint metadata DB at write time.
  2. S3 server-side integrity (Content-MD5 on upload).
  3. In-memory checkpoint replicas (checked first, not in S3 path).
  4. Periodic checkpoint validation job: downloads and verifies a
     random sample of stored checkpoints, alerts on any corruption.
```

### Scenario 5: Cloud Spot Instance Reclamation Wave

```
Scenario: Cloud provider reclaims 20% of spot instances in a region
  within a 2-minute window. 50 nodes (400 GPUs) affected across
  multiple jobs.

t=0s      Cloud provider sends 2-minute termination notices to 50 nodes.
          Node Agents receive SIGTERM and report SPOT_RECLAMATION to
          Recovery Controller.

t=1s      Recovery Controller receives 50 reclamation events.
          Groups them by affected job.
          For each affected job:
            - Triggers IMMEDIATE checkpoint (not waiting for next
              scheduled checkpoint).
            - Marks affected nodes as "draining" (no new work).

t=5s      Emergency checkpoints begin writing to local NVMe.
          D2H copy takes ~1 second. NVMe write takes ~5-10 seconds.
          S3 upload begins in background (may not complete before
          termination -- that is fine, in-memory replicas are the
          fast-path).

t=30s     Most emergency checkpoints are saved (NVMe + in-memory replicas
          on non-reclaimed nodes).

t=60s     Recovery Controller begins requesting replacement nodes for
          each affected job.
          Priority: critical jobs first, then high, then normal.
          Spot jobs (priority=preemptible) are NOT replaced -- they
          are requeued and wait for new spot capacity.

t=90s     Some replacement nodes allocated (on-demand if spot unavailable).
          Jobs begin restoring.

t=120s    Cloud terminates the 50 nodes. Any checkpoint data not yet
          uploaded to S3 is lost -- but in-memory replicas on surviving
          nodes still have it.

t=180s    Most critical/high-priority jobs have been restored and are
          training again. Some normal-priority jobs are still waiting
          for node allocation.

Total impact:
  - Critical jobs: ~3 min downtime, ~3 steps lost.
  - Normal jobs: ~5-10 min downtime.
  - Preemptible jobs: indefinite queue wait for new spot capacity.
  
Platform automatically:
  - Pages on-call if >10% of cluster is reclaimed at once.
  - Temporarily disables new spot instance allocation for affected
    instance types until stability is confirmed.
  - Adjusts fair-share scheduler to redistribute remaining capacity.
```

---

## 11. Cost Model

### GPU-Hour Cost Breakdown

```
=== On-Premise (Amortized) ===

H100 80GB SXM node (8 GPUs):
  Hardware cost: $300,000 (server + GPUs + NVLink)
  Amortization: 3 years
  Annual hardware cost: $100,000 per node
  Power + cooling: ~10 kW * $0.10/kWh * 8760 h = $8,760/year
  Rack space + networking: ~$5,000/year
  Total annual cost per node: ~$113,760
  Per GPU-hour: $113,760 / (8 GPUs * 8760 hours) = $1.62/GPU-hour

  But effective utilization is ~85%:
  Effective cost per useful GPU-hour: $1.62 / 0.85 = $1.91/GPU-hour

=== Cloud (On-Demand) ===

AWS p5.48xlarge (8x H100):
  On-demand: ~$98/hour = $12.25/GPU-hour
  1-year reserved: ~$65/hour = $8.13/GPU-hour
  Spot: ~$30-40/hour = $3.75-5.00/GPU-hour (variable, can be reclaimed)

=== Cost of Idle/Wasted Compute ===

Sources of waste:
  1. Scheduling delay: P50 30s, P99 5min
     Average: ~1 min per job start = 1 min * 256 GPUs = 256 GPU-min = 4.3 GPU-hours
     At $1.91/GPU-hr: $8.19 per job start (negligible for long jobs)

  2. Recovery time: ~60s per failure event
     Expected failures per 1000-GPU 7-day run:
       = 1000 GPUs * 168 hours / 2000 MTBF = 84 failures
     Recovery cost: 84 * 60s * 1000 GPUs / 3600 = 1,400 GPU-hours
     = $2,674 wasted on recovery during a 168,000 GPU-hour run
     = 0.83% overhead (acceptable)

  3. Pipeline bubble: 9.4% for PP=4, m=32
     Bubble cost per step: 9.4% of compute is idle
     For a 200B model, 7-day run: 168,000 GPU-hours * 9.4% = 15,792 GPU-hours
     = $30,163 -- this is the single largest source of waste.
     Reducing PP degree or increasing micro-batches is the top optimization.

  4. Gradient synchronization overhead: ~1.3% (well within target)
     Cost: 168,000 * 1.3% = 2,184 GPU-hours = $4,171

  5. Checkpoint overhead: ~0.006% (async, negligible)
```

### Checkpoint Storage Cost

```
S3 storage: $0.023/GB/month
S3 requests: $0.005 per 1000 PUT, $0.0004 per 1000 GET

Active checkpoint storage: 459 TB
Monthly cost: 459,000 GB * $0.023 = $10,557/month

30-day retention for completed jobs: ~12 PB
Monthly cost: 12,000,000 GB * $0.023 = $276,000/month

With incremental dedup (5x reduction): $55,200/month
With S3 Intelligent-Tiering for old checkpoints: ~$30,000/month

Key optimization levers:
  1. Aggressive retention policy (keep best + last 3 only): 3x reduction
  2. Incremental checkpoints (delta-only): 5x reduction
  3. LZ4 compression: 1.5x reduction
  4. Tiered storage (S3 Infrequent Access for >7 days): 2x cost reduction
  Combined: ~30x reduction from naive baseline.
```

### Per-Team Chargeback Report

```sql
SELECT
    team,
    project,
    count(*) AS num_jobs,
    sum(total_gpu_hours) AS total_gpu_hours,
    sum(training_gpu_hours) AS training_gpu_hours,
    sum(idle_gpu_hours) AS idle_gpu_hours,
    sum(total_gpu_hours * gpu_hour_rate) AS total_cost_usd,
    avg(idle_gpu_hours / total_gpu_hours * 100) AS avg_idle_pct,
    avg(avg_mfu * 100) AS avg_mfu_pct,
    sum(checkpoint_storage_gb) * 0.023 AS checkpoint_storage_cost_usd
FROM training_runs
    JOIN gpu_rates USING (gpu_type)
WHERE completed_at >= date_trunc('month', now())
GROUP BY team, project
ORDER BY total_cost_usd DESC;
```

---

## 12. Evolution Path

| Version | Scope | What Ships | What Is Deliberately Deferred |
|---|---|---|---|
| **v1** | Core training loop | Job submission API (YAML spec), PyTorch DDP/FSDP support, gang scheduling (Volcano), basic topology-aware placement (TP intra-node only), synchronous checkpointing to S3, simple heartbeat failure detection, manual restart on failure, Prometheus metrics collection, single GPU type support | No auto-parallelism, no pipeline parallelism, no elastic scaling, no async checkpoint, no in-memory checkpoint replication, no spot instance support, no experiment comparison UI |
| **v2** | Fault tolerance + parallelism | Automatic failure recovery (state machine), async distributed checkpointing with NVMe staging, pipeline parallelism support (1F1B schedule), auto-parallelism advisor (heuristic), in-memory checkpoint replication, NCCL hang detection, heterogeneous GPU types, experiment tracking with comparison views | No elastic scaling, no MoE/expert parallelism, no spot instances, no cost optimization recommendations |
| **v3** | Elasticity + efficiency | Elastic scaling for DP jobs, spot/preemptible instance support, MoE expert parallelism, checkpoint deduplication and compression, data pipeline streaming from S3 with NVMe cache, MFU dashboard, cost chargeback reports, fair-share multi-tenancy with preemption | No redundant computation (shadow replicas), no learned auto-parallelism, no cross-cloud scheduling |
| **v4** | Advanced optimization | Redundant computation for critical jobs, learned auto-parallelism (profile-guided), cross-region scheduling, predictive failure detection (GPU health trends), automatic checkpoint interval tuning (based on failure rate + training cost), model registry integration with lineage tracking | Fully autonomous operation without human oversight remains out of scope -- the platform surfaces recommendations and requires human approval for risky changes (e.g., reducing checkpoint frequency) |

**v1 is a legitimate stopping point for 3-6 months.** A team of 5-8 engineers can ship v1 in ~3 months and operate it for 50+ teams running DDP/FSDP jobs on up to a few hundred GPUs. The biggest manual burden in v1 is failure recovery (on-call restarts jobs from checkpoints) -- this is painful enough to be the forcing function for v2, but it works.

**v2 is the inflection point where the platform earns its keep.** Automatic recovery means a 256-GPU job that fails at 3 AM no longer pages anyone. This is the feature that makes teams trust the platform enough to run week-long jobs on it.

---

## 13. Trade-offs

| Decision | What We Chose | What We Gave Up | Why |
|---|---|---|---|
| **TP within node, PP across nodes** | Strict: TP groups never span nodes; PP groups never span racks (unless no choice) | Flexibility to put TP across 2 nodes with InfiniBand for models that need TP=16 | NVLink is 18x the bandwidth of InfiniBand. TP communication happens at every layer boundary (hundreds of times per step). Even a 2x slowdown on TP would devastate throughput. For TP>8, we use PP to shard across nodes instead. |
| **FSDP vs. Megatron-style TP+PP for medium models** | FSDP as default for models that fit in per-GPU memory with sharding (up to ~30B) | Peak throughput of hand-tuned Megatron parallelism | FSDP requires zero user code changes beyond wrapping the model. Megatron requires model-specific sharding code. For a multi-tenant platform serving 50 teams, developer experience dominates -- teams should not need to rewrite their model to use the platform. For models >30B where Megatron-style parallelism is needed, the auto-advisor handles the configuration. |
| **Async checkpointing with D2H copy** | Snapshot model state to CPU memory (non-blocking D2H), then write to NVMe/S3 in background | Guaranteed point-in-time consistency (the async snapshot captures state at a slightly different point than training continues from) | The inconsistency is bounded: the D2H copy takes <1 second; during that time, the GPU processes at most 1 micro-batch. On restore, training resumes from the checkpoint state, so the "extra" micro-batch is simply re-trained. The alternative (synchronous checkpoint) blocks all GPUs for 10-30 seconds, which at 256 GPUs = 2,560 GPU-seconds wasted per checkpoint. |
| **In-memory checkpoint replicas vs. fast S3 only** | Replicate last checkpoint to 2 peer nodes via RDMA | Memory overhead on peer nodes (~87 GB per replica), complexity of replica consistency | Recovery from in-memory replica: 1.75 seconds. Recovery from S3: 70 seconds. For a 256-GPU job at $3.50/GPU-hr, each second of recovery costs $0.25. The 68-second difference saves $17 per recovery event. At ~84 failures per week for a 1000-GPU job, that is $1,428/week saved. The memory cost (87 GB on 2 nodes out of 2 TB RAM each) is negligible. |
| **Gang scheduling (all-or-nothing)** | All pods for a job must be placed atomically | Cluster utilization during scheduling (GPUs held idle waiting for the full gang to be placeable) | A partially-placed training job is worse than an unplaced one: the placed pods waste GPUs doing nothing while waiting for the remaining pods. Gang scheduling avoids this by either placing all pods or none. The scheduling delay (P50 30s, P99 5min) is a one-time cost per job start, amortized over hours/days of training. |
| **Volcano as scheduler vs. custom scheduler** | Volcano (open-source gang scheduler for K8s) with custom scoring plugin | Full control over scheduling internals | Volcano is battle-tested for gang scheduling and has an active community. Our custom scoring plugin (topology-aware, health-aware, fair-share) runs within Volcano's extension points. Building a full scheduler from scratch would cost 6+ months of engineering and ongoing maintenance for functionality Volcano already provides. |
| **1F1B pipeline schedule vs. GPipe** | 1F1B (one forward, one backward) interleaved schedule | Simplicity of GPipe (all-forward then all-backward) | GPipe has a bubble ratio of `(PP-1)/PP` at the end of each micro-batch group -- for PP=4, that is 75% bubble on the last stage. 1F1B interleaves forward and backward passes so that the bubble is only at the start and end of the batch: `(PP-1)/m` where m = micro-batches. For PP=4 and m=32: GPipe bubble = 75% vs. 1F1B bubble = 9.4%. The throughput difference is enormous. |
| **Deterministic data ordering vs. truly random per-restart** | Deterministic shuffling (seed + epoch -> same permutation, fast-forward on resume) | Slightly better generalization from truly random ordering across restarts | Reproducibility is non-negotiable for debugging training divergence. If a model starts producing bad outputs at step 30,000, the team needs to re-run from step 29,000 with the exact same data order to isolate the cause. Non-deterministic data loading makes this impossible. |
| **Node-level replacement vs. GPU-level replacement** | Replace the entire node when any GPU fails | Possible to continue with 7/8 GPUs if the parallelism config allows it | TP=8 requires all 8 GPUs on a node. Even for DP-only jobs, a 7-GPU node creates a load imbalance (one worker processes less data, all others wait at the AllReduce barrier). The complexity of handling heterogeneous worker sizes is not worth the ~60 seconds saved by not reprovisioning. |
| **Heartbeat-based failure detection vs. NCCL-integrated** | Node Agent heartbeat (5s interval, 15s detection) augmented by NCCL timeout watchdog | Sub-second failure detection | 15-second detection + 45-second recovery = 60s total is within the 5-minute target. Sub-second detection would require invasive changes to NCCL (intercepting every collective operation), adding latency to the training hot path. The NCCL timeout watchdog (300s timeout) serves as a backstop for silent hangs that heartbeats cannot detect. |

### Parallelism Strategy Trade-offs (Detail)

```
Model size vs. strategy decision tree:

                    Model fits in 1 GPU memory?
                    ┌─── Yes ──── Use DDP (simplest, fastest)
                    │
                    No
                    │
                    Model fits with FSDP sharding?
                    ┌─── Yes ──── Use FSDP
                    │             (works up to ~30B params on H100 80GB
                    │              with 8-way sharding)
                    │
                    No
                    │
                    Single layer fits in 1 GPU?
                    ┌─── Yes ──── Use TP + PP (+ FSDP for DP dimension)
                    │             TP within node, PP across nodes
                    │
                    No ──────── Use TP + PP with larger TP degree
                               (requires models with more attention heads
                                than GPUs per node, or custom sharding)

FSDP vs. TP+PP for the ~7-30B sweet spot:

  FSDP advantages:
  - Zero code changes (wrap model in FSDP, done)
  - No pipeline bubble (pure data parallelism)
  - Scales to large GPU counts without topology constraints

  FSDP disadvantages:
  - 3x communication volume vs DDP (AllGather + ReduceScatter + AllGather)
  - Communication cannot fully overlap with compute for small models
  - AllGather of full layer params before each forward pass: latency
    proportional to layer size / network bandwidth

  TP+PP advantages:
  - TP communication is on NVLink (18x faster than IB)
  - PP has minimal communication (only activations at stage boundaries)
  - Better MFU for very large models (>100B)

  TP+PP disadvantages:
  - PP bubble wastes compute (9-15%)
  - Requires model-aware sharding (TP splits attention heads, FFN columns)
  - Topology constraints (TP must be intra-node, PP should be intra-rack)
  - More complex failure recovery (PP stage must be restored on a node
    in the correct position in the pipeline)

Rule of thumb:
  - Under 30B params and <128 GPUs: FSDP
  - Over 30B params or >128 GPUs: TP + PP + DP (or FSDP for the DP dimension)
  - MoE models: add Expert Parallelism regardless of size
```

---

## Appendix A: Dry-Run Validation

Before allocating any GPUs, the platform validates the job spec:

```python
class DryRunValidator:
    def validate(self, spec: TrainingJobSpec) -> list[ValidationError]:
        errors = []

        # 1. GPU memory feasibility
        estimated_mem = self.estimate_memory(spec)
        if estimated_mem > spec.hardware.gpu_memory_gb * 0.95:
            errors.append(ValidationError(
                "INSUFFICIENT_GPU_MEMORY",
                f"Estimated {estimated_mem:.1f} GB per GPU, "
                f"but {spec.hardware.gpu_memory_gb} GB available. "
                f"Consider increasing TP degree or enabling activation checkpointing."
            ))

        # 2. Parallelism configuration consistency
        if spec.parallelism.strategy == "manual":
            tp = spec.parallelism.tensor_parallel
            pp = spec.parallelism.pipeline_parallel
            dp = spec.parallelism.data_parallel
            if tp * pp * dp != spec.hardware.gpu_count:
                errors.append(ValidationError(
                    "PARALLELISM_MISMATCH",
                    f"TP({tp}) * PP({pp}) * DP({dp}) = {tp*pp*dp} "
                    f"!= requested GPU count {spec.hardware.gpu_count}"
                ))
            if tp > 8:
                errors.append(ValidationError(
                    "TP_EXCEEDS_NODE",
                    f"TP degree {tp} exceeds GPUs per node (8). "
                    f"TP must fit within a single NVLink domain."
                ))
            if spec.parallelism.num_layers % pp != 0:
                errors.append(ValidationError(
                    "PP_LAYER_MISMATCH",
                    f"{spec.parallelism.num_layers} layers not divisible "
                    f"by PP degree {pp}."
                ))

        # 3. Dataset accessibility
        if not self.storage.exists(spec.dataset.source):
            errors.append(ValidationError(
                "DATASET_NOT_FOUND",
                f"Dataset not found at {spec.dataset.source}"
            ))

        # 4. Image availability
        if not self.registry.image_exists(spec.image):
            errors.append(ValidationError(
                "IMAGE_NOT_FOUND",
                f"Container image {spec.image} not found in registry"
            ))

        # 5. Quota check
        team_quota = self.scheduler.get_team_quota(spec.metadata.team)
        if spec.hardware.gpu_count > team_quota.available_gpus:
            errors.append(ValidationError(
                "QUOTA_EXCEEDED",
                f"Requested {spec.hardware.gpu_count} GPUs but team "
                f"{spec.metadata.team} has {team_quota.available_gpus} available."
            ))

        return errors
```

## Appendix B: Platform Integration with Training Frameworks

The platform does not modify user training code. Instead, it provides a thin callback library that hooks into PyTorch's training loop:

```python
# platform_callbacks.py -- installed in the training container image

import torch
import torch.distributed as dist
from torch.distributed.checkpoint import save as dcp_save, load as dcp_load

class PlatformCallback:
    """
    Injected into the training loop via environment variable:
      PLATFORM_CALLBACK=platform_callbacks.PlatformCallback

    The user's training script calls these at appropriate points,
    or the platform's wrapper script calls them automatically via
    PyTorch's training hooks.
    """
    def __init__(self):
        self.metrics_client = MetricsClient()  # gRPC to control plane
        self.checkpoint_manager = CheckpointManager()
        self.health_reporter = HealthReporter()

    def on_train_step_end(self, step: int, metrics: dict):
        """Called after each training step."""
        # Report metrics (non-blocking)
        self.metrics_client.report(step=step, metrics=metrics)

        # Check if checkpoint is due
        if self.checkpoint_manager.should_checkpoint(step):
            self.checkpoint_manager.async_checkpoint(step, self.model, self.optimizer)

        # Report health (piggyback on step completion)
        self.health_reporter.heartbeat(step=step)

    def on_train_begin(self, model, optimizer, dataloader):
        """Called at training start. Restores from checkpoint if resuming."""
        self.model = model
        self.optimizer = optimizer
        resume_step = self.checkpoint_manager.maybe_restore(model, optimizer, dataloader)
        return resume_step

    def on_failure_detected(self):
        """Called by the platform when a peer failure is detected."""
        # Trigger an emergency checkpoint before the recovery process
        # replaces the failed node and restarts all workers.
        self.checkpoint_manager.emergency_checkpoint(
            self.model, self.optimizer, to_memory=True
        )
```

---
