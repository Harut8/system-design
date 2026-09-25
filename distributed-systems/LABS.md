# Distributed Systems — Labs

A learn-by-doing task sheet for the 17 chapters of this track. The chapters are the theory. Here
you reproduce each failure on your own laptop, measure it, fix it, and measure again. Every task
ends in a number, an output, or a failing-then-passing test.

- **Closed book first.** Answer each chapter's checkpoint from memory before you reread anything.
- **Predict before you run.** Write the prediction down, then the measurement, then the reason they differ.
- **Write results down.** A number you did not record is a number you did not get.
- **Space the review.** Redo the checkpoints after 1 day, 1 week and 1 month.

## How to use this sheet

- Work in the README's suggested order: 00 → 04 → 29 → 03, then 06 → 10 → 08 → 07 → 22 (and 23, 17),
  then 33 → 34 → 35 → 37 → 38 → 36. Do the Core tasks of a chapter first; come back for Stretch.
- Tick each `- [ ]` box when its **Verify** line holds, not when the code runs.
- Keep one `lab-notebook.md` in your lab folder. Per task: date, prediction, measured result, the gap
  between them and why, and the exact command or commit that produced the number.
- Where a chapter already has a sandbox section, the sheet says "also do §N". Those experiments are
  not repeated here; the tasks below are different.
- Spacing: checkpoint questions after 1 day, 1 week, 1 month. If you miss one, redo that chapter's
  first Core task, not the reading.
- The capstones at the end combine several chapters. Start one after you finish 00–08.

## Setup

| Environment | Cost | Used by |
|---|---|---|
| Laptop with Docker + Compose v2, 8 GB RAM free for the stack | Free | all chapters |
| Python 3.12 venv (asyncio, `psycopg`, `confluent-kafka`, `redis`, `httpx`, FastAPI) | Free | all chapters |
| Shared compose stack below: Postgres 17 primary + 2 streaming replicas, Kafka 3-broker KRaft, Redis 7, 3-node etcd, toxiproxy | Free | 00, 03, 04, 06, 07, 08, 22, 29, 33, 34, 36, 37, 38 |
| Jaeger all-in-one + OpenTelemetry SDK (compose profile `tracing`) | Free | 37, capstone B |
| Go 1.22+ (MIT 6.5840 Raft labs, Porcupine) | Free, optional | 00, 03, capstone A |
| Java 17 + Flink 1.20 standalone or PyFlink | Free, optional | 22 |
| Java 17 + PySpark in local mode | Free, optional | 23 |
| `prom/prometheus` image (for `promtool test rules`) | Free | 35 |
| Any cloud account | Never required | none |

Python packages (one venv for the whole track):

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install "psycopg[binary,pool]" confluent-kafka "redis>=5" "httpx[http2]" fastapi uvicorn hypercorn \
    grpcio numpy opentelemetry-distro opentelemetry-exporter-otlp
opentelemetry-bootstrap -a install      # instrumentations for the libraries above
```

Create a lab folder (`~/dslab`) with three files. **`compose.yaml`:**

```yaml
name: dslab
x-kafka: &kafka
  image: apache/kafka:4.0.0
x-kafka-env: &kafka-env
  KAFKA_PROCESS_ROLES: broker,controller
  KAFKA_CONTROLLER_QUORUM_VOTERS: 1@kafka1:9093,2@kafka2:9093,3@kafka3:9093
  KAFKA_CONTROLLER_LISTENER_NAMES: CONTROLLER
  KAFKA_LISTENERS: PLAINTEXT://:29092,CONTROLLER://:9093,EXTERNAL://:9092
  KAFKA_LISTENER_SECURITY_PROTOCOL_MAP: CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,EXTERNAL:PLAINTEXT
  KAFKA_INTER_BROKER_LISTENER_NAME: PLAINTEXT
  CLUSTER_ID: 4L6g3nShT-eMCtK--X86sw
  KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 3
  KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR: 3
  KAFKA_TRANSACTION_STATE_LOG_MIN_ISR: 2
  KAFKA_DEFAULT_REPLICATION_FACTOR: 3
  KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS: 0
x-etcd: &etcd
  image: gcr.io/etcd-development/etcd:v3.5.21
x-replica: &replica
  image: postgres:17
  user: postgres
  environment: { PGPASSWORD: pw }
  depends_on: { pg-primary: { condition: service_healthy }, toxiproxy: { condition: service_started } }

