## System Design Task: Parallel ML Training Platform

### Problem Statement

Design a **distributed machine learning training platform** that enables data
scientists and ML engineers to train models across **hundreds to thousands of
GPUs/TPUs** with minimal configuration overhead, while maximizing hardware
utilization and minimizing time-to-convergence.

Today, ML teams spend more time fighting infrastructure — provisioning clusters,
debugging NCCL hangs, recovering from preempted spot instances, tuning
distributed strategies, and manually checkpointing — than they do on actual
model development. A single failed node in a 256-GPU training run can waste
hours of compute. Gradient synchronization stalls silently degrade throughput.
Checkpoints written to shared storage compete with training I/O. Teams run
separate ad-hoc scripts for data parallelism, model parallelism, and pipeline
parallelism, each with its own failure modes.

The platform fixes this by providing a **managed, fault-tolerant distributed
training service** where users submit a training job (model code, dataset
reference, training config) and the platform handles **cluster orchestration,
parallelism strategy selection, elastic scaling, fault recovery, checkpoint
management, and experiment tracking** — all while keeping GPU utilization above
85% at steady state.

This is a shared platform serving 50+ ML teams with heterogeneous workloads:
from fine-tuning a 7B parameter model on 8 GPUs to pre-training a 200B+
parameter foundation model on 2,048 GPUs for weeks. The platform must handle
both efficiently without requiring teams to become distributed systems experts.

---

### Functional Requirements

1. **Job Submission and Configuration**

   * Users submit a training job with: model code (Git repo + entrypoint),
     dataset reference (S3/GCS path or internal data catalog ID), hardware
     requirements (GPU type, minimum/maximum count, memory per node), and a
     training config (hyperparameters, parallelism hints, max runtime).
   * Support for common frameworks: **PyTorch (primary), JAX, and
     TensorFlow** — the platform's distributed primitives (communication
     backends, checkpoint hooks, data loaders) must integrate cleanly with
     each, not require a proprietary wrapper that breaks when the framework
     updates.
   * A **declarative job spec** (YAML/JSON) that separates concerns: model
     code knows nothing about cluster topology; the platform injects
     distributed configuration (world size, rank, master addr) at launch.
   * Job priorities (critical, high, normal, low, preemptible) that feed into
     the scheduler's allocation decisions.
   * Dry-run validation: catch misconfigurations (incompatible parallelism +
     model size, insufficient memory, missing dataset) before any GPU is
     allocated.

2. **Parallelism Strategy Engine**

   * **Data Parallelism (DP / DDP / FSDP)**: replicate the model across
     workers, shard data, and synchronize gradients — support both
     synchronized SGD and gradient accumulation for large effective batch
     sizes.
   * **Tensor Parallelism (TP)**: split individual layers (attention heads,
     FFN columns) across GPUs within a node for models that don't fit in a
     single GPU's memory.
   * **Pipeline Parallelism (PP)**: split model layers across stages, with
     micro-batching (GPipe) or interleaved scheduling (1F1B) to minimize
     pipeline bubble.
   * **Fully Sharded Data Parallelism (FSDP / ZeRO Stage 3)**: shard model
     parameters, gradients, and optimizer states across the data-parallel
     group to reduce per-GPU memory footprint.
   * **Expert Parallelism**: for Mixture-of-Experts (MoE) architectures,
     route tokens to expert replicas across nodes with All-to-All
     communication.
   * **Hybrid parallelism**: combine TP + PP + DP (the standard layout for
     large model training — TP within a node, PP across nodes within a rack,
     DP across racks) and select the configuration automatically based on
     model size, cluster topology, and interconnect bandwidth.
   * An **auto-parallelism advisor** that, given a model graph and cluster
     description, recommends a parallelism plan (DP degree, TP degree, PP
     stages, micro-batch size) to maximize throughput, with escape hatches
     for expert override.

3. **Cluster Orchestration and Scheduling**

   * **Gang scheduling**: a training job's workers must all be scheduled
     atomically — a half-placed job wastes GPUs waiting for the rest.
   * **Topology-aware placement**: co-locate TP groups on the same node
     (NVLink), PP stages on nodes within the same rack (high-bandwidth
     InfiniBand), and DP replicas across racks, respecting the network
     hierarchy (NVLink > NVSwitch > InfiniBand > Ethernet).
   * **Multi-tenant GPU cluster management**: support both dedicated GPU pools
     per team and shared pools with fair-share scheduling across teams.
   * **Elastic scaling**: expand or contract the number of workers mid-training
     (for data-parallel jobs) without restarting the entire job, recalculating
     the learning rate schedule accordingly.
   * **Spot/preemptible instance support**: seamlessly migrate or restart
     workers when cloud instances are reclaimed, with automatic checkpoint
     restore.
   * **Heterogeneous hardware**: schedule across mixed GPU types (A100 40GB,
     A100 80GB, H100, H200) in the same cluster, with placement constraints
     ensuring homogeneous GPUs within a single training job.

