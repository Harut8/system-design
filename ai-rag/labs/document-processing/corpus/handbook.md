# Ingestion Service Handbook

The ingestion service turns source documents into indexed chunks. This handbook covers
operation, not design; the design rationale lives in the architecture decision records.

## 1. Running the service

### 1.1 Local development

The service reads from a local directory when `INGEST_SOURCE=file://`. No credentials
are required in this mode, which makes it the right way to reproduce a parsing bug
reported from production.

```python
from ingest import Pipeline

pipeline = Pipeline.from_env()
result = pipeline.run(source="file://./samples", dry_run=True)
print(result.summary())
```

Note that `dry_run=True` still parses and chunks; it only skips the embed and index
stages. That is deliberate — the stages you want to debug are the cheap ones, and
making them free to re-run is the whole reason the pipeline persists intermediate
artifacts.

### 1.2 Production

Production runs on the batch cluster. The unit of work is a document, not a file, so a
multi-document archive is expanded during acquisition rather than during parsing.

## 2. Operational limits

### 2.1 Document size

Documents above 200 MB are routed to the large-document queue, which runs with a
higher memory limit and a lower concurrency. Nothing else about their processing
differs.

### 2.2 Rate limits

The embedding provider enforces a tokens-per-minute quota shared across all tenants.
When the quota is exhausted the pipeline backs off and retries; it does not drop work.
Sustained backoff for more than fifteen minutes raises an alert.

## 3. Failure handling

### 3.1 Quarantine

A document that fails a parse gate is written to the quarantine bucket with the gate's
verdict attached. Quarantine is not a dead-letter queue: documents there are expected
to be re-driven after the parser is fixed, and the verdict is what tells you which
parser change to make.

### 3.2 Poison documents

A document that crashes the parser twice is marked poison and skipped. The count of
poison documents is a tracked metric, and it should be zero. A non-zero steady state
means the parser has a bug nobody has prioritised.
