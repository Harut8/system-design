# System Design Notes

**📖 Read online: [harut8.github.io/system-design](https://harut8.github.io/system-design/)**

Working notes on systems engineering, written while learning each topic from
primary sources. They run from CPU and OS primitives up through storage
engines, distributed systems, Kubernetes, observability, and
retrieval-augmented generation. Most chapters end with lab exercises, and
several have runnable code alongside them.

## Contents

| Track | What it covers | Chapters |
| --- | --- | --- |
| [`python-mastery/`](python-mastery/README.md) | CPU execution model, caches, virtual memory, allocators, syscalls, then CPython internals: refcounting, eval loop, GC, the GIL, free-threading, asyncio | 30 |
| [`databases/`](databases/MENTAL_MODEL.md) | Storage engines, encoding formats, access methods, query engines, transactions, B-trees and LSM-trees, WAL, vector search | 24 |
| [`distributed-systems/`](distributed-systems/README.md) | Consensus, replication, failure detection, coordination | roadmap |
| [`kubernetes/`](kubernetes/ROADMAP.md) | Linux primitives, etcd, API server, scheduler and kubelet internals, CNI/Cilium/eBPF, CSI, operators, multi-tenancy, supply-chain security | 46 |
| [`k8s-learn/`](k8s-learn/README.md) | Hands-on Kubernetes task sheets with manifests | 14 |
| [`sre-observability/`](sre-observability/ROADMAP.md) | OpenTelemetry, instrumentation, telemetry storage and query layers, SLO engineering, on-call, cardinality and cost | 47 |
| [`gpu-observability/`](gpu-observability/README.md) | DCGM internals, GPU cluster telemetry, utilization efficiency, failure detection, LLM inference and training observability | 23 |
| [`ai-rag/`](ai-rag/README.md) | Embeddings, chunking, vector indexes, hybrid retrieval and reranking, evaluation methodology, plus labs | 9 + labs |
| [`tasks/`](tasks/) · [`solutions/`](solutions/) · [`implementation/`](implementation/) | Design problems at four scale tiers (10k → 10m), worked solutions, and reference implementations | — |
| [`primitives/`](primitives/README.md) | The reusable decisions extracted out of the worked solutions — one sheet per design, so the next design costs less than the last | 1 |
| [`SYSTEM-DESIGN-GUIDE.md`](SYSTEM-DESIGN-GUIDE.md) | Cross-cutting reference tying the tracks together | — |

## Building the site locally

The notes are plain Markdown and readable straight from the repository — the
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

These are personal study notes, so they carry my own emphases and the
occasional half-finished section. Corrections are genuinely welcome — open an
issue or a pull request.