4. **Fault Tolerance and Recovery**

   * **Automatic failure detection**: detect node failures (heartbeat timeout),
     GPU errors (ECC errors, thermal throttling, Xid errors), NCCL hangs
     (communication timeout), and training divergence (loss NaN/explosion)
     within seconds.
   * **Transparent recovery**: on node failure, automatically replace the
     failed worker, restore from the latest checkpoint, and resume training
     with minimal lost compute — the training script should not need
     failure-handling code.
   * **Asynchronous distributed checkpointing**: write checkpoints to durable
     storage without blocking training, using background CPU threads and
     staging to local NVMe before uploading to object storage.
   * **Checkpoint deduplication and compression**: for FSDP/ZeRO-sharded
     checkpoints, consolidate shards and deduplicate unchanged parameters
     across incremental checkpoints.
   * **In-memory checkpoint replication**: maintain a recent checkpoint
     replicated across surviving nodes for fast recovery without hitting
     object storage.
   * **Redundant computation**: for critical long-running jobs, optionally run
     shadow replicas of each pipeline stage, switching to the backup on
     failure without any checkpoint restore latency.

5. **Data Pipeline**

   * **Distributed data loading**: shard the dataset across workers with
     deterministic, resumable shuffling — after a restart, replay exactly
     the same data order (given the same seed) to guarantee reproducibility.
   * **Streaming from object storage**: train on datasets too large to fit on
     local disk (100TB+ datasets) by streaming directly from S3/GCS with
     multi-level caching (local NVMe → shared NFS → object storage).
   * **Online preprocessing**: apply tokenization, augmentation, and
     formatting on-the-fly with dedicated CPU workers, pipelining with GPU
     training to eliminate data stalls.
   * **Dataset versioning**: every training job records the exact dataset
     version (commit hash or manifest) used, for reproducibility.
   * **Mixed-precision data loading**: support bf16/fp16 data formats to
     reduce I/O bandwidth requirements.

6. **Experiment Tracking and Observability**

   * **Real-time training metrics**: loss, gradient norm, learning rate,
     throughput (tokens/sec or samples/sec), GPU utilization, memory usage,
     communication overhead — streamed live to a dashboard.
   * **Experiment management**: track every training run with its config,
     hyperparameters, metrics, checkpoints, dataset version, and code
     version, with comparison views across experiments.
   * **GPU-level observability**: per-GPU utilization, memory high-water mark,
     NVLink/InfiniBand bandwidth utilization, thermal state, and ECC error
     counts, aggregated and alertable.
   * **Distributed profiling**: trace the compute/communication overlap, find
     pipeline bubbles, identify straggler GPUs, and measure the actual
     achieved FLOP/s vs. theoretical peak (MFU — Model FLOP/s Utilization).
   * **Cost tracking**: per-job and per-team GPU-hour and dollar cost,
     including idle time and failed runs, for chargeback.

7. **Model and Artifact Management**

   * **Checkpoint lifecycle**: automatic checkpoint retention policy (keep
     last N, keep best-by-metric, keep every Nth epoch), with promotion of
     checkpoints to the model registry on user action.
   * **Model registry integration**: trained models (final or intermediate
     checkpoints) can be published to an internal model registry with
     metadata (training config, metrics, lineage).
   * **Artifact storage**: training logs, TensorBoard events, profiling
     traces, and evaluation results stored and linked to the experiment.

---

### Non-Functional Requirements

1. **Scale**

   * Cluster size: **up to 4,096 GPUs** in a single training job (large
     foundation model training).
   * Total platform capacity: **10,000+ GPUs** across all jobs and teams.
   * Concurrent training jobs: **200+**, from single-GPU fine-tuning to
     multi-thousand-GPU pre-training.
   * Dataset sizes: up to **100 TB** per training job, streamed from object
     storage.

2. **Performance / Efficiency**

   * **GPU utilization ≥ 85%** at steady state for well-configured jobs
     (excluding startup/checkpoint overhead).
   * **Model FLOP/s Utilization (MFU) ≥ 40%** for large model training (this
     is the real metric — raw GPU utilization can be high while actual
     compute is stalled on communication).
   * Checkpoint overhead: **≤ 2%** of total training time for periodic
     checkpoints (async checkpoint must not block the training loop).
   * Pipeline bubble ratio: **≤ 15%** for pipeline-parallel training with ≥ 8
     micro-batches per batch.
   * Gradient synchronization overhead: **≤ 10%** of iteration time for
     data-parallel training within a rack (InfiniBand interconnect).