services:
  archive-perms:                       # the archive volume must belong to the postgres uid (999)
    image: busybox
    command: chown -R 999:999 /archive
    volumes: [pgarchive:/archive]
  pg-primary:
    image: postgres:17
    container_name: pg-primary
    environment: { POSTGRES_PASSWORD: pw, POSTGRES_INITDB_ARGS: --data-checksums }
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - pgarchive:/archive
      - ./pg-init.sh:/docker-entrypoint-initdb.d/10-repl.sh:ro
    depends_on: { archive-perms: { condition: service_completed_successfully } }
    command: [postgres, -c, wal_level=replica, -c, max_wal_senders=10, -c, wal_keep_size=1GB,
              -c, max_prepared_transactions=20, -c, shared_preload_libraries=pg_stat_statements,
              -c, archive_mode=on, -c, "archive_command=test ! -f /archive/%f && cp %p /archive/%f"]
    healthcheck: { test: ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], interval: 2s, retries: 30 }
  pg-replica1:
    <<: *replica
    container_name: pg-replica1
    ports: ["5433:5432"]
    command: [bash, -c, 'D=/var/lib/postgresql/replica;
      [ -s $$D/PG_VERSION ] || until pg_basebackup -h toxiproxy -p 25432 -U postgres -D $$D -R -X stream -c fast; do sleep 1; done;
      chmod 700 $$D; exec postgres -D $$D -c hot_standby_feedback=on -c max_prepared_transactions=20 -c cluster_name=replica1']
  pg-replica2:
    <<: *replica
    container_name: pg-replica2
    ports: ["5434:5432"]
    command: [bash, -c, 'D=/var/lib/postgresql/replica;
      [ -s $$D/PG_VERSION ] || until pg_basebackup -h toxiproxy -p 25433 -U postgres -D $$D -R -X stream -c fast; do sleep 1; done;
      chmod 700 $$D; exec postgres -D $$D -c hot_standby_feedback=on -c max_prepared_transactions=20 -c cluster_name=replica2']
  pg-delayed:                          # chapter 38 only: docker compose --profile dr up -d
    <<: *replica
    container_name: pg-delayed
    profiles: [dr]
    ports: ["5435:5432"]
    command: [bash, -c, 'D=/var/lib/postgresql/replica;
      [ -s $$D/PG_VERSION ] || until pg_basebackup -h pg-primary -U postgres -D $$D -R -X stream -c fast; do sleep 1; done;
      chmod 700 $$D; exec postgres -D $$D -c max_prepared_transactions=20 -c cluster_name=delayed -c recovery_min_apply_delay=10min']
  toxiproxy:
    image: ghcr.io/shopify/toxiproxy:2.9.0
    container_name: toxiproxy
    command: [-host=0.0.0.0, -config=/toxiproxy.json]
    volumes: [./toxiproxy.json:/toxiproxy.json:ro]
    extra_hosts: ["host.docker.internal:host-gateway"]
    ports: ["8474:8474", "15432:15432", "16379:16379", "18000-18002:18000-18002"]
  redis:
    image: redis:7.4
    container_name: redis
    ports: ["6379:6379"]
  kafka1: { <<: *kafka, container_name: kafka1, ports: ["19092:9092"],
            environment: { <<: *kafka-env, KAFKA_NODE_ID: 1, KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka1:29092,EXTERNAL://localhost:19092" } }
  kafka2: { <<: *kafka, container_name: kafka2, ports: ["19093:9092"],
            environment: { <<: *kafka-env, KAFKA_NODE_ID: 2, KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka2:29092,EXTERNAL://localhost:19093" } }
  kafka3: { <<: *kafka, container_name: kafka3, ports: ["19094:9092"],
            environment: { <<: *kafka-env, KAFKA_NODE_ID: 3, KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka3:29092,EXTERNAL://localhost:19094" } }
  etcd1: { <<: *etcd, container_name: etcd1, ports: ["2379:2379"], command: [/usr/local/bin/etcd, --name=etcd1, --initial-advertise-peer-urls=http://etcd1:2380, --advertise-client-urls=http://etcd1:2379, --listen-peer-urls=http://0.0.0.0:2380, --listen-client-urls=http://0.0.0.0:2379, "--initial-cluster=etcd1=http://etcd1:2380,etcd2=http://etcd2:2380,etcd3=http://etcd3:2380", --initial-cluster-token=lab, --data-dir=/etcd-data] }
  etcd2: { <<: *etcd, container_name: etcd2, ports: ["22379:2379"], command: [/usr/local/bin/etcd, --name=etcd2, --initial-advertise-peer-urls=http://etcd2:2380, --advertise-client-urls=http://etcd2:2379, --listen-peer-urls=http://0.0.0.0:2380, --listen-client-urls=http://0.0.0.0:2379, "--initial-cluster=etcd1=http://etcd1:2380,etcd2=http://etcd2:2380,etcd3=http://etcd3:2380", --initial-cluster-token=lab, --data-dir=/etcd-data] }
  etcd3: { <<: *etcd, container_name: etcd3, ports: ["32379:2379"], command: [/usr/local/bin/etcd, --name=etcd3, --initial-advertise-peer-urls=http://etcd3:2380, --advertise-client-urls=http://etcd3:2379, --listen-peer-urls=http://0.0.0.0:2380, --listen-client-urls=http://0.0.0.0:2379, "--initial-cluster=etcd1=http://etcd1:2380,etcd2=http://etcd2:2380,etcd3=http://etcd3:2380", --initial-cluster-token=lab, --data-dir=/etcd-data] }
  jaeger:                              # docker compose --profile tracing up -d
    image: jaegertracing/all-in-one:1.60.0
    container_name: jaeger
    profiles: [tracing]
    environment: { COLLECTOR_OTLP_ENABLED: "true" }
    ports: ["16686:16686", "4317:4317", "4318:4318"]
volumes: { pgdata: {}, pgarchive: {} }
```

**`pg-init.sh`** (runs once, at the primary's first start):

```bash
#!/bin/bash
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 -U postgres -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"
```

**`toxiproxy.json`** (replicas stream WAL *through* toxiproxy, so you can lag one replica at a time):

```json
[
  {"name": "pg_repl1",   "listen": "0.0.0.0:25432", "upstream": "pg-primary:5432", "enabled": true},
  {"name": "pg_repl2",   "listen": "0.0.0.0:25433", "upstream": "pg-primary:5432", "enabled": true},
  {"name": "pg_primary", "listen": "0.0.0.0:15432", "upstream": "pg-primary:5432", "enabled": true},
  {"name": "redis",      "listen": "0.0.0.0:16379", "upstream": "redis:6379",      "enabled": true},
  {"name": "app0",       "listen": "0.0.0.0:18000", "upstream": "host.docker.internal:8000", "enabled": true},
  {"name": "app1",       "listen": "0.0.0.0:18001", "upstream": "host.docker.internal:8001", "enabled": true},
  {"name": "app2",       "listen": "0.0.0.0:18002", "upstream": "host.docker.internal:8002", "enabled": true}
]
```

Start and smoke-test, and define the helpers every chapter uses:

```bash
docker compose up -d
psql postgresql://postgres:pw@localhost:5432/postgres -c \
  "SELECT application_name, state, sync_state, replay_lag FROM pg_stat_replication;"   # replica1, replica2, streaming
docker exec etcd1 etcdctl --endpoints=etcd1:2379,etcd2:2379,etcd3:2379 endpoint status -w table
docker exec kafka1 /opt/kafka/bin/kafka-metadata-quorum.sh --bootstrap-server kafka1:29092 describe --status
# toxiproxy: add / remove a toxic, cut / restore a link (downstream = server-to-client bytes)
tox()    { curl -s -X POST localhost:8474/proxies/$1/toxics -d "$2"; }   # tox pg_repl1 '{"name":"lag","type":"latency","stream":"downstream","attributes":{"latency":300}}'
untox()  { curl -s -X DELETE localhost:8474/proxies/$1/toxics/$2; }        # untox pg_repl1 lag
cut()    { curl -s -X POST localhost:8474/proxies/$1 -d '{"enabled":false}'; }
heal()   { curl -s -X POST localhost:8474/proxies/$1 -d '{"enabled":true}'; }
KT="docker exec kafka1 /opt/kafka/bin"      # $KT/kafka-topics.sh --bootstrap-server kafka1:29092 ...
```

Faults you will use: `docker kill` (SIGKILL, crash-stop), `docker stop` (SIGTERM, graceful),
`docker pause` / `kill -STOP <pid>` (a GC pause or a hung VM: alive but silent),
`docker network disconnect dslab_default <c>` (partition), toxiproxy toxics (latency, `timeout`,
`reset_peer`, `bandwidth`). Reset everything with `docker compose down -v`.

Related code in this repo: [KV store design](../solutions/key-value-store-design.md),
[job scheduler on Postgres](../solutions/job-scheduler-postgres-deep-dive.md),
[workflow orchestration](../solutions/workflow-orchestration-design.md),
[WAL deep dive](../solutions/write-ahead-log-deep-dive.md),
[distributed counter](../implementation/distributed-counter/distributed-counter-10m/),
[Kafka fan-out feed](../implementation/instagram-feed/instagram-feed-10m/).

---

## 00 — Primitives, System Models & Consistency Models  ([chapter](00-primitives-and-system-models.md))
**Time:** ~4 h · **Needs:** compose (Redis, etcd, toxiproxy, Postgres replicas), Python; Go optional

- [ ] **00.1 The third outcome of a remote call** *(Level: Core)*
  - **Goal:** see §1.2's "unknown" outcome: the client times out, the effect still happened.
  - **Do:** 1,000 times, open a fresh raw socket to `localhost:16379` (Redis via toxiproxy), send
    `b"*2\r\n$4\r\nINCR\r\n$1\r\nc\r\n"` and wait 200 ms for the reply; count successes and timeouts.
    (Raw RESP, because a client library's handshake would also be dropped; toxicity applies per connection.) Run once with
    `tox redis '{"name":"t","type":"timeout","stream":"downstream","toxicity":0.1,"attributes":{"timeout":0}}'`
    (replies dropped) and once with the same toxic on `"stream":"upstream"` (requests dropped).
  - **Predict:** for each run, `GET c` minus the client's success count.
  - **Verify:** downstream run: `c` ≈ 1,000 while the client saw ~100 timeouts; upstream run: `c` ≈ 900.
    Same client view, different server state. Write one line on why a retry is safe only for one of them.
- [ ] **00.2 Build a linearizability checker** *(Level: Core)*
  - **Goal:** turn §9's definition into code and catch a real violation.
  - **Do:** record a history of `Op(client, kind, value, invoke_t, return_t)` on one register
    (4 clients, 1 writer, ~12 ops per run, `time.monotonic()`). Write
    `def linearizable(history: list[Op]) -> bool` as a brute-force search over orders that respects
    real time (`a.return_t < b.invoke_t` ⇒ a first) and register semantics. Run it on (a) Redis
    `SET`/`GET` on one node, (b) writes to the Postgres primary and reads from `pg-replica1` with a
    100 ms `latency` toxic on `pg_repl1`. Optionally feed the same histories to Porcupine (Go).
  - **Predict:** which of (a) and (b) fails, and in what fraction of 50 runs.
  - **Verify:** (a) passes 50/50; (b) fails in most runs, and the checker prints the stale read. A
    hand-written 3-op violating history fails; its serial rewrite passes.
- [ ] **00.3 CAP on a real partition** *(Level: Core)*
  - **Goal:** observe §7.4: under partition, a linearizable read on the minority is unavailable; a serializable one is stale.
  - **Do:** `etcdctl put k v1`; `docker network disconnect dslab_default etcd3`; `etcdctl put k v2` via etcd1.
    On etcd3: `docker exec etcd3 etcdctl --command-timeout=3s get k` and then `... get k --consistency=s`.
    Reconnect with `docker network connect dslab_default etcd3`.
  - **Predict:** the result of each read on etcd3.
  - **Verify:** the default read times out; `--consistency=s` returns `v1` while the majority has `v2`.
- [ ] **00.4 Gray failure** *(Level: Stretch)*
  - **Goal:** reproduce §5: healthy to the detector, dead to the user.
  - **Do:** a FastAPI app with `/healthz` (returns 200 immediately) and `/work` (needs one of 4 DB
    connections from a pool, then a query). Put a 3 s `latency` toxic on `pg_primary` and point the pool
    at `localhost:15432`. Poll `/healthz` every second and send 20 req/s to `/work` with a 1 s timeout.
  - **Verify:** 100% of health checks pass while `/work` success is < 10%. Then add a health check
    that exercises the pool with a 200 ms budget and show it goes red within 5 s.
- [ ] **00.5 Explain it** *(Level: Core)*
  - **Goal:** use §8 and §11. Write a 5-sentence note to a product manager classifying Redis
    (single node), etcd and your Postgres primary + async replicas under PACELC, citing your 00.2
    and 00.3 numbers.

**Checkpoint (closed book):**
1. What are the three outcomes of a remote call, and why can't a client tell two of them apart?
2. Why does tolerating `f` crash faults need `n = 2f + 1` nodes with majority quorums?
3. What does linearizability require that sequential consistency does not?
<details><summary>Answers</summary>

1. Success, failure, unknown (timeout). "Never executed" and "executed but the reply was lost" look identical to the caller.
2. Any two majorities of `2f+1` intersect, and after `f` crashes `f+1` nodes, still a majority, remain.
3. Real-time order: if op A returns before op B is invoked, A must appear before B in the single total order.
</details>

---

## 03 — Consensus (Raft) and Distributed Locking  ([chapter](03-consensus-raft-and-distributed-locking.md))
**Time:** ~5 h (plus 2–4 days for the Raft lab) · **Needs:** compose (etcd, Redis, Postgres), Python; Go for 03.5

- [ ] **03.1 Measure etcd's unavailability window** *(Level: Core)*
  - **Goal:** time a leader loss against the §3.4 timing rules.
  - **Do:** find the leader (`etcdctl endpoint status -w table`, column IS LEADER). Writer: `httpx` POST
    to a *follower's* JSON gateway `http://localhost:<port>/v3/kv/put` (base64 key/value) every 10 ms
    with a 300 ms timeout; log each success timestamp. (Try a 5 s timeout once: the request in flight to
    the dead leader hangs, and your client timeout becomes part of the outage.) Take the leader down three ways, one per run:
    `docker stop` (SIGTERM), `docker kill` (SIGKILL), `docker pause`. The window is the largest gap
    between successes.
  - **Predict:** the window for each of the three (etcd defaults: heartbeat 100 ms, election timeout 1,000 ms).
  - **Verify:** SIGTERM ≈ tens of ms (etcd transfers leadership on shutdown); SIGKILL and pause ≈ 1–2 s.
    Recreate the cluster with `--election-timeout=5000` and show the SIGKILL window scales with it.
- [ ] **03.2 A lease lock without fencing corrupts data** *(Level: Core)*
  - **Goal:** reproduce §9.1 and §13.2 with a simulated GC pause.
  - **Do:** table `acct(id int primary key, balance int, fence bigint default 0)`. Each worker takes
    `SET lock:acct <uuid> NX PX 2000` in Redis, reads the balance, sleeps 300 ms, writes `balance + 100`.
    Harness: start worker A, `kill -STOP` it right after its read, wait 3 s, run worker B to completion,
    then `kill -CONT` A.
  - **Predict:** the final balance after starting from 0.
  - **Verify:** 100, not 200: B's update is lost although "the lock" was held for each write.
- [ ] **03.3 Fix it with a fencing token** *(Level: Core)*
  - **Goal:** apply §9.2 and §9.4: the storage rejects stale holders.
  - **Do:** on acquire, also `INCR fence:acct` and keep the value. Write with
    `UPDATE acct SET balance = $1, fence = $2 WHERE id = 1 AND fence < $2`; 0 rows ⇒ abort.
    Alternative: use `etcdctl lock` and the lock key's `create_revision` as the token.
  - **Verify:** 20 paused trials, final balance always 200; worker A logs "fenced off" every time.
- [ ] **03.4 ReadIndex has a price** *(Level: Stretch)*
  - **Goal:** measure §7.2 vs a serializable read.
  - **Do:** 5,000 `POST /v3/kv/range` to a follower with and without `"serializable": true`; p50 and p99.
    Repeat while the leader is paused for 500 ms every 5 s (`docker pause`/`unpause` loop).
  - **Predict:** the ratio of linearizable to serializable p99 in both runs.
  - **Verify:** linearizable reads stall for the pause; serializable ones do not, and 00.3 tells you what they may return.
- [ ] **03.5 Raft under partitions** *(Level: Stretch)*
  - **Goal:** implement Raft and make it survive the §15.1 failure scenarios.
  - **Do:** MIT 6.5840 Lab 3 (3A–3D; follow the current year's lab page) or `eliben/raft` in Go.
    Loop each test: `for i in $(seq 100); do go test -run 3B -race >/dev/null || echo FAIL $i; done`.
  - **Verify:** 0 failures in 100 runs of each part. Record your first failure and which §5 rule it broke.

**Checkpoint (closed book):**
1. Why does a lock service with perfect leases still need fencing tokens?
2. What does a leader do to serve a ReadIndex read?
3. Why may a new leader not commit an entry from an earlier term by counting replicas?
<details><summary>Answers</summary>

1. The holder can pause (GC, SIGSTOP, VM stall) past lease expiry and then write; only the storage, checking a monotonic token, can reject the stale write.
2. Records its commit index, confirms it is still leader with a heartbeat round to a majority, waits until applied index ≥ that index, then answers locally.
3. Such an entry can still be overwritten by a leader from another term (Figure 8). Only current-term entries are committed by count; earlier ones commit indirectly with them.
</details>

---

## 04 — Replication and Consistency  ([chapter](04-replication-and-consistency.md))
**Time:** ~6 h · **Needs:** compose (Postgres primary + 2 replicas, toxiproxy), Python

- [ ] **04.1 Measure replication lag three ways** *(Level: Core)*
  - **Goal:** use §6.3's queries and learn which ones lie.
  - **Do:** `pgbench -i -s 20` on the primary, then `pgbench -c 8 -T 120`. Every second, log from
    `pg_stat_replication` the three backlog columns and `replay_lag`, and on each replica
    `now() - pg_last_xact_replay_timestamp()`. Add `tox pg_repl1 '{"name":"lag","type":"latency","attributes":{"latency":250}}'`
    at t = 40 s, remove it at t = 80 s. Stop pgbench and keep logging for 60 s.
  - **Predict:** `replay_lag` on replica1 during the toxic; the replay-timestamp metric after pgbench stops.
  - **Verify:** replica1 `replay_lag` ≈ 250 ms+ during the toxic, replica2 unchanged; the timestamp
    metric grows 1 s per second on an idle primary although lag is 0.
- [ ] **04.2 Break read-your-writes, fix it with an LSN token** *(Level: Core)*
  - **Goal:** reproduce §6.4's anomaly and implement §7.2 option (3).
  - **Do:** with a 100 ms toxic on `pg_repl1`, 1,000 iterations of: `UPDATE profile SET name = $n`
    on the primary, then `SELECT name` on replica1; count mismatches. Fix: after the write run
    `SELECT pg_current_wal_insert_lsn()`; before reading, poll the replica with
    `SELECT pg_last_wal_replay_lsn() >= $1::pg_lsn` every 5 ms for up to 300 ms, else read the primary.
  - **Predict:** mismatch count before the fix; added p50 latency after it.
  - **Verify:** ~1,000 mismatches before; 0 after. Record p50/p99 read latency and the primary-fallback rate at toxics of 100 ms and 500 ms.
- [ ] **04.3 Monotonic reads across two replicas** *(Level: Core)*
  - **Goal:** §7.3: time must not go backwards for one session.
  - **Do:** toxics of 50 ms on `pg_repl1` and 800 ms on `pg_repl2`; one writer increments a counter every
    20 ms; a reader alternates replicas and counts reads smaller than its previous read. Fix: keep the
    max LSN seen per session (the 04.2 token) and apply it to every read.
  - **Verify:** hundreds of backwards reads before, 0 after.
- [ ] **04.4 Last-writer-wins loses writes under clock skew** *(Level: Core)*
  - **Goal:** §14: timestamps from skewed clocks silently drop the later write.
  - **Do:** table `kv(k text primary key, v int, ts timestamptz)`; upsert with
    `ON CONFLICT (k) DO UPDATE SET v = excluded.v, ts = excluded.ts WHERE excluded.ts > kv.ts`.
    Two writers alternate every 20 ms (real time); writer B stamps `ts = now() - skew`. Log what each
    writer believes it wrote; count writes that were acknowledged but never visible.
  - **Predict:** the lost-write fraction at skew 0, 10, 50, 200 ms.
  - **Verify:** 0 at skew 0; at 200 ms nearly all of B's writes vanish with no error anywhere.
- [ ] **04.5 Quorum staleness and the non-linearizable quorum** *(Level: Stretch)*
  - **Goal:** check §8.8's stale-read probability and §9.1's anomaly by simulation.
  - **Do:** asyncio simulation of N = 3 replicas with lognormal propagation delay (median 5 ms). Measure
    P(stale) vs time since the write ack for (W, R) = (1, 1), (2, 1), (2, 2). Then construct §9.1: a read
    concurrent with a write returns the new value, a later read returns the old one. Add ABD write-back (§9.2).
  - **Predict:** P(stale) for (1, 1) immediately after the ack.
  - **Verify:** about 2/3 for (1, 1) at t = 0, decaying with the delay CDF; exactly 0 for (2, 2); the §9.1 history appears without write-back and never with it (check with your 00.2 checker).
- [ ] **04.6 Explain it** *(Level: Core)*
  - **Goal:** a design note to your backend team: which reads in your app need which §7.1 guarantee, and the §7.8 cost of each, using your 04.2 numbers.

**Checkpoint (closed book):**
1. Why is `now() - pg_last_xact_replay_timestamp()` not a lag metric?
2. `R + W > N` guarantees overlap. Why is that still not linearizable?
3. How does an LSN token give read-your-writes, and what must it also cover?
<details><summary>Answers</summary>

1. It is the time since the last *replayed commit*; on an idle primary it grows by one second per second while the replica is fully caught up.
2. A read concurrent with a write can see the new value on one read and the old value on a later one; readers must write back what they return (ABD) before returning.
3. Save the commit LSN in the session and read from a replica only when `pg_last_wal_replay_lsn() >= token`, else wait or use the primary. It must cover every read path, including caches.
</details>

---

## 06 — Transactions Across Services: 2PC, Sagas, Outbox, Idempotency  ([chapter](06-distributed-transactions-sagas-outbox-idempotency.md))
**Time:** ~7 h · **Needs:** compose (Postgres, Kafka, toxiproxy), Python, FastAPI. **Also do §12** (four `psql` experiments).

- [ ] **06.1 The dual write, crashed on purpose** *(Level: Core)*
  - **Goal:** reproduce §1.2 Timeline A and count the damage.
  - **Do:** `create_order()` commits an `orders` row, then with probability 0.05 calls `os._exit(1)`,
    else produces `OrderPlaced` to topic `orders` (RF 3). A supervisor restarts it until 2,000 orders exist.
    Count distinct order ids in the table and in the topic (a consumer from the earliest offset).
  - **Predict:** how many orders have no event.
  - **Verify:** about 100 (5%) missing events, with no error logged anywhere.
- [ ] **06.2 Fix it with an outbox and a relay** *(Level: Core)*
  - **Goal:** §4.2–§4.4: same transaction, then at-least-once publish.
  - **Do:** insert into `outbox` (§4.2 schema) in the order's transaction. Relay: `FOR UPDATE SKIP LOCKED`
    batch of 100, produce with `acks=all`, flush, set `published_at`, commit. `kill -9` the relay at
    random every 5–15 s during a 5,000-order run.
  - **Predict:** missing events and duplicate `event_id`s in the topic.
  - **Verify:** 0 missing; duplicates > 0 (published, then killed before `published_at` committed). Compare with [the job scheduler's SKIP LOCKED design](../solutions/job-scheduler-postgres-deep-dive.md).
- [ ] **06.3 Double charge from a retried POST, then an idempotency key** *(Level: Core)*
  - **Goal:** §5.3 and §6.1 against a real timeout.
  - **Do:** FastAPI `POST /charge` inserts into `charges` then responds; put a 500 ms `latency` toxic
    on `app0` and call it at `localhost:18000` with a 300 ms timeout and 3 retries. Then implement the
    §5.3 table with the `idempotency_keys` schema from §6. Fire 50 concurrent identical requests with one key.
  - **Predict:** `charges` rows per logical payment before the fix.
  - **Verify:** 4 rows per payment before; after: 1 row, every response 200 with the same body or 409
    (in progress); the same key with a different amount gets 422.
- [ ] **06.4 A saga whose compensation fails** *(Level: Core)*
  - **Goal:** §3.7 and §7.5: a persisted state machine that never loses track.
  - **Do:** orchestrator with steps reserve inventory → charge → create shipment; shipment fails 20%.
    Compensation `release_inventory` fails transiently 50% and permanently 2%. Persist saga state and
    step results in Postgres; retry compensations with capped exponential backoff; after 10 attempts
    move to `NEEDS_ATTENTION`. Kill the orchestrator every 10 s during 1,000 sagas.
  - **Verify:** every saga ends `COMPLETED`, `COMPENSATED` or `NEEDS_ATTENTION` (0 stuck in between);
    no saga has a charge without a shipment or a refund; `NEEDS_ATTENTION` ≈ 2% of the failed ones.
- [ ] **06.5 The 2PC blocking window** *(Level: Stretch)*
  - **Goal:** feel §2.2 with Postgres's real prepared transactions.
  - **Do:** session A: `BEGIN; UPDATE acct SET balance = balance - 10 WHERE id = 1; PREPARE TRANSACTION 'tx1';`
    then close A (the "coordinator" is gone). Session B: `UPDATE acct SET balance = 0 WHERE id = 1;`
    Inspect `pg_prepared_xacts` and `pg_locks`; restart the primary; resolve with `COMMIT PREPARED 'tx1'`.
  - **Predict:** does B finish? Does the lock survive a restart?
  - **Verify:** B blocks until you resolve tx1, including across the restart. Time the block.

**Checkpoint (closed book):**
1. Commit-then-publish and publish-then-commit fail differently. How?
2. What does an outbox guarantee about delivery, and what must consumers add?
3. What is a pivot step in a saga?
<details><summary>Answers</summary>

1. Commit-then-publish loses the event on a crash between the two. Publish-then-commit emits an event for a change that may roll back (a phantom).
2. At-least-once publication in commit order per relay batch, not exactly-once. Consumers deduplicate by `event_id`, for example with an inbox table written in the same transaction as the effect.
3. The go/no-go step: before it every step is compensatable, after it the saga can only move forward with retriable steps.
</details>

---

## 07 — Kafka and Event Streaming  ([chapter](07-kafka-and-event-streaming.md))
**Time:** ~6 h · **Needs:** compose (3-broker KRaft Kafka), Python `confluent-kafka`

- [ ] **07.1 acks=1 vs acks=all under a broker kill** *(Level: Core)*
  - **Goal:** count lost acknowledged messages (§3.5, §12.1).
  - **Do:** `$KT/kafka-topics.sh --bootstrap-server kafka1:29092 --create --topic acks --partitions 1 --replication-factor 3 --config min.insync.replicas=2`.
    Producer sends sequence numbers at ~2,000/s for 20 s with `linger.ms=5`, recording every seq whose
    delivery callback had no error. First run: `docker kill` the partition leader (`--describe` shows it)
    at t = 5 s. Second run: at t = 5 s `docker pause` both followers, at t = 10 s `docker kill` the leader
    and `docker unpause` the followers. Restart the leader, consume from the beginning, compute
    `acked − consumed`. Do both runs with `acks=1` and with `acks=all` (+ `enable.idempotence=true`), on a fresh topic each time.
  - **Predict:** lost count for each of the four runs.
  - **Verify:** plain kill: usually 0 even for `acks=1`, because on one laptop followers are
    milliseconds behind. Paused followers: `acks=1` loses ≈ the 5 s of writes only the leader held
    (thousands); `acks=all` loses 0, its writes waited instead.
- [ ] **07.2 Durability costs availability** *(Level: Core)*
  - **Goal:** §12.2's trade from the other side.
  - **Do:** topic with `min.insync.replicas=3`, RF 3. `docker stop` one follower, then produce 100
    messages with `acks=all` and `delivery.timeout.ms=10000`.
  - **Predict:** what the delivery callbacks report, and what changes with `min.insync.replicas=2`.
  - **Verify:** MISR 3: all 100 fail with `_MSG_TIMED_OUT` (the broker kept answering the retriable
    `NOT_ENOUGH_REPLICAS`); MISR 2: all succeed.
    Record the longest delivery gap during a leader kill with MISR 2 (the election window).
- [ ] **07.3 A consumer rebalance storm** *(Level: Core)*
  - **Goal:** §4.4 and §12.3.
  - **Do:** 12-partition topic, 6 consumers, `session.timeout.ms=6000`, `max.poll.interval.ms=10000`;
    processing sleeps 0–1 s, but 3% of records sleep 12 s. Count `on_assign` calls and records processed
    twice (log `(partition, offset)`). Then fix: `max.poll.records`-style batching (`consume(num_messages=10)`),
    `partition.assignment.strategy=cooperative-sticky`, and `group.instance.id` (static membership).
    Finally do a rolling restart of all 6 consumers, each down for 3 s.
  - **Predict:** rebalances in 10 minutes before and after; rebalances during the rolling restart with static membership.
  - **Verify:** tens of rebalances and duplicates before; near 0 after; rolling restart: 0 rebalances with static membership, 6+ without.
- [ ] **07.4 Exactly-once consume-transform-produce** *(Level: Core)*
  - **Goal:** §6.2–§6.3: what Kafka's EOS covers.
  - **Do:** read `in` (50,000 records), write `out` with a transactional producer:
    `init_transactions()`, `begin_transaction()`, produce, `send_offsets_to_transaction(consumer.position(consumer.assignment()), consumer.consumer_group_metadata())`,
    `commit_transaction()` every 100 records. `os._exit(1)` at random every ~5 s; a supervisor restarts it.
    Verify with a consumer using `isolation.level=read_committed`, then `read_uncommitted`.
  - **Predict:** duplicates in `out` for each isolation level, and for a non-transactional version.
  - **Verify:** read_committed: exactly 50,000 distinct, 0 duplicates; read_uncommitted and the non-transactional run: duplicates > 0.
- [ ] **07.5 A hot partition in consumer lag** *(Level: Stretch)*
  - **Goal:** §5.4: one key can pin one consumer.
  - **Do:** 6 partitions, 3 consumers, 60% of messages keyed `tenant-1`. Watch
    `$KT/kafka-consumer-groups.sh --bootstrap-server kafka1:29092 --describe --group g` every 10 s.
    Mitigate with a key suffix `tenant-1#<n % 4>` and re-measure. Compare with [the feed fan-out implementation](../implementation/instagram-feed/instagram-feed-10m/).
  - **Verify:** before: one partition's LAG grows while others stay ~0; after: max lag / mean lag < 2. Note which ordering guarantee you gave up.

**Checkpoint (closed book):**
1. RF 3, `min.insync.replicas=2`, `acks=all`: how many broker failures before producers get errors?
2. List three things that trigger a rebalance with the classic group protocol.
3. What does Kafka exactly-once cover, and what does it not?
<details><summary>Answers</summary>

1. One is tolerated (after a brief leader election if it was the leader). With two down the ISR is below 2 and produces fail with `NOT_ENOUGH_REPLICAS`.
2. Any three of: a consumer joins or leaves, a missed session timeout, `max.poll.interval.ms` exceeded, a subscription or partition-count change.
3. Read-process-write inside Kafka: idempotent producer, transactions that commit output and input offsets atomically, `read_committed` consumers. Not external side effects such as a DB write or an HTTP call.
</details>

---

## 08 — Caching Strategies and Patterns  ([chapter](08-caching-strategies-and-patterns.md))
**Time:** ~5 h · **Needs:** compose (Redis, Postgres), Python asyncio

- [ ] **08.1 A cache stampede at TTL expiry** *(Level: Core)*
  - **Goal:** reproduce §4.1 and count the herd.
  - **Do:** cache-aside key with TTL 5 s; the loader runs `SELECT pg_sleep(0.2), now()` on Postgres.
    500 concurrent asyncio readers at 1,000 reads/s total for 60 s. Count loader calls per expiry
    (`calls` in `pg_stat_statements`, reset with `pg_stat_statements_reset()`).
  - **Predict:** loader calls per expiry (rate × recompute time).
  - **Verify:** ≈ 200 per expiry and a p99 read latency above 200 ms.
- [ ] **08.2 Three fixes, measured side by side** *(Level: Core)*
  - **Goal:** compare §4.2 (Redis lock), §4.4 (singleflight) and §4.3 (XFetch).
  - **Do:** re-run 08.1 with each. Singleflight: run 4 reader processes. XFetch: store `delta` and `expiry`
    with the value and use the chapter's `should_refresh_early`.
  - **Predict:** loads per expiry for each fix, and which one has zero cache misses.
  - **Verify:** lock ≈ 1 load but readers wait or serve stale; singleflight ≈ 4 (one per process); XFetch ≈ 1–3 loads and 0 misses.
- [ ] **08.3 The stale-cache race** *(Level: Core)*
  - **Goal:** the cache-aside race of §3 and §11 that TTLs only bound.
  - **Do:** reproduce deterministically with `asyncio.Event` gates: reader misses and reads v1 from the DB;
    writer updates DB to v2 and `DEL`s the key; reader then `SET`s v1. Then run 10,000 random
    reader/writer interleavings (TTL 60 s) for three write orders: set cache then DB, DB then set cache,
    DB then delete cache. Count keys where cache ≠ DB at the end. Fix with a lease (§3.4) or a version
    check in a Lua script (`SET` only if the stored version is older).
  - **Predict:** which write order produces the most stale keys.
  - **Verify:** all three orders leave stale keys > 0; the lease or version fix leaves 0.
- [ ] **08.4 Eviction policy under a scan** *(Level: Stretch)*
  - **Goal:** §5: measure, don't trust, hit ratios.
  - **Do:** Redis with `maxmemory 50mb`; replay a Zipf(1.0) trace over 1M keys with a 20% one-off
    scan in the middle. Compare `allkeys-lru` and `allkeys-lfu` using `INFO stats` (`keyspace_hits`,
    `keyspace_misses`). In Python, add SIEVE (§5.5) on the same trace.
  - **Predict:** which policy loses most hit ratio during the scan.
  - **Verify:** a table of hit ratio before, during and after the scan for each policy.

**Checkpoint (closed book):**
1. How big is a stampede for a key read R times per second whose recompute takes T seconds?
2. State the XFetch early-refresh condition.
3. Describe the cache-aside interleaving that leaves a stale value after a correct "update DB, then delete cache".
<details><summary>Answers</summary>

1. About `R × T` concurrent recomputations for each expiry.
2. Refresh when `now − delta · beta · ln(rand()) ≥ expiry`, where `delta` is the recompute time and `rand()` is in (0, 1].
3. A reader misses and reads the old value from the DB. The writer then updates the DB and deletes the key. The reader then writes its old value into the cache, where it stays until the TTL.
</details>

---

## 10 — Sharding and Consistent Hashing  ([chapter](10-sharding-and-consistent-hashing.md))
**Time:** ~4 h · **Needs:** Python; compose Postgres for 10.4

- [ ] **10.1 Vnodes and balance** *(Level: Core)*
  - **Goal:** reproduce §4.3's table.
  - **Do:** a ring with `bisect` over 64-bit hashes (`hashlib.blake2b(digest_size=8)`), N = 10 nodes,
    V ∈ {1, 10, 50, 256}, 1M keys. Report std/mean and max/mean of keys per node.
  - **Predict:** std/mean for V = 1 and V = 256 (§4.3 gives `≈ 1/√V`).
  - **Verify:** your numbers are within 2× of the chapter's table.
- [ ] **10.2 Key movement when adding a node** *(Level: Core)*
  - **Goal:** §3.3 and §5 variants.
  - **Do:** go from 10 to 11 nodes with modulo hashing, the ring (V = 256), jump hash (§5.1) and
    rendezvous hashing (§5.2). Measure the fraction of keys that move and where they go.
  - **Predict:** the moved fraction for modulo and for the ring.
  - **Verify:** modulo ≈ 91%, the others ≈ 9.1% (1/11), and every moved key goes to the new node.
- [ ] **10.3 A hot key and its mitigation** *(Level: Core)*
  - **Goal:** §6.4 and §13.4: vnodes do not fix a hot key.
  - **Do:** Zipf(1.2) traffic over 100k keys on the V = 256 ring; max/mean load per node. Mitigate
    (a) by splitting the top 10 keys into 8 sub-keys (writes pick one, reads fan in; compare with
    [the sharded counter](../implementation/distributed-counter/distributed-counter-10m/app/services/sharded_counter.py)),
    (b) with bounded-load consistent hashing (§5.5, c = 1.25) applied to key placement.
  - **Predict:** max/mean traffic before and after each.
  - **Verify:** before: the hottest node carries well above the mean; (a) brings max/mean under ~1.3;
    (b) balances key *counts* but not the hottest key's traffic. Write down the read cost of (a).
- [ ] **10.4 Logical shards in Postgres** *(Level: Stretch)*
  - **Goal:** §6.5's playbook: many small shards, moved whole.
  - **Do:** `CREATE TABLE ev (user_id bigint, ...) PARTITION BY HASH (user_id)` with 16 partitions
    (`FOR VALUES WITH (MODULUS 16, REMAINDER r)`); load 2M rows; check
    `SELECT tableoid::regclass, count(*) FROM ev GROUP BY 1`. Map partitions to 2 "hosts" in a
    directory table, then "add a host" by moving 5 partitions (`DETACH PARTITION`, copy, `ATTACH`).
  - **Verify:** `EXPLAIN SELECT ... WHERE user_id = 42` touches 1 partition; the move touched about
    5/16 of rows; show that going from `MODULUS 16` to 17 would rewrite nearly everything.
- [ ] **10.5 Explain it** *(Level: Core)*
  - **Goal:** use §14.1 to write a 5-sentence partition-key choice for an orders table (tenant, order id, created_at), citing 10.3.

**Checkpoint (closed book):**
1. Adding one node to N: what fraction of keys move with modulo hashing and with consistent hashing?
2. How does load spread scale with the number of vnodes V?
3. Why don't vnodes fix a hot key?
<details><summary>Answers</summary>

1. Modulo: about N/(N+1). Consistent hashing: about 1/(N+1).
2. std/mean ≈ 1/√V.
3. One key still hashes to one node whatever the number of vnodes. You must split, replicate or cache that key.
</details>

---

## 17 — Networking Protocols and Communication  ([chapter](17-networking-protocols-and-communication.md))
**Time:** ~5 h · **Needs:** Python (`hypercorn`, `httpx[http2]`, `grpcio`), Docker for 17.3

- [ ] **17.1 Nagle meets delayed ACK** *(Level: Core)*
  - **Goal:** §1.5 as a latency number.
  - **Do:** raw-socket client that sends a request as two writes (header, then body) and waits for a
    reply; the server replies only after the full message. 1,000 round trips, then set
    `sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)` and repeat.
  - **Predict:** p50 per request with and without `TCP_NODELAY` (on Linux).
  - **Verify:** ~40 ms p50 with Nagle on (Linux delayed-ACK minimum), well under 1 ms with it off.
- [ ] **17.2 Head-of-line blocking** *(Level: Core)*
  - **Goal:** §3.1–§3.3 at the application layer.
  - **Do:** an ASGI app under `hypercorn` where `/slow` sleeps 2 s and `/fast` returns at once. Send 1
    `/slow` then 20 `/fast` concurrently: (a) `httpx.AsyncClient(limits=httpx.Limits(max_connections=1))`
    over HTTP/1.1, (b) `httpx.AsyncClient(http1=False, http2=True)` (h2c, one connection).
  - **Predict:** p50 of `/fast` in (a) and (b).
  - **Verify:** (a) ≈ 2 s, (b) ≈ ms. Stretch: in a container with `--cap-add NET_ADMIN`, add
    `tc qdisc add dev eth0 root netem loss 2%` and compare `/fast` p99 on one HTTP/2 connection vs
    six HTTP/1.1 connections (TCP-level HOL, the reason for QUIC).
- [ ] **17.3 TIME_WAIT exhaustion** *(Level: Core)*
  - **Goal:** §1.4's port math, on purpose.
  - **Do:** run a TCP server container and a client container on one network; start the client with
    `docker run --sysctl net.ipv4.ip_local_port_range="40000 40999" --sysctl net.ipv4.tcp_tw_reuse=0 ...`.
    The client opens a connection per request, sends, reads, and closes first (the server waits for EOF
    before closing), as fast as possible.
    Watch `ss -tan state time-wait | wc -l` in the client. Then reuse one keep-alive connection.
  - **Predict:** connections before the first `EADDRNOTAVAIL` (errno 99), and the sustainable rate.
  - **Verify:** errno 99 after ~1,000 connections (in well under a second); sustainable ≈ 1,000/60 ≈ 17 conn/s; with reuse, TIME_WAIT stays ~0.
- [ ] **17.4 Deadlines that propagate, and ones that don't** *(Level: Core)*
  - **Goal:** §4.5: the callee should stop when the caller has given up.
  - **Do:** three `grpc.aio` servers A → B → C using generic handlers and raw bytes (no `.proto`:
    `grpc.method_handlers_generic_handler("lab.B", {"Call": grpc.unary_unary_rpc_method_handler(fn)})`,
    client `channel.unary_unary("/lab.C/Call")`). C sleeps 0.2–3 s. A calls with `timeout=1.0`.
    Version 1: B calls C with a fixed `timeout=5`. Version 2: `timeout=context.time_remaining() - 0.05`.
    Count, in C, requests that finished after A had already failed.
  - **Predict:** that wasted-work count out of 1,000 for each version.
  - **Verify:** version 1 wastes a large share; version 2 wastes ~0, and C sees `CANCELLED`/`DEADLINE_EXCEEDED`.
- [ ] **17.5 Explain it** *(Level: Stretch)*
  - **Goal:** a 5-sentence note to a service owner on why their proxy fails at 500 new connections/s to one backend (§1.4, §10.5), with the fix in order of preference.

**Checkpoint (closed book):**
1. With the default Linux port range and a 60 s TIME_WAIT, how many new connections per second can a client sustain to one destination IP:port?
2. What head-of-line blocking does HTTP/2 remove, and what remains?
3. How should a service set the timeout of its downstream call?
<details><summary>Answers</summary>

1. About 28,232 / 60 ≈ 470 per second.
2. It removes application-level HOL (one request per connection at a time). TCP-level HOL remains: one lost segment stalls every stream on the connection. QUIC removes that.
3. From the caller's remaining deadline minus a margin, never a fixed value, so the whole chain gives up together and cancels abandoned work.
</details>

---

## 22 — Stream Processing: Flink, Watermarks, Exactly-Once  ([chapter](22-stream-processing-flink-watermarks-eos.md))
**Time:** ~6 h · **Needs:** Python + compose Kafka; Java 17 + Flink 1.20 or PyFlink for 22.4

- [ ] **22.1 Watermark delay vs dropped late events** *(Level: Core)*
  - **Goal:** §4.4's trade-off as a curve.
  - **Do:** generate 1M events with event time = send time minus a delay: 95% Exp(mean 1 s), 5% uniform
    5–60 s. Implement 10 s tumbling windows with `watermark = max_event_time − B`; a window fires when the
    watermark passes its end, later events are dropped. Sweep B ∈ {0, 2, 5, 10, 30, 60} s.
  - **Predict:** dropped fraction at B = 0 and B = 10 s.
  - **Verify:** a table of B vs dropped % vs mean output delay; dropped falls as delay rises by exactly B.
- [ ] **22.2 One idle partition stalls everything** *(Level: Core)*
  - **Goal:** §4.5 on real Kafka.
  - **Do:** topic with 3 partitions; one consumer tracks a watermark per partition and uses the minimum.
    Produce to all three, then stop producing to partition 2. Log when windows fire. Add idleness:
    exclude a partition with no events for 5 s from the minimum.
  - **Predict:** how many windows fire after partition 2 goes quiet, without and with idleness.
  - **Verify:** 0 without; windows resume ~5 s later with it, and partition 2 rejoins when it produces again.
- [ ] **22.3 Allowed lateness and side outputs** *(Level: Core)*
  - **Goal:** §3.6: late data is a product decision.
  - **Do:** extend 22.1 with `allowed_lateness = 30 s`: re-emit an updated result for each late event
    within lateness; send later ones to a side list. Count re-emissions downstream.
  - **Verify:** 0 events silently dropped; downstream sees N updates per window and must upsert, not append.
- [ ] **22.4 Checkpoint and restore** *(Level: Stretch)*
  - **Goal:** §6.1 and §6.5 on a real runtime.
  - **Do:** Flink standalone (`bin/start-cluster.sh`), a keyed count job reading Kafka with checkpoints
    every 5 s to `file:///tmp/ckpt`. Kill the TaskManager JVM with `kill -9` mid-run; check recovery.
    Then `bin/flink stop --savepointPath /tmp/sp <jobId>` and resume with `bin/flink run -s <savepoint> -p 4 ...`
    at a different parallelism (§11.3). Without Flink: in Python, store counts and Kafka offsets in one
    Postgres transaction every 1,000 records and compare with committing offsets separately.
  - **Predict:** final counts vs ground truth in each variant.
  - **Verify:** atomic state + offsets: exact counts after `kill -9`; separate commits: over- or under-counts.

**Checkpoint (closed book):**
1. How does a bounded-out-of-orderness watermark generator compute the watermark?
2. Why does one idle input stall downstream windows, and what fixes it?
3. What does end-to-end exactly-once need besides Flink checkpoints?
<details><summary>Answers</summary>

1. Max event time seen minus the bound B (minus 1 ms in Flink).
2. An operator's watermark is the minimum over its inputs, so a silent input holds it back. Mark idle inputs (`withIdleness`) so they are excluded until they produce again.
3. A replayable source (Kafka offsets in the checkpoint) and a transactional (two-phase commit) or idempotent sink.
</details>

---

## 23 — Batch Processing: Spark and MapReduce  ([chapter](23-batch-processing-spark-mapreduce.md))
**Time:** ~4 h · **Needs:** Python; Java 17 + `pip install pyspark` for 23.2–23.4

- [ ] **23.1 MapReduce by hand, with and without a combiner** *(Level: Core)*
  - **Goal:** §2 "The Shuffle": measure what crosses the network.
  - **Do:** word count over `distributed-systems/*.md` with `multiprocessing`: 8 mappers,
    `hash(word) % 4` partitioning to 4 reducers via files. Count shuffled records and bytes,
    then add a map-side combiner.
  - **Predict:** the shuffle reduction factor from the combiner.
  - **Verify:** identical output both ways; the reduction factor ≈ total words / distinct words per mapper.
- [ ] **23.2 A skewed shuffle** *(Level: Core)*
  - **Goal:** §14 "Identifying Skew" in the Spark UI.
  - **Do:** `local[8]`, 20M rows where 40% have `user_id = NULL`; `groupBy("user_id").count()` with
    `spark.sql.shuffle.partitions=16`. Open `localhost:4040`, stage page, Summary Metrics.
  - **Predict:** max/median task duration of the shuffle stage.
  - **Verify:** max/median > 5×. Then salt (two-stage aggregation, salt computed once) or filter NULLs first; max/median < 2 and the wall time drops.
- [ ] **23.3 AQE fixes skewed joins, not skewed aggregations** *(Level: Stretch)*
  - **Goal:** §13 and §14's caveat.
  - **Do:** join the skewed table to a 1M-row dimension with `spark.sql.autoBroadcastJoinThreshold=-1`,
    AQE off, then on with `spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes=1MB`.
  - **Verify:** with AQE on, the SQL tab plan shows the skewed partition split and the stage max/median falls; the 23.2 groupBy is unchanged by AQE.
- [ ] **23.4 Read a plan before running it** *(Level: Core)*
  - **Goal:** retrieval of §6–§9.
  - **Do:** for 5 queries (filter, groupBy, join small×large, join large×large, `distinct`), write down
    how many `Exchange` nodes you expect, then check `df.explain("formatted")`. See also
    [the DAG pipeline design](../solutions/dag-pipeline-orchestration-design.md).
  - **Verify:** you got at least 4 of 5 right; explain each miss in one line.

**Checkpoint (closed book):**
1. Which operations cause a shuffle?
2. Why doesn't AQE's skew-join handling fix a skewed `groupBy`?
3. How does Spark recover a lost partition?
<details><summary>Answers</summary>

1. Wide dependencies: groupBy/aggregations, non-broadcast joins, `repartition`, `distinct`, sorts.
2. It splits skewed partitions of a sort-merge *join*. An aggregation still sends all rows of one key to one task; salting (two-stage aggregation) spreads them.
3. It recomputes the partition from its lineage, reusing persisted shuffle files where they survive; losing an executor's shuffle files re-runs the map stage that wrote them.
</details>

---

## 29 — Failure Detection: Phi Accrual, Heartbeats, Timeouts  ([chapter](29-failure-detection-phi-accrual.md))
**Time:** ~4 h · **Needs:** Python; compose toxiproxy for 29.2

- [ ] **29.1 Phi accrual vs a fixed timeout** *(Level: Core)*
  - **Goal:** §3 and §4 under jittery heartbeats.
  - **Do:** implement `class PhiAccrual: heartbeat(t)`, `phi(now) -> float` with a 1,000-interval
    window and `P_later = 0.5 * math.erfc((dt - mu) / (sigma * math.sqrt(2)))`. Simulate 24 h of 1 s
    heartbeats with N(0, 0.1 s) jitter plus 0.5% "GC pauses" of 2–4 s, then a real crash. Compare
    fixed timeouts of 2 s and 5 s with phi thresholds 3, 8, 12.
  - **Predict:** false suspicions per day and detection time for each detector.
  - **Verify:** a table; the 2 s timeout has the most false positives, the 5 s timeout the slowest detection; phi sits on a better point of that curve.
- [ ] **29.2 Real heartbeats through a noisy network** *(Level: Core)*
  - **Goal:** §7 "GC Pauses and False Failure Detection" with real processes.
  - **Do:** a sender process sends a TCP heartbeat every 200 ms to a receiver via toxiproxy (`app1`,
    `latency` toxic 20 ms with `jitter` 15). The receiver prints phi each 50 ms. `kill -STOP` the sender for
    1 s, then for 5 s; then kill it for good.
  - **Predict:** peak phi during the 1 s pause.
  - **Verify:** log of phi vs time; the 1 s pause crosses phi 8 or not as you predicted; detection time after the kill.
- [ ] **29.3 When the normal model lies** *(Level: Stretch)*
  - **Goal:** §4 "Why a Normal Distribution — and When That Breaks".
  - **Do:** feed 29.1 Pareto-tailed (α = 1.5) delays. For thresholds 1–8, compare the predicted false-positive probability `10^-φ` with the measured one.
  - **Verify:** measured ≫ predicted at high φ; record the factor at φ = 8.
- [ ] **29.4 SWIM indirect probes** *(Level: Stretch)*
  - **Goal:** §5 SWIM and Lifeguard: a single bad link should not kill a node.
  - **Do:** asyncio simulation, 10 members; one link A↔B drops 50% of packets. Count false "dead" declarations of B with direct probes only, then with k = 3 indirect probes.
  - **Verify:** false declarations fall by at least 10×.

**Checkpoint (closed book):**
1. Define φ.
2. Why is the real false-positive rate at φ = 8 much higher than 10⁻⁸?
3. What problem do SWIM's indirect probes solve?
<details><summary>Answers</summary>

1. φ = −log10(P_later(Δt)), where P_later is the probability, under the fitted inter-arrival distribution, that a live node's next heartbeat arrives later than now.
2. Real delays have heavier tails (GC, congestion) than the fitted normal.
3. A failure local to the prober or one link. Other members probe the target on its behalf before it is suspected.
</details>

---

## 33 — Resilience Patterns: Circuit Breakers, Bulkheads, Retries  ([chapter](33-resilience-patterns-circuit-breakers.md))
**Time:** ~6 h · **Needs:** Python asyncio + FastAPI, compose toxiproxy. **Also do §11** (eight simulator experiments in [`../ai-rag/labs/llm-resilience/simulator.html`](../ai-rag/labs/llm-resilience/simulator.html)); the tasks below use real processes instead.

- [ ] **33.1 A retry storm on real sockets** *(Level: Core)*
  - **Goal:** §2.1–§2.4 and §2.7 with a real server.
  - **Do:** server with capacity 100 req/s (`asyncio.Semaphore(10)`, 100 ms work, 503 when 50 are queued).
    Open-loop client at 80 req/s, 500 ms timeout. At t = 20 s make the server return 503 for 3 s. Four
    runs: (a) 3 immediate retries, (b) exponential backoff, (c) + full jitter, (d) + a 10% retry budget.
    Record attempts per 100 ms bucket and goodput per second.
  - **Predict:** peak attempts/s and seconds to recover goodput in each run.
  - **Verify:** (a) peak ≈ 4× offered load and slow recovery (maybe none); (d) peak ≈ 1.1× and recovery within ~1 s of the fault ending.
- [ ] **33.2 Retries multiply per layer** *(Level: Core)*
  - **Goal:** §2.1's amplification.
  - **Do:** A → B → C over `app0..app2`, each layer 3 attempts. Make C fail 100% for 10 s. Count calls arriving at C per user request.
  - **Predict:** calls at C per user request.
  - **Verify:** 27. Then retry only at the edge and show 3.
- [ ] **33.3 Circuit breaker states** *(Level: Core)*
  - **Goal:** §3.2–§3.3 as a timeline.
  - **Do:** implement closed → open (50% failures over the last 20 calls, min 10) → half-open after 5 s
    (3 probes) → closed. Put it in front of `app1`; `cut app1` for 30 s, then `heal app1`. Log every transition and every call that reached the backend.
  - **Predict:** backend calls during the 30 s outage; time from heal to closed.
  - **Verify:** outage calls ≈ trip threshold + probes (not thousands); closed within ~5–10 s of heal.
- [ ] **33.4 Bulkhead** *(Level: Core)*
  - **Goal:** §4: a slow dependency must not take down a fast one.
  - **Do:** a service calls dep X (`app1`) and dep Y (`app2`) through one shared pool of 20 connections.
    Add a 5 s `latency` toxic on `app1`. Measure Y's success rate. Then give each its own `Semaphore(10)`.
  - **Verify:** Y success drops near 0 with the shared pool and stays ~100% with bulkheads.
- [ ] **33.5 Hedged requests** *(Level: Stretch)*
  - **Goal:** §2.6: tail cut vs extra load.
  - **Do:** a backend with 1% of calls taking 1 s (else 10 ms). Hedge after the p95 latency.
  - **Verify:** p99 falls from ~1 s to ~20 ms for about 5% extra load; record both numbers.

**Checkpoint (closed book):**
1. Three layers each make up to 3 attempts. How many calls can reach the bottom per user request?
2. Write full jitter, and say what it fixes.
3. Name the breaker states and what moves between them.
<details><summary>Answers</summary>

1. 3³ = 27.
2. `delay = random.uniform(0, min(cap, base * 2**attempt))`. It desynchronizes clients that failed together, so their retries do not arrive as one spike.
3. Closed → open when the failure rate over a window crosses a threshold; open → half-open after a cooldown; half-open → closed when probes succeed, back to open when one fails.
</details>

---

## 34 — Adaptive Load Control and Backpressure  ([chapter](34-adaptive-load-control-and-backpressure.md))
**Time:** ~6 h · **Needs:** Python asyncio, compose Redis. **Also do §15** (seven simulator experiments); here you build the mechanisms.

- [ ] **34.1 The knee of an M/M/1 queue** *(Level: Core)*
  - **Goal:** §2.1–§2.2 against a real event loop.
  - **Do:** one worker coroutine taking Exp(mean 10 ms) per job from an `asyncio.Queue`; an *open-loop*
    Poisson load generator (never wait for replies before sending). Run ρ = 0.5, 0.8, 0.9, 0.95 for 60 s each.
  - **Predict:** mean latency at each ρ from `W = 1/(μ − λ)`.
  - **Verify:** measured within ~20% of 20, 50, 100, 200 ms.
- [ ] **34.2 Queue collapse vs load shedding** *(Level: Core)*
  - **Goal:** §7.1–§7.4 and §4.
  - **Do:** same server at 120% load; clients give up after 1 s. Measure goodput (responses within
    the deadline per second) for: unbounded FIFO, bounded queue of 50 + immediate 503, LIFO, CoDel
    (target 5 ms, interval 100 ms).
  - **Predict:** when the FIFO's goodput reaches ~0.
  - **Verify:** FIFO goodput → ~0 after ≈ 5 s (queue delay grows 0.2 s per second); the other three hold ≈ 100 req/s.
- [ ] **34.3 An AIMD concurrency limiter** *(Level: Core)*
  - **Goal:** §6.3 against a moving ceiling.
  - **Do:** backend with true concurrency capacity 40, dropped to 10 at t = 30 s and restored at t = 60 s
    (excess requests queue). Client limiter: +1 per window of successes, ×0.5 when p90 latency > 2× the
    no-load latency or on 503. Compare with a fixed limit of 40.
  - **Predict:** the limit's range while capacity is 10.
  - **Verify:** a plot or table of the limit sawtoothing around 10, then 40; p99 and errors lower than the fixed limit during the drop.
- [ ] **34.4 A distributed rate limiter, broken then fixed** *(Level: Core)*
  - **Goal:** §8.1, §8.3, §8.5.
  - **Do:** 3 processes share a 100 req/s, burst 20 limit in Redis. Version 1: `GET` tokens, compute,
    `SET` (not atomic). Version 2: the same logic in one Lua `EVAL` (or GCRA with `redis.call('TIME')`).
    Each process tries 1,000 req/s for 60 s. See also [the rate limiter design](../solutions/api-gateway-rate-limiter-design.md).
  - **Predict:** admitted requests in 60 s for each version.
  - **Verify:** version 1 admits well above 6,020; version 2 admits ≤ 6,020 (100 × 60 + 20).
- [ ] **34.5 A metastable failure** *(Level: Stretch)*
  - **Goal:** §11.2: the system stays down after the trigger is gone.
  - **Do:** 34.2's FIFO server at 85% load plus clients that retry twice on timeout. Trigger: halve capacity for 5 s.
  - **Verify:** goodput stays near 0 long after capacity returns; adding the 33.1 retry budget or 34.2 shedding makes it recover within seconds.

**Checkpoint (closed book):**
1. M/M/1 with service time 10 ms at ρ = 0.9: mean time in system?
2. Why does an unbounded FIFO queue drive goodput to zero under overload with client deadlines?
3. What are AIMD's two rules, and what shape does its limit take?
<details><summary>Answers</summary>

1. `W = 1/(μ − λ) = 1/(100 − 90) = 100 ms`.
2. The queue grows until every request waits longer than its client's deadline, so the server spends all its capacity on answers nobody reads.
3. Add a constant on success, multiply by β < 1 on a congestion signal (drop, 503, latency above target). The limit forms a sawtooth just under capacity.
</details>

---

## 35 — Reliability Math: SLOs and Error Budgets  ([chapter](35-reliability-math-slos-and-error-budgets.md))
**Time:** ~3 h · **Needs:** Python + numpy; Docker for `promtool`. **Also do** the pre-mortem exercises in §7.

- [ ] **35.1 Burn-rate alerts on generated data** *(Level: Core)*
  - **Goal:** §5's multi-window, multi-burn-rate table, computed rather than recited.
  - **Do:** per-minute requests (1,000/min) and errors for 30 days: baseline 0.02% errors; day 10, a
    20-minute outage at 20%; days 20–22, a slow burn of +0.3%. SLO 99.9%. With cumulative sums,
    evaluate the 14.4× (1 h/5 m), 6× (6 h/30 m), 3× (1 d/2 h) and 1× (3 d/6 h) rules every minute.
  - **Predict:** minutes from outage start to the first page; whether the slow burn ever pages; budget left on day 30.
  - **Verify:** page after ≈ 4 min; the slow burn never pages and opens tickets after roughly 19–22 h; ≈ 40% budget left (baseline 20%, outage ≈ 9%, slow burn ≈ 30%).
- [ ] **35.2 The same alert as PromQL, unit-tested** *(Level: Core)*
  - **Goal:** make 35.1 deployable.
  - **Do:** recording rules for `sum(rate(http_requests_total{code=~"5.."}[1h])) / sum(rate(http_requests_total[1h]))`
    (and 5m), an alert requiring both > `14.4 * 0.001`, and a test file with `input_series` in expanding
    notation (`'0+1000x120'`). Run `docker run --rm -v $PWD:/w -w /w --entrypoint promtool prom/prometheus test rules tests.yml`.
  - **Verify:** the test fails with a wrong threshold and passes with the right one; one test case asserts no alert on a 2-minute blip.
- [ ] **35.3 Composite availability by Monte Carlo** *(Level: Core)*
  - **Goal:** §3's serial and parallel formulas, and where they break (§7 "Independent vs. Correlated Failures").
  - **Do:** simulate 12 serial dependencies at 99.9%; then 2 redundant replicas at 99% each; then the same pair with a shared failure 0.5% of the time.
  - **Predict:** all three availabilities.
  - **Verify:** ≈ 98.8%, ≈ 99.99%, ≈ 99.5%: correlation erases the redundancy.
- [ ] **35.4 Explain it** *(Level: Stretch)*
  - **Goal:** a 5-sentence error-budget policy (§6) for an engineering manager, using 35.1's day-30 numbers to say what gets frozen and when.

**Checkpoint (closed book):**
1. What fraction of a 30-day budget does a 14.4× burn consume in one hour?
2. Availability of 12 serial dependencies at 99.9% each?
3. Why does each burn-rate alert need a short window as well as a long one?
<details><summary>Answers</summary>

1. 14.4 × 1 h / 720 h = 2%.
2. 0.999¹² ≈ 98.8%.
3. The long window proves enough budget burned; the short one proves it is still burning, so the alert stops soon after the fix instead of staying red for the rest of the long window.
</details>

---

## 36 — Multi-Region: Active-Active, Geo-Replication, Evacuation  ([chapter](36-multi-region-active-active-and-geo-replication.md))
**Time:** ~5 h · **Needs:** Python; compose Postgres + toxiproxy for 36.2 and 36.4

- [ ] **36.1 Commit latency for a placement** *(Level: Core)*
  - **Goal:** §4.1's formula `T_commit = T_fsync + RTT_(Q−1)(leader)`.
  - **Do:** write `commit_latency(voters: dict[str, int], leader: str, rtt: dict) -> float` and reproduce
    rows P1–P7 of the §4.1 table from its RTTs; add a column for the leader's region lost.
  - **Predict:** P6 (E:2, W:2, witness in Ohio) before computing.
  - **Verify:** your function matches every row (e.g. P3 ≈ 66 ms, P6 ≈ 13 ms, P7 ≈ 66 ms).
- [ ] **36.2 Synchronous replication across fake regions** *(Level: Core)*
  - **Goal:** measure §2.4 and §3.3 on real Postgres.
  - **Do:** toxics on `pg_repl1` of 32 ms downstream + 33 ms upstream (RTT 65 ms) and on `pg_repl2`
    37 + 38 ms (75 ms). `ALTER SYSTEM SET synchronous_standby_names = 'ANY 1 (replica1, replica2)'; SELECT pg_reload_conf();`
    Run `pgbench -N -c 1 -T 30` and `pgbench -N -c 16 -j 4 -T 30` (`-N` avoids branch-row lock contention);
    then `'FIRST 2 (replica1, replica2)'`; then `PGOPTIONS='-c synchronous_commit=local'`.
  - **Predict:** single-client TPS for each setting, and what 16 clients change.
  - **Verify:** ≈ 15 TPS for ANY 1 (≈ 68 ms/commit), ≈ 13 for FIRST 2, hundreds for local; 16 clients give ≈ 16× the TPS at the same latency.
    Undo with `ALTER SYSTEM RESET synchronous_standby_names; SELECT pg_reload_conf();` or later labs hang on commit.
- [ ] **36.3 Region evacuation with a latency model** *(Level: Core)*
  - **Goal:** §7.4, §7.6, §7.7.
  - **Do:** regions us-east 45%, us-west 20%, eu-west 35% of a 10k req/s peak, each sized for 70% at its
    normal peak. Latency per region = user RTT + M/M/c queueing (Erlang C, c = 100 servers). Evacuate
    eu-west to us-east in steps 10/25/50/100% every 5 min; cache hit rate of moved traffic rises as `0.9 * (1 - exp(-t/120 s))`, misses hit the DB.
  - **Predict:** us-east utilization after full evacuation.
  - **Verify:** ≈ 124% (80% of global peak onto capacity sized for 45% at 70%), so p99 diverges; find the smallest us-east size or the split (e.g. part to another EU region) that keeps it ≤ 70%.
- [ ] **36.4 Promote a lagging replica: measured RPO, then split brain** *(Level: Stretch)*
  - **Goal:** §7.3 and §7.5.
  - **Do:** async replication, 500 ms toxic on `pg_repl1`; a writer inserts ids every 5 ms and logs each acked id. `docker kill pg-primary`; on replica1 `SELECT pg_promote();`. Compare acked ids with rows present. Then `docker start pg-primary` and write to it.
  - **Predict:** acknowledged writes lost.
  - **Verify:** lost ≈ writes in the last ~0.5 s; the old primary accepts writes too (two primaries). Write the fencing step your runbook needs.
    Reset afterwards with `docker compose down -v && docker compose up -d`.

**Checkpoint (closed book):**
1. Formula for the leader's commit latency under a majority quorum?
2. Why can't a two-region deployment survive losing either region with a majority quorum?
3. Highest safe steady-state utilization with N equal regions and ceiling u_max?
<details><summary>Answers</summary>

1. `T_fsync` + the (Q−1)-th smallest RTT from the leader to the other voters.
2. One region must hold a majority of voters; losing that region leaves a minority, which cannot commit.
3. `u_safe = u_max × (N − 1) / N`.
</details>

---

## 37 — Debugging Distributed Systems in Production  ([chapter](37-distributed-systems-debugging.md))
**Time:** ~6 h · **Needs:** compose with `--profile tracing` (Jaeger), Postgres, Kafka, toxiproxy; Python + OpenTelemetry. **Also do §23** (seven sandbox experiments).

- [ ] **37.1 Build the 8-second request on purpose** *(Level: Core)*
  - **Goal:** recreate the chapter's running example and find it with traces.
  - **Do:** three FastAPI services: gateway (:8000, `httpx` to order with 2.5 s per-try timeout + 1
    jittered retry), order (:8001, `asyncio.Semaphore(4)` as the worker queue, `psycopg_pool` with
    `max_size=2` to `localhost:15432` with a 50 ms toxic, 20 parallel calls to pricing), pricing (:8002, 2%
    of calls sleep 400 ms). Run each with `OTEL_SERVICE_NAME=<svc> OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 opentelemetry-instrument uvicorn ...`.
    Drive 30 req/s open-loop for 3 min.
  - **Verify:** each service's handler p99 < 500 ms while the client max > 5 s. In Jaeger (`localhost:16686`,
    min duration 5 s), decompose one slow trace into ≥ 4 mechanisms (§3) that sum to the client time within 5%.
- [ ] **37.2 Find what is in the gap** *(Level: Core)*
  - **Goal:** §6.2: auto-instrumentation does not show pool or queue waits.
  - **Do:** add manual spans around `pool.connection()` acquisition and around the semaphore wait in order.
  - **Verify:** the unexplained gap in 37.1's trace becomes named spans; record the pool wait's share of the total.
- [ ] **37.3 Break context propagation, then fix it** *(Level: Core)*
  - **Goal:** §5.3–§5.4.
  - **Do:** order publishes an event to Kafka and a consumer handles it. Without header injection the
    consumer starts a new trace. Fix: `opentelemetry.propagate.inject(carrier)` into the message headers
    and `extract` in the consumer.
  - **Verify:** before, two unrelated traces; after, one trace crossing Kafka with the consumer span under the producer.
- [ ] **37.4 Head sampling throws away the slow trace** *(Level: Stretch)*
  - **Goal:** §6.4.
  - **Do:** set `OTEL_TRACES_SAMPLER=parentbased_traceidratio`, `OTEL_TRACES_SAMPLER_ARG=0.01`, rerun 37.1.
    Then put an OpenTelemetry Collector (contrib image) with a `tail_sampling` processor (latency policy, 2,000 ms) in front of Jaeger.
  - **Predict:** probability of keeping at least one of the k slow requests at 1% head sampling.
  - **Verify:** `1 − 0.99^k` matches roughly; with tail sampling every slow trace is kept.
- [ ] **37.5 Explain it** *(Level: Core)*
  - **Goal:** a 5-sentence incident note to the support lead explaining why every dashboard was green while the customer waited 8 s, using 37.1's decomposition.

**Checkpoint (closed book):**
1. Why does a handler latency histogram miss queueing time?
2. With 20 parallel calls, how often is at least one slower than the callee's p99?
3. What is wrong with head sampling for debugging tail latency?
<details><summary>Answers</summary>

1. Its timer starts when a worker picks the request up; time in the accept queue, load balancer or worker queue happened before.
2. `1 − 0.99²⁰ ≈ 18%` of requests.
3. The keep/drop decision is made before the latency is known, so rare slow traces are dropped at the sampling rate. Tail sampling decides after the trace completes.
</details>

---

## 38 — Disaster Recovery: Backups, PITR, RPO/RTO  ([chapter](38-disaster-recovery-backups-rpo-rto.md))
**Time:** ~5 h · **Needs:** compose Postgres (`--profile dr` for 38.2), `pgbench`. **Also do §16** (PITR after a bad `UPDATE`, plus its variations).

- [ ] **38.1 Replication is not backup** *(Level: Core)*
  - **Goal:** §1: a logical disaster reaches every copy.
  - **Do:** `DELETE FROM pgbench_accounts WHERE aid % 2 = 0;` on the primary while polling `count(*)` on both replicas every 10 ms.
  - **Predict:** how long the replicas keep the deleted rows.
  - **Verify:** both replicas lose the rows within tens of ms; record the number.
- [ ] **38.2 A delayed replica as a fast undo** *(Level: Core)*
  - **Goal:** §5.5.
  - **Do:** `docker compose --profile dr up -d pg-delayed` (10 min apply delay). Wait 10 min, run the bad
    `DELETE`, "notice" it 3 min later: `SELECT pg_wal_replay_pause();` on `pg-delayed`, copy the lost rows
    out with `\copy (SELECT ...) TO` and back into the primary with `INSERT ... ON CONFLICT DO NOTHING`.
  - **Predict:** minutes from "noticed" to "repaired", compared with your §16 PITR time.
  - **Verify:** all rows back, new rows written after the incident kept; repair time a fraction of the PITR time.
- [ ] **38.3 Timed restore drill with an RTO decomposition** *(Level: Core)*
  - **Goal:** §2.3 and §8.3: predict RTA from measured rates.
  - **Do:** `pgbench -i -s 100`; `docker exec -u postgres pg-primary pg_basebackup -D /archive/base1 -Fp -X stream -c fast`;
    `pgbench -c 8 -T 300`; `SELECT pg_switch_wal()`. Restore into a new container as in §16 (with `postgres:17`,
    `--network dslab_default`, volume `dslab_pgarchive`, and `-c max_prepared_transactions=20`, or recovery
    aborts because the primary's value is higher) to a target time, timing `t_restore` (copy),
    `t_replay` (logs: start to "recovery stopping"), `t_verify` (`pg_amcheck --install-missing --all --heapallindexed`
    plus a row-count and `sum(abalance)` check against values recorded before). Compute `B` and `R_replay`.
  - **Predict:** before a second drill at `-s 200` with 600 s of pgbench, predict its RTA from the first drill's rates.
  - **Verify:** the second drill's RTA within ±30% of the prediction; a filled-in §2.3 table for your laptop.
- [ ] **38.4 Measure RPO when the disk is gone** *(Level: Core)*
  - **Goal:** §2.2: the real RPO of WAL archiving.
  - **Do:** `ALTER SYSTEM SET archive_timeout = '60s'; SELECT pg_reload_conf();`. A writer inserts an id every
    10 ms and logs acked ids. Take a fresh base backup, wait 2 min, then `docker kill pg-primary` and pretend
    its volume is lost: restore only from `/archive` (that base + archived WAL). Repeat with `archive_timeout = '10s'`.
  - **Predict:** lost seconds of acked writes for 60 s and 10 s.
  - **Verify:** loss ≤ archive_timeout and roughly uniform in [0, timeout] across 3 runs; `pg_stat_archiver.last_archived_time` explains each.
- [ ] **38.5 A corrupt page, caught by checksums** *(Level: Stretch)*
  - **Goal:** §8.3's structural level; the stack's primary has `--data-checksums`.
  - **Do:** on a stopped *copy* of a restored data directory, overwrite 16 bytes in the middle of a table's file (`SELECT pg_relation_filepath('pgbench_accounts')`, then `dd conv=notrunc`). Start it and run `pg_amcheck --all` and `SELECT sum(abalance) FROM pgbench_accounts` (a heap scan; `count(*)` may use an index-only scan and never read the page).
  - **Verify:** `invalid page in block N` errors name the block; restoring that table from backup clears them.
    Repeat on another copy after `pg_checksums --disable`: the corrupt read is silent or returns garbage.
- [ ] **38.6 Explain it** *(Level: Stretch)*
  - **Goal:** a one-page runbook (§9.1 format) for "bad UPDATE noticed within 1 hour", with your measured times from 38.2 and 38.3. Compare with [the WAL deep dive](../solutions/write-ahead-log-deep-dive.md).

**Checkpoint (closed book):**
1. Why is replication not a backup?
2. List the phases of RTA after detection.
3. When is a delayed replica useless?
<details><summary>Answers</summary>

1. Replication copies every change, including a bad `UPDATE`, `DELETE` or corrupting bug, to every copy within seconds. A backup keeps older states.
2. Decide, provision, restore, replay, verify, cut over, warm (detection is measured separately).
3. When the mistake is noticed after the apply delay has passed (`t_detect > delay`), or for failover, since it is hours behind.
</details>

---

## Capstone projects

Each takes 1–3 days on a laptop with the shared compose stack. Keep a results table per capstone in `lab-notebook.md`.

### A. Replicated KV store with Raft and linearizable reads  (00, 03, 04, 29, 10)
**Spec.** A 3- or 5-node KV service (`PUT`, `GET`, `CAS`) on your Raft (Go, from 03.5, or Python
asyncio). Nodes talk through toxiproxy proxies so you can cut links. Reads use ReadIndex (§7.2 of 03);
clients attach `(client_id, seq)` so retried writes apply once. A nemesis script kills, pauses
(`kill -STOP`) and partitions nodes every 10 s. Every client op is logged as `invoke`/`ok`/`fail`/`info`
(unknown). Design reference: [KV store design](../solutions/key-value-store-design.md).
**Acceptance.**
- 20 runs of 60 s with the nemesis: 0 linearizability violations under Porcupine (or your 00.2 checker, extended with pruning).
- No acknowledged write is lost after any single-node kill.
- A deliberately broken build (reads served locally by any node) is caught by the checker in ≥ 1 of 5 runs.

**What to measure.** Throughput and p99 for writes and reads; unavailability window after a leader
kill vs election timeout; `info` (unknown-outcome) rate per nemesis type; ReadIndex vs lease-read latency.

### B. Outbox + saga checkout that survives chaos  (06, 07, 08, 33, 34, 37)
**Spec.** `POST /checkout` with an idempotency key (06.3) writes the order, saga state and an outbox row
in one transaction. A relay publishes to Kafka (06.2). Inventory and payment consumers use an inbox
table; payment calls a fake provider through toxiproxy with a breaker (33.3) and a retry budget (33.1).
The orchestrator persists state and compensates (06.4). Product reads go through a cache with XFetch (08.2).
Every hop propagates trace context, including Kafka (37.3). Compare with [workflow orchestration](../solutions/workflow-orchestration-design.md).
**Acceptance.** A 30-minute chaos run (random `kill -9` of any service every 20 s, one broker kill, provider
latency and 503 bursts, a client that retries every request with the same key) ends with a reconciliation
query showing: every order either paid + reserved + confirmed or refunded + released + cancelled; 0 double
charges; 0 events missing from the outbox-to-consumer path; 0 sagas in a non-terminal state older than 5 min.
**What to measure.** Saga completion p50/p99; duplicates absorbed by inboxes; breaker open time; the slowest checkout's trace decomposition.

### C. A multi-replica Postgres app that demonstrates every session guarantee  (04, 36, 38, 37)
**Spec.** A FastAPI notes app on the primary + 2 replicas with a routing layer that can run naive
(random replica) or guaranteed. Implement read-your-writes, monotonic reads, monotonic writes,
writes-follow-reads and consistent prefix (04 §7.1–§7.6) with LSN tokens in a signed cookie. One test
per guarantee reproduces its violation in naive mode under toxiproxy lag.
**Acceptance.** Every test fails in naive mode and passes in guaranteed mode at replica lags of 0, 100 ms
and 2 s; after promoting a replica (36.4) the app keeps its guarantees or falls back to the new primary,
never serves older data than a session has seen; a PITR restore (38) of the database passes the same suite.
**What to measure.** Share of reads served by replicas; added read latency p50/p99 per guarantee; primary fallback rate vs lag.

### D. Overload game day  (29, 33, 34, 35, 37)
**Spec.** One service with an AIMD limiter (34.3), shedding (34.2), a breaker toward its dependency (33.3),
phi-accrual health of its dependency (29.1), Prometheus metrics and the 35.2 burn-rate alerts. A load
script runs a 2× traffic spike, a dependency brownout and a 5 s capacity halving (34.5).
**Acceptance.** No metastable state: goodput is back within 10 s of each fault ending; the fast-burn
page fires during the brownout and not during a 2-minute blip; your written prediction of each alert's firing time is within 2 minutes.
**What to measure.** Goodput, p99, shed rate, limiter value and budget burned per fault.
