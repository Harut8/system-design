<div align="center">

# System Design Notes

**System design from the internals up.**<br>
The layer below the interview guides: how CPython, storage engines, Kubernetes and
telemetry pipelines actually work, plus labs where you predict, break, and measure.

[![Read online](https://img.shields.io/badge/read-online-4051b5?style=flat-square)](https://harut8.github.io/system-design/)
[![Stars](https://img.shields.io/github/stars/Harut8/system-design?style=flat-square)](https://github.com/Harut8/system-design/stargazers)
[![License: CC BY-SA 4.0](https://img.shields.io/badge/notes-CC%20BY--SA%204.0-lightgrey?style=flat-square)](LICENSE)
[![Code: MIT](https://img.shields.io/badge/code-MIT-lightgrey?style=flat-square)](LICENSE-CODE)

**📖 [harut8.github.io/system-design](https://harut8.github.io/system-design/)**: searchable, dark mode, works on mobile

</div>

---

Most system design material stops at the whiteboard: "put a cache here, shard there."
These notes go one layer down, to the mechanisms that decide whether that box diagram
survives production. Why an `UPDATE` in Postgres is really an `INSERT`. What the kubelet does
between `kubectl apply` and a running container. Where the GIL is actually dropped. Why
your p99 alert pages at 3 a.m. for nothing.

- **220+ chapters across 8 tracks**, from CPU caches to RAG evaluation, written from primary sources (papers, source code, RFCs)
- **Labs for every track**: predict a number, measure it on your laptop with Docker, then break something on purpose
- **21 design problems** with full solutions, and **reference implementations at four scales** (10k → 100k → 1m → 10m)
- Spaced-repetition questions at the end of each lab section (redo after a day, a week, a month)

## Start here

| If you want to… | Read in this order |
| --- | --- |
| **Prepare for a system design interview** | [Distributed systems primitives](distributed-systems/00-primitives-and-system-models.md) → [Replication](distributed-systems/04-replication-and-consistency.md) → [Caching](distributed-systems/08-caching-strategies-and-patterns.md) → [Sharding](distributed-systems/10-sharding-and-consistent-hashing.md) → then work the [design problems](tasks/README.md) |
| **Understand databases for real** | [Mental model](databases/MENTAL_MODEL.md) → [Storage engines](databases/01-storage-engine-fundamentals.md) → [Transactions](databases/05-transactions-and-concurrency.md) → [LSM trees](databases/13-lsm-trees-and-compaction.md) → [WAL](databases/14-write-ahead-log-internals.md) |
| **Go from Kubernetes user to Kubernetes operator** | [Linux primitives](kubernetes/00-linux-primitives-for-containers.md) → [etcd](kubernetes/04-etcd-internals.md) → [API server](kubernetes/05-kube-apiserver-internals.md) → [Scheduler](kubernetes/09-kube-scheduler-internals.md) → [Kubelet](kubernetes/10-kubelet-internals.md) |
| **Own reliability / on-call** | [SRE mental models](sre-observability/00-mental-models.md) → [OpenTelemetry](sre-observability/02-opentelemetry-deep-dive.md) → [Alerting](sre-observability/12-alerting.md) → [SLO engineering](sre-observability/13-slo-engineering.md) → [On-call](sre-observability/14-on-call.md) |
| **Run GPU / LLM infrastructure** | [GPU mental models](gpu-observability/00-mental-models.md) → [DCGM exporter](gpu-observability/02-dcgm-exporter-deep-dive.md) → [Utilization efficiency](gpu-observability/05-gpu-allocation-and-utilization-efficiency.md) → [LLM inference](gpu-observability/14-llm-inference-observability.md) |
| **Build RAG that holds up in production** | [RAG mental models](ai-rag/00-mental-models.md) → [Chunking](ai-rag/02-chunking-and-document-processing.md) → [Hybrid retrieval](ai-rag/04-retrieval-hybrid-and-reranking.md) → [Evaluation](ai-rag/08-evaluation-methodology.md) |
| **Know what your Python actually does** | [CPU execution model](python-mastery/00-cpu-execution-model.md) → [Refcounting](python-mastery/15-refcounting-and-ownership.md) → [Eval loop](python-mastery/20-eval-loop.md) → [The GIL](python-mastery/24-the-gil.md) → [Free-threading](python-mastery/26-free-threading.md) |

## Contents

| Track | What it covers | Chapters |
| --- | --- | --- |
| [`python-mastery/`](python-mastery/README.md) | CPU execution model, caches, virtual memory, allocators, syscalls, then CPython internals: refcounting, eval loop, GC, the GIL, free-threading, asyncio | 30 |
| [`databases/`](databases/MENTAL_MODEL.md) | Storage engines, encoding formats, access methods, query engines, transactions, B-trees and LSM-trees, WAL, vector search, data lake and lakehouse | 25 |
| [`distributed-systems/`](distributed-systems/README.md) | System models and CAP/PACELC, replication and consistency, consensus, sagas/outbox/idempotency, sharding, streaming, resilience and load control, multi-region, production debugging, disaster recovery | 17 |
| [`kubernetes/`](kubernetes/ROADMAP.md) | Linux primitives, etcd, API server, scheduler and kubelet internals, CNI/Cilium/eBPF, CSI, operators, multi-tenancy, supply-chain security | 46 |
| [`k8s-learn/`](k8s-learn/README.md) | Hands-on Kubernetes task sheets with manifests | 14 |
| [`sre-observability/`](sre-observability/ROADMAP.md) | OpenTelemetry, instrumentation, telemetry storage and query layers, SLO engineering, on-call, cardinality and cost | 47 |
| [`gpu-observability/`](gpu-observability/README.md) | DCGM internals, GPU cluster telemetry, utilization efficiency, failure detection, LLM inference and training observability | 23 |
| [`ai-rag/`](ai-rag/README.md) | Embeddings, chunking, vector indexes, hybrid retrieval and reranking, evaluation methodology, agents and orchestration, plus labs | 21 + labs |
| [`tasks/`](tasks/README.md) · [`solutions/`](solutions/README.md) | 21 interview-style design problems, Mid to Staff level, each with a full solution and an interview kit | 21 |
| [`implementation/`](implementation/) | Runnable reference implementations of the same design at 10k, 100k, 1m and 10m scale (FastAPI, Postgres, Redis, Docker Compose) | 4 designs |
| [`primitives/`](primitives/README.md) | The reusable decisions extracted from the worked solutions, so the next design costs less than the last | — |
| [`SYSTEM-DESIGN-GUIDE.md`](SYSTEM-DESIGN-GUIDE.md) | Cross-cutting reference tying the tracks together | — |

### Labs

Every track has a `LABS.md` of learn-by-doing tasks, one section per chapter. Each task asks you to
predict a result before measuring it, break something on purpose, and end with a number you can check.
Each section closes with three closed-book questions to redo after a day, a week and a month, and each
sheet ends with multi-day capstones. They run on a laptop with Docker; anything paid is marked optional.

[databases](databases/LABS.md) · [distributed-systems](distributed-systems/LABS.md) ·
[python-mastery](python-mastery/LABS.md) · [kubernetes](kubernetes/LABS.md) (with [`k8s-learn/`](k8s-learn/README.md) for the API basics) ·
[sre-observability](sre-observability/LABS.md) · [ai-rag](ai-rag/LABS.md) · [gpu-observability](gpu-observability/tasks.md)

## Building the site locally

The notes are plain Markdown and readable straight from the repository. The
site build is only needed to preview the published HTML.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-docs.txt

./scripts/stage-docs.sh   # collect the topic dirs into .docs-build/
mkdocs serve              # http://127.0.0.1:8000
```

`scripts/stage-docs.sh` copies the tracked Markdown into a single tree so
MkDocs has one `docs_dir`, keeping the relative layout intact so the
cross-links between tracks still resolve. Re-run it after adding or renaming
files. Publishing happens automatically on push to `main` via
[`.github/workflows/pages.yml`](.github/workflows/pages.yml).

## Contributing

Found a wrong number, a broken lab, or an explanation that doesn't land? Open an issue or a pull
request. See [CONTRIBUTING.md](CONTRIBUTING.md). Fixes with a source (paper, source line, docs
link) are merged fastest.

If these notes saved you an afternoon, a ⭐ helps other engineers find them.

## License

The notes are licensed under [CC BY-SA 4.0](LICENSE): share and adapt them with attribution,
under the same license. Code (`implementation/`, `ai-rag/labs/`, and other source files) is
[MIT](LICENSE-CODE).