3. **Fault Tolerance**

   * **Mean time to recover (MTTR)** from a single node failure: **≤ 5
     minutes** (including detection, replacement, checkpoint restore, and
     training resumption).
   * A single node failure in a 1,000-GPU job must not lose more than **10
     minutes** of training progress.
   * The platform must tolerate **simultaneous failure of up to 5% of nodes**
     in the cluster without manual intervention.

4. **Availability**

   * Platform control plane (job submission, scheduling, monitoring):
     **99.95%**.
   * Training job uptime (fraction of wall-clock time a submitted job is
     actually training, not waiting/recovering): **≥ 95%** for long-running
     jobs (> 24 hours), measured over the job's lifetime.

5. **Latency**

   * Job scheduling latency (submission to first GPU allocated): P50 ≤ **30
     seconds**, P99 ≤ **5 minutes** (excluding queue wait for capacity).
   * Failure detection to recovery initiation: **≤ 30 seconds**.

6. **Security**

   * Tenant isolation: one team's training job must not access another team's
     data, model weights, or GPU memory.
   * Secrets (API keys, data credentials) injected securely, never logged or
     checkpointed.
   * Network isolation between training jobs (no cross-job NCCL traffic).

---

### Constraints and Assumptions

* The cluster runs on a mix of on-premise GPU nodes (InfiniBand interconnect)
  and cloud GPU instances (EFA on AWS, GPUDirect on GCP), with the platform
  abstracting the difference.
* GPU hardware is heterogeneous across the fleet but homogeneous within a
  single training job.
* Training frameworks (PyTorch, JAX) provide the distributed primitives
  (NCCL, XLA); the platform orchestrates around them rather than replacing
  them.
* Assume a Kubernetes-based orchestration layer (with GPU operator and
  network operator) exists; you are designing the ML-specific platform
  layer on top.
* Not in scope: model serving / inference. The platform's responsibility ends
  when a trained model checkpoint is saved and optionally registered.
* Not in scope: hyperparameter search / AutoML. However, the platform should
  support running multiple independent trials as separate jobs and provide
  the metrics API that an external HPO orchestrator (like Optuna or Ray Tune)
  would call.

---

### What You Should Deliver

1. Requirement clarification and explicit assumptions.
2. High-level architecture: every major component, data flows, and the split
   between control plane and data plane.
3. Parallelism strategy engine: how the platform selects and configures
   hybrid parallelism for a given job, with concrete examples for a 7B and
   200B model.
4. Cluster scheduler design: gang scheduling, topology-aware placement, and
   fair-share multi-tenancy.
5. Fault tolerance design: failure detection, checkpoint management, and
   recovery orchestration, with state machines.
6. Data pipeline architecture: distributed data loading, caching hierarchy,
   and streaming from object storage.
7. Experiment tracking and observability: what you measure, how you collect
   it, and what you'd alert on.
8. Network architecture: NCCL topology, InfiniBand/NVLink layout, and how
   the platform configures communication groups.
9. Capacity estimates with the arithmetic shown, not just the answer — GPU
   memory budgets, communication bandwidth requirements, storage throughput,
   checkpoint sizes.
10. Failure walkthroughs for at least: a single GPU failure mid-training,
    a full node loss, a network partition between racks, a checkpoint
    corruption, and a cloud spot instance reclamation wave.
11. Cost model: GPU-hour cost, idle waste, checkpoint storage cost, and
    the levers to optimize each.
12. Evolution path: what ships in v1 vs. what is deferred and why.
13. Trade-offs explicitly called out — especially parallelism strategy
    trade-offs (TP vs. PP vs. FSDP for different model sizes) and
    consistency vs. throughput in gradient synchronization.

---

### Expectations

* **Do the arithmetic.** GPU memory breakdown for a 70B model (parameters,
  gradients, optimizer states, activations, KV cache), communication volume
  per iteration, and checkpoint sizes should be concrete numbers.
* **Name concrete mechanisms** — NCCL AllReduce vs. Ring AllReduce, 1F1B
  pipeline schedule vs. GPipe, FSDP ShardingStrategy, async checkpoint with
  `torch.distributed.checkpoint` — and say what each buys and costs.
* **Show the topology.** Draw how GPUs, nodes, and racks are connected and
  how parallelism dimensions map to the physical topology.
* **Failure recovery must be automatic, not aspirational.** Show the exact
  sequence from failure detection to training resumption.
* Prefer a design a mid-sized ML platform team (5-8 engineers) can operate
  over one that requires a distributed systems PhD to debug at 3 a.m.
* Assume this platform will be used for workloads nobody has planned yet —
  the parallelism framework and scheduler must be extensible.

---
